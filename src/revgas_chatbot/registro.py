"""Registrar a venda de um pedido confirmado (M3).

`RegistradorDeVendas.registrar(pedido)` é a única porta de entrada: valida
o pedido, garante o cliente, monta e envia a venda e interpreta o resultado.
A API é injetada (`ApiRevGas`), então tudo pode ser testado sem rede.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from revgas_chatbot.api import ApiRevGas, Relogio, Resposta, TempoEsgotado
from revgas_chatbot.estados import nome_do_estado
from revgas_chatbot.pedido import ForaDoCatalogo, Invalido, Pedido, Problema, ProdutoCatalogo, validar
from revgas_chatbot.venda import CANAL, marca, montar_corpo_venda


@dataclass(frozen=True)
class VendaCriada:
    id: int
    total: Decimal
    status: str


@dataclass(frozen=True)
class PedidoInvalido:
    """Algum dado do cliente está faltando ou errado: o chatbot pede correção."""

    problemas: tuple[Problema, ...]


@dataclass(frozen=True)
class TransferirAtendente:
    """O chatbot não consegue concluir: passa para um atendente humano.

    `venda_pode_existir` avisa o atendente para procurar a venda pela marca
    `[chatbot:<pedido_id>]` antes de criar outra.
    """

    motivo: str
    pedido_id: str
    venda_pode_existir: bool = False


@dataclass(frozen=True)
class EmAndamento:
    """O mesmo pedido já está sendo registrado: nada é enviado de novo."""

    pedido_id: str


Resultado = VendaCriada | PedidoInvalido | TransferirAtendente | EmAndamento

_PROCESSANDO = object()

# Novas tentativas além da primeira.
REPETICOES_409 = 1  # só para 409 de lock
REPETICOES_429 = 2  # 3 tentativas no total

# Reconciliação depois de timeout no POST /sales.
TENTATIVAS_DE_RECONCILIACAO = 3
ESPERA_ENTRE_TENTATIVAS = 3.0  # segundos
MARGEM_ANTES_DO_ENVIO = timedelta(minutes=5)

# Limite global da API: 1 requisição por segundo.
INTERVALO_MINIMO = 1.0


def _travado(resposta: Resposta) -> bool:
    """409 de lock no banco da API (o único 409 que vale repetir)."""
    if resposta.status != 409 or not isinstance(resposta.dados, dict):
        return False
    return "lock" in str(resposta.dados.get("message", "")).lower()


def _aviso_de_venda(pedido_id: str) -> str:
    return f"pode existir venda com a marca {marca(pedido_id)}: verifique antes de criar outra"


def _venda_criada(venda: dict) -> VendaCriada:
    return VendaCriada(id=venda["id"], total=Decimal(str(venda["total"])), status=venda["status"])


class _Parar(Exception):
    """Interrompe o processamento com um resultado já decidido."""

    def __init__(self, resultado: Resultado) -> None:
        self.resultado = resultado


# Campos da API que são dados informados pelo cliente -> (campo do Pedido, mensagem).
# Erro em qualquer outro campo é de configuração: vai para o atendente.
_CAMPOS_DO_CLIENTE = {
    "name": ("nome", "confira o seu nome"),
    "phones": ("telefone", "confira o número de telefone"),
    "location": ("endereco.logradouro", "confira a rua do endereço"),
    "number": ("endereco.numero", "confira o número do endereço"),
    "neighborhood": ("endereco.bairro", "confira o bairro"),
    "city": ("endereco.cidade", "confira a cidade"),
    "state_federation": ("endereco.estado", "confira o estado"),
    "complement": ("endereco.complemento", "confira o complemento do endereço"),
    "landmark": ("endereco.referencia", "confira o ponto de referência"),
}


def _problemas_do_cliente(dados: Any) -> tuple[Problema, ...]:
    """Problemas de um 422, se TODOS os campos rejeitados forem do cliente.

    Devolve vazio se houver campo de configuração ou desconhecido. A mensagem
    é nossa: o texto da API nunca chega ao cliente.
    """
    erros = dados.get("data") if isinstance(dados, dict) else None
    if not erros:
        return ()
    campos = [e.get("field") for e in erros if isinstance(e, dict)]
    if len(campos) != len(erros) or any(c not in _CAMPOS_DO_CLIENTE for c in campos):
        return ()
    return tuple(Problema(*_CAMPOS_DO_CLIENTE[c]) for c in dict.fromkeys(campos))


def dados_do_cliente(pedido: Pedido) -> dict[str, Any]:
    """Corpo do POST /customers para um cliente novo (pessoa física)."""
    endereco = pedido.endereco
    assert endereco is not None  # garantido pela validação
    dados: dict[str, Any] = {
        "name": pedido.nome.strip(),
        "type": "PHYSICAL",
        "phones": [pedido.telefone],
        "location": endereco.logradouro.strip(),
        "number": endereco.numero.strip(),
        "neighborhood": endereco.bairro.strip(),
        "city": endereco.cidade.strip(),
        "state_federation": nome_do_estado(endereco.estado) or endereco.estado.strip(),
    }
    if endereco.complemento and endereco.complemento.strip():
        dados["complement"] = endereco.complemento.strip()
    if endereco.referencia and endereco.referencia.strip():
        dados["landmark"] = endereco.referencia.strip()
    return dados


class RegistradorDeVendas:
    def __init__(
        self,
        *,
        api: ApiRevGas,
        relogio: Relogio,
        catalogo: dict[int, ProdutoCatalogo],
        pagamentos_permitidos: set[str],
        establishment_id: int,
    ) -> None:
        self._api = api
        self._relogio = relogio
        self._catalogo = catalogo
        self._pagamentos_permitidos = pagamentos_permitidos
        self._establishment_id = establishment_id
        # Trava por pedido_id: ausente = NOVO; _PROCESSANDO; ou o Resultado final.
        self._pedidos: dict[str, object] = {}
        self._ultima_requisicao: datetime | None = None
        # Pedidos cujo POST /sales já saiu (a partir daí a trava nunca se solta).
        self._venda_enviada: set[str] = set()

    def registrar(self, pedido: Pedido) -> Resultado:
        """Registra a venda do pedido confirmado. Um pedido_id gera no máximo uma venda."""
        estado = self._pedidos.get(pedido.pedido_id)
        if estado is _PROCESSANDO:
            return EmAndamento(pedido.pedido_id)
        if estado is not None:
            return estado  # já concluído: devolve o mesmo resultado

        self._pedidos[pedido.pedido_id] = _PROCESSANDO
        try:
            resultado = self._processar(pedido)
        except Exception:
            if pedido.pedido_id not in self._venda_enviada:
                # Nada foi enviado à API de venda: é seguro voltar a NOVO.
                del self._pedidos[pedido.pedido_id]
                raise
            # O POST /sales já saiu: a venda pode existir. Nunca soltar a trava.
            resultado = TransferirAtendente(
                f"falha depois de enviar a venda; {_aviso_de_venda(pedido.pedido_id)}",
                pedido.pedido_id,
                venda_pode_existir=True,
            )
        if isinstance(resultado, PedidoInvalido):
            del self._pedidos[pedido.pedido_id]  # volta a NOVO: o cliente corrige e confirma de novo
        else:
            self._pedidos[pedido.pedido_id] = resultado
        return resultado

    def _processar(self, pedido: Pedido) -> Resultado:
        validacao = validar(pedido, self._catalogo, self._pagamentos_permitidos)
        if isinstance(validacao, Invalido):
            return PedidoInvalido(validacao.problemas)
        if isinstance(validacao, ForaDoCatalogo):
            # A conversa só oferece o catálogo: isto é erro de configuração.
            return TransferirAtendente("produto fora do catálogo", pedido.pedido_id)

        try:
            customer_id = self._garantir_cliente(pedido)
            corpo = montar_corpo_venda(
                pedido,
                customer_id=customer_id,
                catalogo=self._catalogo,
                establishment_id=self._establishment_id,
            )
            enviado_em = self._relogio.agora()
            self._venda_enviada.add(pedido.pedido_id)
            try:
                resposta = self._chamar(lambda: self._api.criar_venda(corpo))
            except TempoEsgotado:
                # O servidor pode ter criado a venda: nunca reenviar às cegas.
                return self._reconciliar(pedido, customer_id, enviado_em)
            if 500 <= resposta.status <= 599:
                # 5xx também é incerto (ex.: 504 de um proxy depois de criar a venda).
                return self._reconciliar(pedido, customer_id, enviado_em)
            venda = self._exigir_status(resposta, 201, pedido, "criar a venda")
        except _Parar as parada:
            return parada.resultado
        return _venda_criada(venda)

    def _reconciliar(self, pedido: Pedido, customer_id: int, enviado_em: datetime) -> Resultado:
        """Procura a venda pela marca depois de um timeout no POST /sales."""
        for tentativa in range(TENTATIVAS_DE_RECONCILIACAO):
            if tentativa:
                self._relogio.dormir(ESPERA_ENTRE_TENTATIVAS)  # a venda pode aparecer com atraso
            try:
                venda = self._procurar_venda(pedido.pedido_id, customer_id, enviado_em)
            except TempoEsgotado:
                venda = None
            if venda is not None:
                return _venda_criada(venda)
        return TransferirAtendente(
            f"resultado incerto ao criar a venda; {_aviso_de_venda(pedido.pedido_id)}",
            pedido.pedido_id,
            venda_pode_existir=True,
        )

    def _procurar_venda(self, pedido_id: str, customer_id: int, enviado_em: datetime) -> dict | None:
        # `from`/`to` aceitam só data, em fuso desconhecido: 1 dia de folga de cada lado
        # (cobre UTC x UTC-3 e o envio às 23:59 procurado depois da meia-noite).
        de = enviado_em.date() - timedelta(days=1)
        ate = self._relogio.agora().date() + timedelta(days=1)
        limite = enviado_em - MARGEM_ANTES_DO_ENVIO
        pagina = 1
        while True:
            resposta = self._chamar(
                lambda: self._api.buscar_vendas(
                    customer_id=customer_id, channel=CANAL, de=de, ate=ate, pagina=pagina
                )
            )
            if resposta.status != 200:
                return None
            vendas = resposta.dados.get("data", [])
            for venda in vendas:
                if marca(pedido_id) in (venda.get("observation") or ""):
                    return venda
            # A lista vem da mais nova para a mais antiga: passou do envio, pode parar.
            if any(datetime.fromisoformat(v["created_at"]) < limite for v in vendas):
                return None
            if not resposta.dados.get("pagination", {}).get("has_next"):
                return None
            pagina += 1

    def _garantir_cliente(self, pedido: Pedido) -> int:
        clientes = self._buscar_clientes(pedido)
        if len(clientes) > 1:
            raise _Parar(TransferirAtendente("mais de um cliente com o mesmo telefone", pedido.pedido_id))
        if clientes:
            return clientes[0]["id"]
        try:
            resposta = self._chamar(lambda: self._api.cadastrar_cliente(dados_do_cliente(pedido)))
        except TempoEsgotado:
            # O cadastro pode ter sido feito: buscar de novo em vez de recadastrar.
            clientes = self._buscar_clientes(pedido)
            if len(clientes) == 1:
                return clientes[0]["id"]
            raise _Parar(
                TransferirAtendente("resultado incerto ao cadastrar o cliente", pedido.pedido_id)
            ) from None
        return self._exigir_status(resposta, 201, pedido, "cadastrar o cliente")["id"]

    def _buscar_clientes(self, pedido: Pedido) -> list[dict]:
        try:
            resposta = self._chamar(lambda: self._api.buscar_clientes(pedido.telefone))
        except TempoEsgotado:
            raise _Parar(
                TransferirAtendente("sem resposta da API ao buscar o cliente", pedido.pedido_id)
            ) from None
        return self._exigir_status(resposta, 200, pedido, "buscar o cliente")["data"]

    def _chamar(self, requisicao: Callable[[], Resposta]) -> Resposta:
        """Faz a requisição com as únicas novas tentativas permitidas.

        409 de lock ("cannot modify object due to lock"): 1 nova tentativa.
        429 (limite): espera >= 1 s e tenta de novo, até 3 tentativas no
        total. Nos dois casos o servidor recusou, então nada foi criado e
        repetir é seguro. Outro 409 (ex.: dado duplicado) não é repetido.
        """
        repeticoes_409 = repeticoes_429 = 0
        while True:
            self._aguardar_vez()
            try:
                resposta = requisicao()
            finally:
                self._ultima_requisicao = self._relogio.agora()
            if _travado(resposta) and repeticoes_409 < REPETICOES_409:
                repeticoes_409 += 1
            elif resposta.status == 429 and repeticoes_429 < REPETICOES_429:
                repeticoes_429 += 1
            else:
                return resposta
            # A próxima tentativa espera >= 1 s por causa de _aguardar_vez.

    def _aguardar_vez(self) -> None:
        """Garante no máximo 1 requisição por segundo (limite global da API)."""
        if self._ultima_requisicao is None:
            return
        passou = (self._relogio.agora() - self._ultima_requisicao).total_seconds()
        if passou < INTERVALO_MINIMO:
            self._relogio.dormir(INTERVALO_MINIMO - passou)

    def _exigir_status(self, resposta: Resposta, status: int, pedido: Pedido, etapa: str) -> Any:
        """Devolve os dados se o status for o exigido; senão, para com o resultado certo."""
        if resposta.status == status:
            return resposta.dados
        if resposta.status in (400, 422):
            problemas = _problemas_do_cliente(resposta.dados)
            if problemas:
                raise _Parar(PedidoInvalido(problemas))
        raise _Parar(
            TransferirAtendente(f"erro HTTP {resposta.status} ao {etapa}", pedido.pedido_id)
        )

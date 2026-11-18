"""Devolução: achar os pedidos do cliente e abrir solicitações para a equipe.

Nesta parte o chatbot SÓ LÊ a API: nunca cancela, estorna ou altera nada.
Quem decide e executa é sempre uma pessoa da equipe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping

from revgas_chatbot.api_revgas.api import Relogio, TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.atendimento.atendimento import TransferirAtendente
from revgas_chatbot.atendimento.fila import TIPOS as TIPOS_DA_FILA
from revgas_chatbot.atendimento.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.pedido import Problema, ProdutoCatalogo
from revgas_chatbot.vendas.venda import reais


# Quantos pedidos o chatbot mostra ao cliente (decisão #7).
MAXIMO_DE_PEDIDOS = 3

# Teto de páginas lidas numa busca (proteção contra uma API que nunca termina).
MAXIMO_DE_PAGINAS = 20


@dataclass(frozen=True)
class PedidoDoCliente:
    id: int
    data: date
    itens: tuple[tuple[str, int], ...]  # (descrição, quantidade)
    total: Decimal
    status: str  # status da API: só para o atendente (o significado para o cliente é pendente)
    pagamentos: tuple[str, ...] | None = None  # ticket 07: formas de pagamento; None = desconhecidas
    criada_em: datetime | None = None  # M1 (18/11): para saber se o entregador pode ter saído
    entregue: bool = False  # M3 (18/11): status CONFIRM ou `delivered_at`

    @property
    def cancelado(self) -> bool:
        return self.status == "CANCEL"  # ✅ "Cancelada" na doc da API

    def texto_para_cliente(self) -> str:
        """Sem status (o significado de cada um para o cliente é pendente),
        exceto o rótulo "(cancelado)"."""
        texto = self._descricao()
        return f"{texto} (cancelado)" if self.cancelado else texto

    def texto_para_atendente(self) -> str:
        return f"{self._descricao()} (status na API: {self.status})"

    def _descricao(self) -> str:
        itens = ", ".join(f"{quantidade}x {descricao}" for descricao, quantidade in self.itens)
        return f"Pedido {self.id} de {self.data:%d/%m/%Y}: {itens}, total {reais(self.total)}"


@dataclass(frozen=True)
class PedidosEncontrados:
    customer_id: int
    nome: str | None
    pedidos: tuple[PedidoDoCliente, ...]


@dataclass(frozen=True)
class NenhumPedido:
    """Cliente não cadastrado ou sem pedido no prazo: o chatbot oferece o atendente."""


ResultadoDaBusca = PedidosEncontrados | NenhumPedido | TransferirAtendente


@dataclass(frozen=True)
class SolicitacaoAberta:
    protocolo: str
    mensagem_para_cliente: str
    entregador_pode_ter_saido: bool = False  # M1: a conversa avisa o cliente antes do protocolo


@dataclass(frozen=True)
class SolicitacaoInvalida:
    """Algum dado informado pelo cliente está faltando ou errado: ele corrige."""

    problemas: tuple[Problema, ...]

    def campos(self) -> list[str]:
        return [p.campo for p in self.problemas]


ResultadoDaSolicitacao = SolicitacaoAberta | SolicitacaoInvalida


@dataclass(frozen=True)
class RegraDoTipo:
    motivos: dict[str, str]  # motivo -> como aparece no resumo
    preferencia: str  # "obrigatoria", "opcional" ou "proibida"


_SO_TEXTO = {"OUTRO": "descrito pelo cliente"}

# Tipos que se abrem por `solicitar` (decisões #2, #6, #8 e #9).
# VAZAMENTO tem fluxo próprio; VENDA e COMPROVANTE_PIX vêm de outros fluxos.
REGRAS = {
    "CANCELAMENTO": RegraDoTipo(
        motivos={"DESISTI": "desisti", "PEDI_ERRADO": "pedi errado", "OUTRO": "outro"},  # DC-4: sem "demorou"
        preferencia="opcional",  # o pedido pode nem ter sido pago
    ),
    "ESTORNO": RegraDoTipo(
        motivos={
            "PEDIDO_CANCELADO_JA_PAGO": "pedido cancelado já pago",
            "PRODUTO_COM_PROBLEMA": "produto com problema",
            "COBRANCA_ERRADA": "cobrança errada",
            "OUTRO": "outro",
        },
        preferencia="obrigatoria",
    ),
    # O chatbot reconhece e transfere: só texto livre.
    "PROBLEMA_PRODUTO": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
    "ALTERACAO": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
    "VASILHAME": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
}
assert set(REGRAS) <= set(TIPOS_DA_FILA), "todo tipo de solicitação precisa existir na fila"

MAX_TEXTO = 200

# Preferência do cliente se houver valor a devolver (decisão #8).
PREFERENCIAS = {
    "CREDITO_PROXIMA_COMPRA": "crédito na próxima compra",
    "DINHEIRO_DE_VOLTA": "dinheiro de volta",
}


class Devolucao:
    def __init__(
        self,
        *,
        api: ApiCadenciada,
        relogio: Relogio,
        fila: FilaDeAtendimento,
        prazo_dias: int = 7,
        cliente_fixo: int | None = None,  # --venda-real: /customers dá 403 (D16)
        catalogo: Mapping[int, ProdutoCatalogo] | None = None,  # ticket 06: nome do produto
    ) -> None:
        if not isinstance(api, ApiCadenciada):
            # Uma API crua aqui criaria um segundo controle de ritmo (ou nenhum).
            raise TypeError("Devolucao precisa da ApiCadenciada compartilhada (ponto único de montagem)")
        self._api = api
        self._relogio = relogio
        self._fila = fila
        self._prazo_dias = prazo_dias
        self._cliente_fixo = cliente_fixo
        self._nomes = {pid: p.nome for pid, p in (catalogo or {}).items()}

    def _entregador_pode_ter_saido(self, tipo: str, pedido: PedidoDoCliente | None) -> bool:
        """M1 (18/11): cancelamento de pedido não entregue feito há até 2 horas (pelos dados do pedido)."""
        if tipo != "CANCELAMENTO" or pedido is None or pedido.criada_em is None:
            return False
        if pedido.entregue or pedido.cancelado:
            return False
        return self._relogio.agora() - pedido.criada_em <= timedelta(hours=2)

    @property
    def usa_cliente_fixo(self) -> bool:
        """Modo real: os pedidos vêm do cliente de demonstração, não do telefone."""
        return self._cliente_fixo is not None

    def pedidos_do_cliente(self, telefone: str) -> ResultadoDaBusca:
        """Até 3 pedidos do cliente no prazo de devolução, mais recentes primeiro.

        Só leitura (GET). Vendas de qualquer canal. O prazo de N dias inclui
        do dia (hoje − N) até hoje, em datas de Teresina.

        Não confia nos filtros nem na ordem da API: lê todas as páginas,
        confere o cliente e a data de cada venda e ordena aqui. Assim um
        pedido de outra pessoa nunca aparece, mesmo se a API errar.

        ⚠️ A busca por telefone tem o risco da D8 da criar-venda (número com
        ou sem o 9º dígito): um "não encontrado" pode ser falso.

        Qualquer falha ou ambiguidade vira `TransferirAtendente`; nada encontrado
        vira `NenhumPedido`.
        """
        if self._cliente_fixo is not None:
            # Modo real: as vendas do cliente de demonstração, sem buscar pelo telefone.
            return self._pedidos_de(self._cliente_fixo, None)
        try:
            resposta = self._api.buscar_clientes(telefone)
        except TempoEsgotado:
            return TransferirAtendente("sem resposta da API ao buscar o cliente")
        if resposta.status != 200:
            return TransferirAtendente(f"erro HTTP {resposta.status} ao buscar o cliente")
        clientes = _lista(resposta.dados)
        if clientes is None or not all(_inteiro(c.get("id")) for c in clientes):
            return TransferirAtendente("resposta inesperada da API ao buscar o cliente")
        if not clientes:
            return NenhumPedido()
        if len(clientes) > 1:
            return TransferirAtendente("mais de um cliente com o mesmo telefone")
        [cliente] = clientes
        nome = cliente.get("name")
        return self._pedidos_de(cliente["id"], nome if isinstance(nome, str) else None)

    def _pedidos_de(self, customer_id: int, nome: str | None) -> ResultadoDaBusca:
        hoje = self._relogio.agora().astimezone(FUSO_TERESINA).date()
        inicio = hoje - timedelta(days=self._prazo_dias)
        vendas = []
        for pagina in range(1, MAXIMO_DE_PAGINAS + 1):
            try:
                resposta = self._api.buscar_vendas(
                    customer_id=customer_id,
                    channel=None,
                    de=inicio,
                    ate=hoje + timedelta(days=1),  # folga para o fuso da API
                    pagina=pagina,
                )
            except TempoEsgotado:
                return TransferirAtendente("sem resposta da API ao buscar os pedidos")
            if resposta.status != 200:
                return TransferirAtendente(f"erro HTTP {resposta.status} ao buscar os pedidos")
            lista = _lista(resposta.dados)
            if lista is None:
                return TransferirAtendente("resposta inesperada da API ao buscar os pedidos")
            vendas.extend(lista)
            if not _tem_proxima(resposta.dados):
                break
        else:
            return TransferirAtendente("pedidos demais para listar com segurança")

        if self._cliente_fixo is not None:
            # M2 (18/11): no modo real, só as vendas feitas pelo chatbot (não as do painel).
            vendas = [v for v in vendas if "[chatbot:" in str(v.get("observation") or "")]
        # Só as vendas DESTE cliente são validadas: venda de balcão (sem cliente)
        # ou de outra pessoa é simplesmente ignorada, mesmo se vier estranha.
        do_cliente = [v for v in vendas if _inteiro(v.get("customer_id")) and v["customer_id"] == customer_id]
        try:
            convertidas = [_venda(v, self._nomes) for v in do_cliente]
        except _VendaInvalida:
            return TransferirAtendente("resposta inesperada da API ao buscar os pedidos")
        do_cliente_no_prazo = [
            (criada_em, pedido) for criada_em, pedido in convertidas if inicio <= pedido.data <= hoje
        ]
        if not do_cliente_no_prazo:
            return NenhumPedido()
        do_cliente_no_prazo.sort(key=lambda par: par[0], reverse=True)  # mais novo primeiro
        return PedidosEncontrados(
            customer_id=customer_id,
            nome=nome,
            pedidos=tuple(pedido for _, pedido in do_cliente_no_prazo[:MAXIMO_DE_PEDIDOS]),
        )


    def solicitar(
        self,
        tipo: str,
        telefone: str,
        *,
        encontrados: PedidosEncontrados | None,
        escolha: int | None,
        motivo: str,
        texto: str | None = None,
        preferencia: str | None = None,
        nota: str | None = None,  # observação do sistema para a equipe (ex.: por que não há pedido)
    ) -> ResultadoDaSolicitacao:
        """Abre a solicitação na fila para uma pessoa da equipe resolver.

        - `encontrados`: o que `pedidos_do_cliente` devolveu para este telefone
          (None se o cliente não foi encontrado). Assim o pedido é sempre do cliente.
        - `escolha`: o número do pedido na lista (1, 2, 3) ou None ("não achei").
        - `texto`: o que o cliente escreveu; obrigatório com o motivo OUTRO e
          aceito com os demais. Vai para a fila como texto do cliente (mascarado).

        Não chama a API: o chatbot nunca cancela nem estorna sozinho.
        Dado inválido do cliente → `SolicitacaoInvalida` e nada é gravado.
        Argumento de tipo errado → TypeError (erro de programação).
        """
        if tipo not in REGRAS:
            raise ValueError(f"tipo que não se abre por solicitar: {tipo!r}")
        if not isinstance(telefone, str):
            raise TypeError("telefone deve ser texto")
        if encontrados is not None and not isinstance(encontrados, PedidosEncontrados):
            raise TypeError("encontrados deve ser o PedidosEncontrados da busca")
        if escolha is not None and (not _inteiro(escolha) or encontrados is None):
            raise TypeError("escolha deve ser o número inteiro de um pedido da lista da busca")
        regra = REGRAS[tipo]
        texto = texto.strip() if texto else None

        problemas = []
        if not telefone.strip():
            problemas.append(Problema("telefone", "precisamos de um telefone para a equipe falar com você"))
        pedido = None
        if escolha is not None:
            if 1 <= escolha <= len(encontrados.pedidos):
                pedido = encontrados.pedidos[escolha - 1]
            else:
                problemas.append(Problema("escolha", "escolha um dos números da lista"))
        if motivo not in regra.motivos:
            problemas.append(Problema("motivo", "escolha um dos motivos da lista"))
        if motivo == "OUTRO" and not texto:
            problemas.append(Problema("texto", "conte com suas palavras o que aconteceu"))
        if texto and len(texto) > MAX_TEXTO:
            problemas.append(Problema("texto", f"use no máximo {MAX_TEXTO} caracteres"))
        if preferencia is None and regra.preferencia == "obrigatoria":
            problemas.append(Problema("preferencia", "escolha crédito na próxima compra ou dinheiro de volta"))
        if preferencia is not None and (preferencia not in PREFERENCIAS or regra.preferencia == "proibida"):
            problemas.append(Problema("preferencia", "preferência que não se aplica a esta solicitação"))
        if tipo == "CANCELAMENTO" and pedido is not None and pedido.cancelado:
            problemas.append(Problema("tipo", "este pedido já está cancelado; se pagou, peça o estorno"))
        if problemas:
            return SolicitacaoInvalida(tuple(problemas))

        linhas = [
            pedido.texto_para_atendente() if pedido else "O cliente não encontrou o pedido na lista.",
            f"Motivo: {regra.motivos[motivo]}",
        ]
        recente = self._entregador_pode_ter_saido(tipo, pedido)
        if recente:
            hora = pedido.criada_em.astimezone(FUSO_TERESINA).strftime("%H:%M")
            linhas.append(f"Pedido feito às {hora} e ainda não entregue: o entregador pode já ter saído.")
        if preferencia is not None:
            rotulo = "Preferência" if regra.preferencia == "obrigatoria" else "Preferência, se houver valor a devolver"
            linhas.append(f"{rotulo}: {PREFERENCIAS[preferencia]}")
        if nota:
            linhas.append(nota)
        protocolo = self._fila.abrir(
            tipo,
            telefone,
            "\n".join(linhas),
            nome=encontrados.nome if encontrados else None,
            # Só identificadores e escolhas de lista: o texto do cliente vai à parte.
            dados={
                "pedido_id": pedido.id if pedido else None,
                "customer_id": encontrados.customer_id if encontrados else None,
                "motivo": motivo,
                "preferencia": preferencia,
            },
            texto_cliente=texto,
            alta=recente,
        )
        return SolicitacaoAberta(
            entregador_pode_ter_saido=recente,
            protocolo=protocolo,
            mensagem_para_cliente=(
                f"Sua solicitação foi registrada com o protocolo {protocolo}. "
                "Uma pessoa da nossa equipe vai continuar o atendimento."
            ),
        )


class _VendaInvalida(Exception):
    """Venda da API fora do formato esperado."""


def _inteiro(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)  # bool é subclasse de int


def _lista(dados: object) -> list[dict] | None:
    """A lista `data` de uma resposta de listagem, ou None se vier fora do formato."""
    lista = dados.get("data") if isinstance(dados, dict) else None
    if not isinstance(lista, list) or not all(isinstance(item, dict) for item in lista):
        return None
    return lista


def _tem_proxima(dados: dict) -> bool:
    paginacao = dados.get("pagination")
    return isinstance(paginacao, dict) and paginacao.get("has_next") is True


def _venda(venda: dict, nomes: Mapping[int, str] | None = None) -> tuple[datetime, PedidoDoCliente]:
    """(criada_em, pedido) de uma venda do cliente; lança _VendaInvalida."""
    try:
        if not _inteiro(venda.get("id")):
            raise _VendaInvalida("id")
        criada_em = datetime.fromisoformat(venda["created_at"])
        if criada_em.tzinfo is None:
            raise _VendaInvalida("created_at sem fuso")
        produtos = venda.get("products")
        if not isinstance(produtos, list) or not produtos:
            raise _VendaInvalida("sem produtos")
        itens = []
        for p in produtos:
            if not isinstance(p, dict) or not _inteiro(p.get("quantity")) or p["quantity"] < 1:
                raise _VendaInvalida("quantidade")
            descricao = p.get("product_description")
            if not isinstance(descricao, str) or not descricao.strip():
                # Ticket 06: sem descrição, o nome do catálogo; "produto N" só se não estiver nele.
                descricao = (nomes or {}).get(p.get("product_id")) or f"produto {p.get('product_id')}"
            itens.append((descricao, p["quantity"]))
        total = Decimal(str(venda["total"]))
        if not total.is_finite():
            raise _VendaInvalida("total")
        status = venda.get("status")
        if not isinstance(status, str):
            raise _VendaInvalida("status")
    except (KeyError, TypeError, ValueError, ArithmeticError) as erro:
        raise _VendaInvalida(str(erro)) from erro
    pagamentos = venda.get("payments")
    formas = (
        tuple(p["payment_type"] for p in pagamentos)
        if isinstance(pagamentos, list)
        and pagamentos
        and all(isinstance(p, dict) and isinstance(p.get("payment_type"), str) for p in pagamentos)
        else None
    )
    pedido = PedidoDoCliente(
        id=venda["id"],
        data=criada_em.astimezone(FUSO_TERESINA).date(),
        itens=tuple(itens),
        total=total,
        status=status,
        pagamentos=formas,
        criada_em=criada_em,
        entregue=status == "CONFIRM" or bool(venda.get("delivered_at")),
    )
    return criada_em, pedido

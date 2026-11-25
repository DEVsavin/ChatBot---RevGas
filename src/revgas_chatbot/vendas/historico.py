"""Módulo único de histórico e acompanhamento de vendas (somente leitura na API RevGás).

Reutilizado por:
- Devolução / cancelamento / estorno (últimos 7 dias);
- Comando "meus pedidos";
- Acompanhamento "onde está meu pedido?" (`GET /sales/{id}`);
- Recompra ("repetir o último pedido", últimos 365 dias).

Usa sempre a `ApiCadenciada` compartilhada (limite de 1 req/s) e `page_size=100`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping

from revgas_chatbot.api_revgas.api import Relogio, TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.atendimento.atendimento import TransferirAtendente
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.pedido import Endereco, ProdutoCatalogo
from revgas_chatbot.vendas.venda import marca_telefone, reais

MAXIMO_DE_PEDIDOS = 3
MAXIMO_DE_PAGINAS = 20
TAMANHO_DA_PAGINA = 100

STATUS_PARA_CLIENTE: dict[str, str] = {
    "OPEN": "pedido recebido (aguardando atendimento)",
    "ACTIVE": "em andamento (aguardando entregador)",
    "WARNING": "entregador avisado / a caminho",
    "WARN": "entregador avisado / a caminho",
    "CONFIRM": "entregue",
    "CHECK": "conferido e finalizado",
    "CANCEL": "cancelado",
    "IGNORED": "aguardando outro entregador",
    "CANCEL_DELIVERY": "entrega cancelada (aguardando reenvio)",
    "RECEIVED": "recebido pelo entregador",
}


def traduzir_status(status: str) -> str:
    return STATUS_PARA_CLIENTE.get(status.upper(), "em andamento")


@dataclass(frozen=True)
class PedidoDoCliente:
    id: int
    data: date
    itens: tuple[tuple[str, int], ...]  # (descrição, quantidade)
    total: Decimal
    status: str
    pagamentos: tuple[str, ...] | None = None
    criada_em: datetime | None = None
    entregue: bool = False
    itens_ids: tuple[tuple[int, int], ...] = ()  # (product_id, quantidade)
    endereco: Endereco | None = None

    @property
    def cancelado(self) -> bool:
        return self.status == "CANCEL"

    def texto_para_cliente(self) -> str:
        texto = self._descricao()
        return f"{texto} (cancelado)" if self.cancelado else texto

    def texto_com_status(self) -> str:
        return f"{self._descricao()} — {traduzir_status(self.status)}"

    def texto_para_atendente(self) -> str:
        return f"{self._descricao()} (status na API: {self.status})"

    def _descricao(self) -> str:
        itens = ", ".join(f"{quantidade}x {descricao}" for descricao, quantidade in self.itens)
        return f"Pedido {self.id} de {self.data:%d/%m/%Y}: {itens}, total {reais(self.total)}"


@dataclass(frozen=True)
class LogDoPedido:
    status: str
    quando: datetime | None
    responsavel: str | None = None


@dataclass(frozen=True)
class DetalhesDoPedido:
    pedido: PedidoDoCliente
    entregador: str | None = None
    entregue_em: datetime | None = None
    logs: tuple[LogDoPedido, ...] = ()

    def texto_para_cliente(self) -> str:
        linhas = [
            f"Pedido {self.pedido.id} ({self.pedido.data:%d/%m/%Y}):",
            "Itens: " + ", ".join(f"{q}x {d}" for d, q in self.pedido.itens) + f" — total {reais(self.pedido.total)}",
            f"Situação atual: {traduzir_status(self.pedido.status)}.",
        ]
        if self.entregador:
            linhas.append(f"Entregador: {self.entregador}.")
        if self.entregue_em is not None:
            hora = self.entregue_em.astimezone(FUSO_TERESINA).strftime("%d/%m/%Y às %H:%M")
            linhas.append(f"Entregue em: {hora}.")
        if self.logs:
            linhas.append("Histórico:")
            for log in self.logs:
                quando = (
                    log.quando.astimezone(FUSO_TERESINA).strftime("%d/%m %H:%M")
                    if log.quando is not None
                    else ""
                )
                prefixo = f"{quando} — " if quando else ""
                linhas.append(f"- {prefixo}{traduzir_status(log.status)}")
        return "\n".join(linhas)


@dataclass(frozen=True)
class PedidosEncontrados:
    customer_id: int
    nome: str | None
    pedidos: tuple[PedidoDoCliente, ...]


@dataclass(frozen=True)
class NenhumPedido:
    """Cliente não cadastrado ou sem pedido no prazo."""


ResultadoDaBusca = PedidosEncontrados | NenhumPedido | TransferirAtendente


class HistoricoDeVendas:
    def __init__(
        self,
        *,
        api: ApiCadenciada,
        relogio: Relogio,
        cliente_fixo: int | None = None,
        catalogo: Mapping[int, ProdutoCatalogo] | None = None,
    ) -> None:
        if not isinstance(api, ApiCadenciada):
            raise TypeError("HistoricoDeVendas precisa da ApiCadenciada compartilhada (ponto único de montagem)")
        self._api = api
        self._relogio = relogio
        self._cliente_fixo = cliente_fixo
        self._catalogo = dict(catalogo or {})
        self._nomes = {pid: p.nome for pid, p in self._catalogo.items()}

    @property
    def usa_cliente_fixo(self) -> bool:
        return self._cliente_fixo is not None

    def pedidos_do_cliente(
        self, telefone: str, *, prazo_dias: int = 7, limite: int = MAXIMO_DE_PEDIDOS
    ) -> ResultadoDaBusca:
        """Até `limite` pedidos do cliente nos últimos `prazo_dias`, mais recentes primeiro."""
        if self._cliente_fixo is not None:
            return self._pedidos_de(
                self._cliente_fixo, None, prazo_dias=prazo_dias, limite=limite, telefone=telefone
            )
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
        return self._pedidos_de(
            cliente["id"],
            nome if isinstance(nome, str) else None,
            prazo_dias=prazo_dias,
            limite=limite,
        )

    def ultima_venda_repetivel(
        self,
        telefone: str,
        *,
        prazo_dias: int = 365,
        pagamentos_permitidos: set[str] | None = None,
    ) -> PedidoDoCliente | None:
        """Última venda não cancelada do cliente que pode ser repetida (ou None em qualquer falha)."""
        resultado = self.pedidos_do_cliente(telefone, prazo_dias=prazo_dias, limite=10)
        if not isinstance(resultado, PedidosEncontrados):
            return None
        for pedido in resultado.pedidos:
            if pedido.cancelado:
                continue
            if not pedido.itens_ids or len(pedido.itens_ids) != 1:
                continue
            product_id, quantidade = pedido.itens_ids[0]
            if self._catalogo and product_id not in self._catalogo:
                continue
            if quantidade < 1 or pedido.endereco is None:
                continue
            if pagamentos_permitidos is not None:
                if not pedido.pagamentos or len(pedido.pagamentos) != 1:
                    continue
                if pedido.pagamentos[0] not in pagamentos_permitidos:
                    continue
            return pedido
        return None

    def detalhar_pedido(self, venda_id: int) -> DetalhesDoPedido | None:
        """Consulta `GET /sales/{id}` e devolve status, entregador e logs formatados (ou None)."""
        try:
            resposta = self._api.buscar_venda(venda_id)
        except TempoEsgotado:
            return None
        if resposta.status != 200 or not isinstance(resposta.dados, dict):
            return None
        try:
            _, pedido = _venda(resposta.dados, self._nomes)
        except _VendaInvalida:
            return None
        dados = resposta.dados
        entregador = dados.get("deliverer_name")
        entregador = entregador.strip() if isinstance(entregador, str) and entregador.strip() else None
        entregue_em = _parse_datetime(dados.get("delivered_at"))
        logs = _parse_logs(dados.get("logs"))
        return DetalhesDoPedido(pedido=pedido, entregador=entregador, entregue_em=entregue_em, logs=logs)

    def _pedidos_de(
        self,
        customer_id: int,
        nome: str | None,
        *,
        prazo_dias: int,
        limite: int,
        telefone: str | None = None,
    ) -> ResultadoDaBusca:
        hoje = self._relogio.agora().astimezone(FUSO_TERESINA).date()
        inicio = hoje - timedelta(days=prazo_dias)
        vendas = []
        for pagina in range(1, MAXIMO_DE_PAGINAS + 1):
            try:
                resposta = self._api.buscar_vendas(
                    customer_id=customer_id,
                    channel=None,
                    de=inicio,
                    ate=hoje + timedelta(days=1),
                    pagina=pagina,
                    page_size=TAMANHO_DA_PAGINA,
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
            tag_tel = marca_telefone(telefone) if telefone else None
            vendas = [
                v
                for v in vendas
                if "[chatbot:" in str(v.get("observation") or "")
                and (tag_tel is None or tag_tel in str(v.get("observation") or ""))
            ]
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
        do_cliente_no_prazo.sort(key=lambda par: par[0], reverse=True)
        return PedidosEncontrados(
            customer_id=customer_id,
            nome=nome,
            pedidos=tuple(pedido for _, pedido in do_cliente_no_prazo[:limite]),
        )


class _VendaInvalida(Exception):
    """Venda da API fora do formato esperado."""


def _inteiro(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _lista(dados: object) -> list[dict] | None:
    lista = dados.get("data") if isinstance(dados, dict) else None
    if not isinstance(lista, list) or not all(isinstance(item, dict) for item in lista):
        return None
    return lista


def _tem_proxima(dados: dict) -> bool:
    paginacao = dados.get("pagination")
    return isinstance(paginacao, dict) and paginacao.get("has_next") is True


def _parse_datetime(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        dt = datetime.fromisoformat(valor)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=FUSO_TERESINA)
    except ValueError:
        return None


def _parse_logs(bruto: object) -> tuple[LogDoPedido, ...]:
    if not isinstance(bruto, list):
        return ()
    logs = []
    for item in bruto:
        if not isinstance(item, dict) or not isinstance(item.get("status"), str):
            continue
        quando = _parse_datetime(item.get("created_at"))
        resp = item.get("user_name")
        logs.append(LogDoPedido(status=item["status"], quando=quando, responsavel=resp if isinstance(resp, str) else None))
    return tuple(logs)


def _endereco_da_venda(venda: dict) -> Endereco | None:
    campos = ("location", "number", "neighborhood", "city", "state_federation")
    if not all(isinstance(venda.get(c), str) and venda[c].strip() for c in campos):
        return None
    return Endereco(
        logradouro=venda["location"].strip(),
        numero=venda["number"].strip(),
        bairro=venda["neighborhood"].strip(),
        cidade=venda["city"].strip(),
        estado=venda["state_federation"].strip(),
        complemento=venda["complement"].strip() if isinstance(venda.get("complement"), str) and venda["complement"].strip() else None,
        referencia=venda["landmark"].strip() if isinstance(venda.get("landmark"), str) and venda["landmark"].strip() else None,
    )


def _venda(venda: dict, nomes: Mapping[int, str] | None = None) -> tuple[datetime, PedidoDoCliente]:
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
        itens_ids = []
        for p in produtos:
            if not isinstance(p, dict) or not _inteiro(p.get("quantity")) or p["quantity"] < 1:
                raise _VendaInvalida("quantidade")
            descricao = p.get("product_description")
            pid = p.get("product_id")
            if not isinstance(descricao, str) or not descricao.strip():
                descricao = (nomes or {}).get(pid) or f"produto {pid}"
            itens.append((descricao, p["quantity"]))
            if _inteiro(pid):
                itens_ids.append((pid, p["quantity"]))
        total = Decimal(str(venda["total"]))
        if not total.is_finite():
            raise _VendaInvalida("total")
        status = venda.get("status")
        if not isinstance(status, str):
            raise _VendaInvalida("status")
    except (KeyError, TypeError, ValueError, ArithmeticError) as erro:
        raise _VendaInvalida(str(erro)) from erro

    pagamentos = venda.get("payments")
    formas: tuple[str, ...] | None = None
    if isinstance(pagamentos, list) and pagamentos:
        extraidas = []
        for p in pagamentos:
            tipo = (p.get("payment_type") or p.get("type")) if isinstance(p, dict) else None
            if isinstance(tipo, str) and tipo.strip():
                extraidas.append(tipo.strip())
            else:
                extraidas = []
                break
        if extraidas:
            formas = tuple(extraidas)

    pedido = PedidoDoCliente(
        id=venda["id"],
        data=criada_em.astimezone(FUSO_TERESINA).date(),
        itens=tuple(itens),
        total=total,
        status=status,
        pagamentos=formas,
        criada_em=criada_em,
        entregue=status == "CONFIRM" or bool(venda.get("delivered_at")),
        itens_ids=tuple(itens_ids),
        endereco=_endereco_da_venda(venda),
    )
    return criada_em, pedido

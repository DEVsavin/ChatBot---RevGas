"""Devolução: achar os pedidos do cliente e registrar a solicitação na própria venda no ERP RevGás.

Usa apenas a API RevGás (sem banco/arquivo local):
- `GET /v1/sales` para listar pedidos do cliente;
- `GET /v1/sales/{id}` + `PUT /v1/sales/{id}` para anotar `[CHATBOT][TIPO] ...` na `observation`
  da venda (preservando a observação original, troco e marca `[chatbot:<id>]`).
O chatbot nunca cancela nem estorna sozinho; quem decide e executa é a equipe no painel do ERP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from revgas_chatbot.api_revgas.api import Relogio, TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.atendimento.atendimento import mascarar
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.historico import (
    MAXIMO_DE_PAGINAS,
    MAXIMO_DE_PEDIDOS,
    HistoricoDeVendas,
    NenhumPedido,
    PedidoDoCliente,
    PedidosEncontrados,
    ResultadoDaBusca,
    _inteiro,
)
from revgas_chatbot.vendas.pedido import Problema, ProdutoCatalogo

__all__ = [
    "MAXIMO_DE_PAGINAS",
    "MAXIMO_DE_PEDIDOS",
    "MAX_TEXTO",
    "PREFERENCIAS",
    "REGRAS",
    "Devolucao",
    "NenhumPedido",
    "PedidoDoCliente",
    "PedidosEncontrados",
    "RegraDoTipo",
    "ResultadoDaBusca",
    "ResultadoDaSolicitacao",
    "SolicitacaoAberta",
    "SolicitacaoInvalida",
]


@dataclass(frozen=True)
class SolicitacaoAberta:
    protocolo: str
    mensagem_para_cliente: str
    entregador_pode_ter_saido: bool = False


@dataclass(frozen=True)
class SolicitacaoInvalida:
    """Algum dado informado pelo cliente está faltando ou errado: ele corrige."""

    problemas: tuple[Problema, ...]

    def campos(self) -> list[str]:
        return [p.campo for p in self.problemas]


ResultadoDaSolicitacao = SolicitacaoAberta | SolicitacaoInvalida


@dataclass(frozen=True)
class RegraDoTipo:
    motivos: dict[str, str]
    preferencia: str  # "obrigatoria", "opcional" ou "proibida"


_SO_TEXTO = {"OUTRO": "descrito pelo cliente"}

REGRAS = {
    "CANCELAMENTO": RegraDoTipo(
        motivos={"DESISTI": "desisti", "PEDI_ERRADO": "pedi errado", "OUTRO": "outro"},
        preferencia="opcional",
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
    "PROBLEMA_PRODUTO": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
    "ALTERACAO": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
    "VASILHAME": RegraDoTipo(motivos=_SO_TEXTO, preferencia="proibida"),
}

MAX_TEXTO = 200

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
        prazo_dias: int = 7,
        cliente_fixo: int | None = None,
        catalogo: Mapping[int, ProdutoCatalogo] | None = None,
    ) -> None:
        if not isinstance(api, ApiCadenciada):
            raise TypeError("Devolucao precisa da ApiCadenciada compartilhada (ponto único de montagem)")
        self._api = api
        self._relogio = relogio
        self._prazo_dias = prazo_dias
        self._cliente_fixo = cliente_fixo
        self._historico = HistoricoDeVendas(
            api=api, relogio=relogio, cliente_fixo=cliente_fixo, catalogo=catalogo
        )

    def _entregador_pode_ter_saido(self, tipo: str, pedido: PedidoDoCliente | None) -> bool:
        if tipo != "CANCELAMENTO" or pedido is None or pedido.criada_em is None:
            return False
        if pedido.entregue or pedido.cancelado:
            return False
        return self._relogio.agora() - pedido.criada_em <= timedelta(hours=2)

    @property
    def usa_cliente_fixo(self) -> bool:
        return self._cliente_fixo is not None

    def pedidos_do_cliente(self, telefone: str) -> ResultadoDaBusca:
        return self._historico.pedidos_do_cliente(
            telefone, prazo_dias=self._prazo_dias, limite=MAXIMO_DE_PEDIDOS
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
        nota: str | None = None,
    ) -> ResultadoDaSolicitacao:
        """Registra a solicitação na `observation` da venda no ERP (`PUT /sales/{id}`).

        Sem arquivo local: se houver pedido escolhido, anota diretamente na venda via API;
        se não houver pedido vinculado, orienta o cliente a falar com a revenda.
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

        recente = self._entregador_pode_ter_saido(tipo, pedido)
        if pedido is None:
            return SolicitacaoAberta(
                entregador_pode_ter_saido=False,
                protocolo="ATENDIMENTO",
                mensagem_para_cliente=(
                    "Não consegui vincular sua solicitação a um pedido no sistema. "
                    "Entre em contato diretamente com a equipe da revenda para atendimento."
                ),
            )

        partes = [f"[CHATBOT][{tipo}]", f"Motivo: {regra.motivos[motivo]}."]
        if texto:
            partes.append(f"Relato: {mascarar(texto)}.")
        if recente and pedido.criada_em is not None:
            hora = pedido.criada_em.astimezone(FUSO_TERESINA).strftime("%H:%M")
            partes.append(f"Pedido feito às {hora} (entregador pode ter saído).")
        if preferencia is not None:
            partes.append(f"Preferência: {PREFERENCIAS[preferencia]}.")
        if nota:
            partes.append(nota)
        anotacao = " ".join(partes)

        anotado = self._anotar_na_venda(pedido.id, anotacao)
        if not anotado:
            raise RuntimeError("não foi possível registrar a solicitação na venda via API RevGás")

        return SolicitacaoAberta(
            entregador_pode_ter_saido=recente,
            protocolo=f"ERP-{pedido.id}",
            mensagem_para_cliente=(
                f"Sua solicitação foi registrada no pedido {pedido.id} (protocolo ERP-{pedido.id}). "
                "Nossa equipe vai analisar pelo painel."
            ),
        )

    def _anotar_na_venda(self, venda_id: int, anotacao: str) -> bool:
        try:
            atual = self._api.buscar_venda(venda_id)
        except TempoEsgotado:
            return False
        if atual.status != 200 or not isinstance(atual.dados, dict):
            return False
        obs_atual = str(atual.dados.get("observation") or "").strip()
        nova_obs = f"{obs_atual} | {anotacao}" if obs_atual else anotacao
        corpo = montar_corpo_atualizacao_obs(atual.dados, nova_obs)
        try:
            resposta = self._api.atualizar_venda(venda_id, corpo)
        except TempoEsgotado:
            return False
        return 200 <= resposta.status < 300


def montar_corpo_atualizacao_obs(venda_atual: dict, nova_obs: str) -> dict:
    """Monta o corpo completo do `PUT /v1/sales/{id}` preservando os campos da venda atual.

    A API RevGás exige os campos obrigatórios (ex.: `channel`, `establishment_id`,
    `products`, `payments`, endereço) no `PUT /sales/{id}`, senão responde 400
    (`sale channel cannot be null`).
    """
    corpo: dict = {
        "observation": nova_obs,
        "origin": venda_atual.get("origin") or "INTEGRATION",
        "option": venda_atual.get("option") or "NORMAL",
    }
    for campo in (
        "establishment_id",
        "customer_id",
        "deliverer_id",
        "channel",
        "location",
        "number",
        "neighborhood",
        "city",
        "state_federation",
        "complement",
        "landmark",
        "lat",
        "lon",
    ):
        valor = venda_atual.get(campo)
        if valor is not None:
            corpo[campo] = valor
    if isinstance(venda_atual.get("products"), list):
        corpo["products"] = [
            {
                "product_id": p["product_id"],
                "quantity": p["quantity"],
                "value": p["value"],
            }
            for p in venda_atual["products"]
            if isinstance(p, dict) and "product_id" in p and "quantity" in p and "value" in p
        ]
    if isinstance(venda_atual.get("payments"), list):
        pagamentos = []
        for p in venda_atual["payments"]:
            if not isinstance(p, dict):
                continue
            tipo = p.get("payment_type") or p.get("type")
            if tipo and "value" in p:
                item: dict = {"payment_type": tipo, "value": p["value"]}
                if p.get("serial"):
                    item["serial"] = p["serial"]
                pagamentos.append(item)
        if pagamentos:
            corpo["payments"] = pagamentos
    return corpo

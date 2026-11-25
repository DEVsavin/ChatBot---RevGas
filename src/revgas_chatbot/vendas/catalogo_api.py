"""Catálogo de produtos e preços carregado direto da API RevGás.

Usa `GET /v1/products` (produtos ativos) + `GET /v1/deals` (preços via acordos),
respeitando o limite global de 1 req/s pela `ApiCadenciada` compartilhada.
"""

from __future__ import annotations

import logging
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from revgas_chatbot.api_revgas.api import TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.vendas.pedido import ProdutoCatalogo

_log = logging.getLogger(__name__)
MAXIMO_DE_PAGINAS = 20
TAMANHO_DA_PAGINA = 100

_TERMOS_GAS = ("glp", "botij", "gas", "regulador", "mangueira", "vasilhame")
_TERMOS_EXCLUIDOS = ("agua", "gelo", "carvao", "vela", "bomba")


def _eh_item_de_gas(descricao: str) -> bool:
    norm = unicodedata.normalize("NFKD", descricao).encode("ascii", "ignore").decode().casefold()
    if any(exc in norm for exc in _TERMOS_EXCLUIDOS):
        return False
    return any(termo in norm for termo in _TERMOS_GAS)


def carregar_catalogo_da_api(
    api: ApiCadenciada, establishment_id: int
) -> dict[int, ProdutoCatalogo] | None:
    """Busca produtos ativos e preços em `/products` e `/deals`.

    Retorna None em caso de erro HTTP, timeout ou catálogo vazio (para permitir
    fallback seguro quando o token ainda não tem permissão nesses endpoints).
    """
    produtos = _listar_tudo(
        lambda pagina: api.buscar_produtos(
            establishment_id=establishment_id, pagina=pagina, page_size=TAMANHO_DA_PAGINA
        )
    )
    if not produtos:
        return None

    acordos = _listar_tudo(
        lambda pagina: api.buscar_acordos(
            establishment_id=establishment_id, pagina=pagina, page_size=TAMANHO_DA_PAGINA
        )
    )
    if acordos is None:
        return None

    precos = _melhores_precos(acordos)
    catalogo: dict[int, ProdutoCatalogo] = {}
    for item in produtos:
        pid = item.get("id")
        desc = item.get("description")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if not isinstance(desc, str) or not desc.strip():
            continue
        if not _eh_item_de_gas(desc):
            continue
        estoque = item.get("stock_quantity")
        if isinstance(estoque, (int, float)) and not isinstance(estoque, bool) and estoque <= 0:
            continue
        preco = precos.get(pid)
        if preco is not None and preco >= Decimal("1.00"):
            catalogo[pid] = ProdutoCatalogo(nome=desc.strip(), preco=preco)

    return catalogo or None


def _listar_tudo(buscar_pagina) -> list[dict[str, Any]] | None:
    acumulado: list[dict[str, Any]] = []
    for pagina in range(1, MAXIMO_DE_PAGINAS + 1):
        try:
            resposta = buscar_pagina(pagina)
        except (TempoEsgotado, AttributeError):
            return None
        if resposta.status != 200 or not isinstance(resposta.dados, dict):
            _log.warning("API RevGás retornou HTTP %s ao carregar catálogo", getattr(resposta, "status", "?"))
            return None
        lista = resposta.dados.get("data")
        if not isinstance(lista, list) or not all(isinstance(i, dict) for i in lista):
            return None
        acumulado.extend(lista)
        paginacao = resposta.dados.get("pagination")
        if not (isinstance(paginacao, dict) and paginacao.get("has_next") is True):
            break
    return acumulado


def _decimal_positivo(valor: Any) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        d = Decimal(str(valor).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d.quantize(Decimal("0.01"))


def _melhores_precos(acordos: list[dict[str, Any]]) -> dict[int, Decimal]:
    """Escolhe o melhor preço por produto (prioridade: is_default=True > type=PAYMENT_TYPE > demais)."""
    escolhidos: dict[int, tuple[int, Decimal]] = {}
    for acordo in acordos:
        pid = acordo.get("product_id")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        preco = _decimal_positivo(acordo.get("suggested_value")) or _decimal_positivo(acordo.get("minimum_value"))
        if preco is None:
            continue
        prioridade = 2 if acordo.get("is_default") is True else (1 if acordo.get("type") == "PAYMENT_TYPE" else 0)
        atual = escolhidos.get(pid)
        if atual is None or prioridade > atual[0]:
            escolhidos[pid] = (prioridade, preco)
    return {pid: preco for pid, (_, preco) in escolhidos.items()}

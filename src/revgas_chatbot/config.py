"""Leitura dos arquivos de configuração em `config/`.

- `catalogo.json`: o que o chatbot oferece, com o preço (a API da RevGás
  não informa preço). Preço vai como texto ("110.00") para virar `Decimal`
  exato, nunca `float`.
- `pagamentos.json`: formas de pagamento aceitas (valores de `payment_type`).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from revgas_chatbot.pedido import ProdutoCatalogo, valor_em_reais

# Valores de `payment_type` aceitos pela API (doc do POST /sales).
TIPOS_DE_PAGAMENTO_DA_API = frozenset(
    {
        "MONEY", "WALLET", "BANKDRAFT", "FETLOCK", "VISA", "MASTER", "HIPER",
        "CREDISHOP", "VALEGAS", "ELO", "CARTAO_DE_CREDITO", "CARTAO_OUTRO",
        "CREDITO_CLIENTE", "CARTAO_DE_DEBITO", "CARTAO_REVGAS", "PIX",
        "TRANSFERENCIA_BANCARIA",
    }
)


def carregar_catalogo(caminho: Path) -> dict[int, ProdutoCatalogo]:
    catalogo: dict[int, ProdutoCatalogo] = {}
    for entrada in json.loads(Path(caminho).read_text(encoding="utf-8")):
        product_id = entrada["product_id"]
        # bool é subclasse de int em Python: True passaria como 1.
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            raise ValueError(f"product_id deve ser número inteiro: {product_id!r}")
        if product_id in catalogo:
            raise ValueError(f"product_id repetido no catálogo: {product_id}")
        bruto = entrada["preco"]
        if not isinstance(bruto, str):
            raise ValueError(f"preço do produto {product_id} deve ser texto, ex.: \"110.00\"")
        try:
            preco = Decimal(bruto)
        except InvalidOperation as erro:
            raise ValueError(f"preço inválido no produto {product_id}: {bruto!r}") from erro
        if not valor_em_reais(preco):
            raise ValueError(f"preço inválido no produto {product_id}: {bruto!r}")
        catalogo[product_id] = ProdutoCatalogo(nome=entrada["nome"], preco=preco)
    return catalogo


def carregar_pagamentos(caminho: Path) -> set[str]:
    tipos = set(json.loads(Path(caminho).read_text(encoding="utf-8")))
    desconhecidos = tipos - TIPOS_DE_PAGAMENTO_DA_API
    if desconhecidos:
        raise ValueError(f"formas de pagamento que a API não conhece: {sorted(desconhecidos)}")
    return tipos

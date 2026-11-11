"""Montagem do corpo do `POST /sales` (M2).

Função pura: recebe um Pedido já validado e devolve o dicionário que vai
para a API. Não faz nenhuma chamada de rede.

Decisões (ver `docs/revgas-api-vendas.md`):
- Uma linha por unidade, sempre com `quantity: 1`. Assim `value` é o preço
  de uma unidade, seja ele lido pela API como unitário ou como total da linha.
- Não enviamos `origin`, `option`, `broadcast`, `lat`, `lon` nem
  `deliverer_id`: os valores corretos ainda não foram confirmados.
- Cartão: só a forma de pagamento; nada de bandeira ou dado do cartão.
- `observation` termina com a marca `[chatbot:<pedido_id>]`, usada para
  achar a venda se o envio der timeout.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from revgas_chatbot.estados import nome_do_estado
from revgas_chatbot.pedido import DINHEIRO, Pedido, ProdutoCatalogo

CANAL = "WHATSAPP"
CENTAVOS = Decimal("0.01")


def marca(pedido_id: str) -> str:
    return f"[chatbot:{pedido_id}]"


def reais(valor: Decimal) -> str:
    """Formata no padrão brasileiro: Decimal("1200") -> "R$ 1.200,00"."""
    texto = f"{valor.quantize(CENTAVOS):,.2f}"  # "1,200.00"
    return "R$ " + texto.replace(",", "_").replace(".", ",").replace("_", ".")


def _observation(pedido: Pedido) -> str:
    partes: list[str] = []
    if pedido.observacao and pedido.observacao.strip():
        partes.append(pedido.observacao.strip())
    em_dinheiro = sum((p.valor for p in pedido.pagamentos if p.tipo == DINHEIRO), Decimal("0"))
    # Troco "para" exatamente o valor em dinheiro = não precisa de troco.
    if em_dinheiro > 0 and pedido.troco_para is not None and pedido.troco_para > em_dinheiro:
        partes.append(f"Troco para {reais(pedido.troco_para)}.")
    partes.append(marca(pedido.pedido_id))
    return " ".join(partes)


def montar_corpo_venda(
    pedido: Pedido,
    *,
    customer_id: int,
    catalogo: dict[int, ProdutoCatalogo],
    establishment_id: int,
) -> dict[str, Any]:
    """Monta o corpo do POST /sales. O pedido deve ter passado por `validar`."""
    linhas = []
    for item in pedido.itens:
        if item.product_id not in catalogo:
            raise ValueError(f"produto fora do catálogo: {item.product_id}")
        preco = catalogo[item.product_id].preco.quantize(CENTAVOS)
        # Um dicionário novo por unidade (`[d] * n` repetiria o mesmo objeto).
        linhas += [
            {"product_id": item.product_id, "quantity": 1, "value": preco}
            for _ in range(item.quantidade)
        ]

    endereco = pedido.endereco
    if endereco is None:
        raise ValueError("pedido sem endereço: valide antes de montar a venda")

    corpo: dict[str, Any] = {
        "establishment_id": establishment_id,
        "customer_id": customer_id,
        "channel": CANAL,
        "products": linhas,
        "payments": [
            {"payment_type": p.tipo, "value": p.valor.quantize(CENTAVOS)} for p in pedido.pagamentos
        ],
        "location": endereco.logradouro.strip(),
        "number": endereco.numero.strip(),
        "neighborhood": endereco.bairro.strip(),
        "city": endereco.cidade.strip(),
        # Por extenso ("Piauí"), como nas vendas observadas.
        "state_federation": nome_do_estado(endereco.estado) or endereco.estado.strip(),
        "observation": _observation(pedido),
    }
    if endereco.complemento and endereco.complemento.strip():
        corpo["complement"] = endereco.complemento.strip()
    if endereco.referencia and endereco.referencia.strip():
        corpo["landmark"] = endereco.referencia.strip()
    return corpo


def corpo_para_json(corpo: dict[str, Any]) -> str:
    """Serializa o corpo. Dinheiro sai como número JSON (110.0), nunca como texto."""

    def converter(valor: Any) -> Any:
        if isinstance(valor, Decimal):
            # Decimal já arredondado a 2 casas: o float tem os mesmos dígitos.
            return float(valor)
        raise TypeError(f"tipo não serializável: {type(valor).__name__}")

    return json.dumps(corpo, default=converter, ensure_ascii=False)

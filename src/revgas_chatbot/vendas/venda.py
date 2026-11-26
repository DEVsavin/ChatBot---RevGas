"""Montagem do corpo do `POST /sales` (M2).

Função pura: recebe um Pedido já validado e devolve o dicionário que vai
para a API. Não faz nenhuma chamada de rede.

Decisões (ver `docs/revgas-api-vendas.md`):
- Uma linha por produto, com `quantity` N e `value` = preço UNITÁRIO. A API
  não soma linhas repetidas do mesmo produto, e `value` é unitário
  (confirmado na venda de teste 30811219: quantity 2 x 110,00 = 220,00).
- `origin: INTEGRATION` e `option: NORMAL`: sem `option` a API recusa com
  "Dados da Entrega da Venda estão incompletos!". NORMAL não confirma a
  venda sozinha (INDOOR/OUTDOOR confirmam).
- Não enviamos `broadcast`, `lat`, `lon` nem `deliverer_id`: os valores
  corretos ainda não foram confirmados (a central atribui o entregador).
- Cartão: só a forma de pagamento; nada de bandeira ou dado do cartão.
- `observation` termina com a marca `[chatbot:<pedido_id>]`, usada para
  achar a venda se o envio der timeout.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from revgas_chatbot.conversa.entrada import com_nono_digito
from revgas_chatbot.conversa.estados import nome_do_estado
from revgas_chatbot.vendas.pedido import DINHEIRO, Endereco, Pedido, ProdutoCatalogo

CANAL = "WHATSAPP"
ORIGEM = "INTEGRATION"  # venda criada por integração (central de pedidos)
OPCAO = "NORMAL"  # não confirma a venda sozinha
CENTAVOS = Decimal("0.01")


def marca(pedido_id: str) -> str:
    return f"[chatbot:{pedido_id}]"


def marca_telefone(telefone: str) -> str:
    """Identificador anônimo (hash curto) do telefone para separar histórico no modo cliente fixo."""
    normalizado = com_nono_digito(telefone.strip())
    return f"[tel:{hashlib.sha256(normalizado.encode()).hexdigest()[:8]}]"


def variantes_telefone(telefone: str) -> tuple[str, ...]:
    """Devolve o telefone e a variante com/sem 9º dígito para busca no ERP."""
    digitos = "".join(c for c in telefone if c.isdigit())
    if len(digitos) == 13 and digitos.startswith("55") and digitos[4] == "9":
        return (digitos, digitos[:4] + digitos[5:])
    if len(digitos) == 12 and digitos.startswith("55"):
        return (digitos, digitos[:4] + "9" + digitos[4:])
    return (digitos or telefone,)


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
    if pedido.pagamento_pix == "AGORA":
        partes.append("PIX: pagar agora.")
    elif pedido.pagamento_pix == "ENTREGA":
        partes.append("PIX: pagar na entrega.")
    partes.append(marca(pedido.pedido_id))
    return " ".join(partes)


def complemento_com_quadra(endereco: Endereco) -> str | None:
    """Combina quadra (se houver) e complemento no campo `complement` da API RevGás."""
    partes: list[str] = []
    if endereco.quadra and endereco.quadra.strip():
        quadra = endereco.quadra.strip()
        if not quadra.lower().startswith(("quadra", "qd")):
            quadra = f"Quadra {quadra}"
        partes.append(quadra)
    if endereco.complemento and endereco.complemento.strip():
        partes.append(endereco.complemento.strip())
    return ", ".join(partes) if partes else None


def montar_corpo_venda(
    pedido: Pedido,
    *,
    customer_id: int,
    catalogo: dict[int, ProdutoCatalogo],
    establishment_id: int,
) -> dict[str, Any]:
    """Monta o corpo do POST /sales. O pedido deve ter passado por `validar`."""
    quantidades: dict[int, int] = {}  # ordem do pedido; o mesmo produto vira UMA linha
    for item in pedido.itens:
        if item.product_id not in catalogo:
            raise ValueError(f"produto fora do catálogo: {item.product_id}")
        quantidades[item.product_id] = quantidades.get(item.product_id, 0) + item.quantidade
    linhas = [
        {"product_id": produto, "quantity": quantidade, "value": catalogo[produto].preco.quantize(CENTAVOS)}
        for produto, quantidade in quantidades.items()
    ]

    endereco = pedido.endereco
    if endereco is None:
        raise ValueError("pedido sem endereço: valide antes de montar a venda")

    pagamentos = []
    for p in pedido.pagamentos:
        item_pag: dict[str, Any] = {"payment_type": p.tipo, "value": p.valor.quantize(CENTAVOS)}
        if p.serial and p.serial.strip():
            item_pag["serial"] = p.serial.strip()
        pagamentos.append(item_pag)

    corpo: dict[str, Any] = {
        "establishment_id": establishment_id,
        "customer_id": customer_id,
        "channel": CANAL,
        "origin": ORIGEM,
        "option": OPCAO,
        "products": linhas,
        "payments": pagamentos,
        "location": endereco.logradouro.strip(),
        "number": endereco.numero.strip(),
        "neighborhood": endereco.bairro.strip(),
        "city": endereco.cidade.strip(),
        # Por extenso ("Piauí"), como nas vendas observadas.
        "state_federation": nome_do_estado(endereco.estado) or endereco.estado.strip(),
        "observation": _observation(pedido),
    }
    complemento = complemento_com_quadra(endereco)
    if complemento:
        corpo["complement"] = complemento
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

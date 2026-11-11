"""Pedido e validação (M1 da spec `.scratch/criar-venda/spec.md`, seção 3.1).

Um Pedido é o que o chatbot coletou na conversa. Antes de virar venda na
API da RevGás, ele passa por `validar`, que devolve uma de três respostas:

- `Valido(total)`: pode seguir para a API.
- `Invalido(problemas)`: algum dado do cliente está faltando ou errado;
  o chatbot pede a correção.
- `ForaDoCatalogo(product_ids)`: o pedido tem produto que o chatbot nem
  deveria ter oferecido. É erro de configuração, não do cliente, e leva
  a TRANSFERIR_ATENDENTE.

Dinheiro é sempre `Decimal`, nunca `float` (0.1 + 0.2 != 0.3 em float).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

# 55 (Brasil) + DDD (2 dígitos) + número (8 ou 9 dígitos) = 12 ou 13 dígitos.
TELEFONE = re.compile(r"55\d{10,11}")

DINHEIRO = "MONEY"


@dataclass(frozen=True)
class ProdutoCatalogo:
    nome: str
    preco: Decimal


@dataclass(frozen=True)
class Item:
    product_id: int
    quantidade: int


@dataclass(frozen=True)
class Pagamento:
    tipo: str  # payment_type da API, ex.: "PIX", "MONEY"
    valor: Decimal


@dataclass(frozen=True)
class Endereco:
    logradouro: str
    numero: str
    bairro: str
    cidade: str
    estado: str  # 🟡 nas vendas observadas vem por extenso ("Piauí"), não "PI"
    complemento: str | None = None
    referencia: str | None = None


@dataclass(frozen=True)
class Pedido:
    pedido_id: str
    telefone: str
    nome: str
    itens: tuple[Item, ...]
    endereco: Endereco | None
    pagamentos: tuple[Pagamento, ...]
    troco_para: Decimal | None = None  # só com pagamento em dinheiro
    observacao: str | None = None


@dataclass(frozen=True)
class Problema:
    campo: str
    mensagem: str


@dataclass(frozen=True)
class Valido:
    total: Decimal


@dataclass(frozen=True)
class Invalido:
    problemas: tuple[Problema, ...]

    def campos(self) -> list[str]:
        return [p.campo for p in self.problemas]


@dataclass(frozen=True)
class ForaDoCatalogo:
    product_ids: tuple[int, ...]


Resultado = Valido | Invalido | ForaDoCatalogo


def valor_em_reais(valor: Decimal) -> bool:
    """Valor em dinheiro aceitável: número finito, > 0 e com no máximo 2 casas.

    Rejeita NaN e infinito (Decimal("nan") <= 0 lança InvalidOperation).
    """
    return valor.is_finite() and valor > 0 and valor.as_tuple().exponent >= -2


def validar(
    pedido: Pedido,
    catalogo: dict[int, ProdutoCatalogo],
    pagamentos_permitidos: set[str],
) -> Resultado:
    # 1. Produto fora do catálogo vem primeiro: sem preço não dá para
    #    calcular o total nem conferir os pagamentos.
    fora = tuple(i.product_id for i in pedido.itens if i.product_id not in catalogo)
    if fora:
        return ForaDoCatalogo(product_ids=fora)

    problemas: list[Problema] = []

    def problema(campo: str, mensagem: str) -> None:
        problemas.append(Problema(campo, mensagem))

    # 2. Identificação e cliente
    if not pedido.pedido_id.strip():
        # Sem pedido_id a marca de reconciliação viraria "[chatbot:]".
        problema("pedido_id", "pedido sem identificador")
    if not pedido.nome.strip():
        problema("nome", "informe o nome")
    if not TELEFONE.fullmatch(pedido.telefone):
        problema("telefone", "telefone deve ter 55 + DDD + número, só dígitos")

    # 3. Endereço (obrigatório para o chatbot, mesmo opcional na API: RN2)
    if pedido.endereco is None:
        problema("endereco", "informe o endereço de entrega")
    else:
        for campo in ("logradouro", "numero", "bairro", "cidade", "estado"):
            if not getattr(pedido.endereco, campo).strip():
                problema(f"endereco.{campo}", f"informe o campo {campo} do endereço")

    # 4. Itens
    if not pedido.itens:
        problema("itens", "o pedido precisa de pelo menos um produto")
    if any(i.quantidade < 1 for i in pedido.itens):
        problema("itens", "a quantidade de cada produto deve ser pelo menos 1")
    total = sum(
        (catalogo[i.product_id].preco * i.quantidade for i in pedido.itens),
        Decimal("0"),
    )

    # 5. Pagamentos
    if not pedido.pagamentos:
        problema("pagamentos", "informe a forma de pagamento")
    valores_ok = True
    for p in pedido.pagamentos:
        if p.tipo not in pagamentos_permitidos:
            problema("pagamentos", f"forma de pagamento não aceita: {p.tipo}")
        if not valor_em_reais(p.valor):
            valores_ok = False
            problema("pagamentos", "o valor de cada pagamento deve ser maior que zero, com até 2 casas")
    # Só soma se todos os valores forem números válidos (NaN quebraria a conta).
    if pedido.pagamentos and valores_ok:
        if sum((p.valor for p in pedido.pagamentos), Decimal("0")) != total:
            problema("pagamentos", "a soma dos pagamentos deve ser igual ao total do pedido")

    # 6. Troco: só com dinheiro, e para um valor >= ao que é pago em dinheiro
    if pedido.troco_para is not None:
        em_dinheiro = sum(
            (p.valor for p in pedido.pagamentos if p.tipo == DINHEIRO and valor_em_reais(p.valor)),
            Decimal("0"),
        )
        if not valor_em_reais(pedido.troco_para):
            problema("troco_para", "o troco deve ser um valor maior que zero, com até 2 casas")
        elif em_dinheiro == 0:
            problema("troco_para", "troco só existe para pagamento em dinheiro")
        elif pedido.troco_para < em_dinheiro:
            problema("troco_para", "o troco deve ser para um valor maior ou igual ao pago em dinheiro")

    if problemas:
        return Invalido(tuple(problemas))
    return Valido(total=total)

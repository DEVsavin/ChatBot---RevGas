"""Interpretação de texto livre (C2): a porta para a IA e a validação do que ela diz.

A IA só ENTENDE: devolve uma `Interpretacao` (intenção + dados do pedido).
Quem decide o que vale é `validar_interpretacao`, com as mesmas regras da
conversa: o que não passa é descartado e perguntado pelo roteiro.
A IA nunca registra venda, nunca dá preço e nunca responde ao cliente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from revgas_chatbot.conversa.entrada import normalizar, numero_do_endereco, opcao, valor_em_reais
from revgas_chatbot.conversa.estados import NOMES_POR_SIGLA, nome_do_estado
from revgas_chatbot.vendas.pedido import Endereco, ProdutoCatalogo

# PERGUNTA: o cliente perguntou algo ou disse que não sabe (A1): não é um dado.
INTENCOES = frozenset({"PEDIR", "CANCELAR_OU_DEVOLVER", "ATENDENTE", "VAZAMENTO", "SAIR", "PERGUNTA", "OUTRA"})
MAX_QUANTIDADE = 10
MAX_TEXTO = 100
DINHEIRO = "MONEY"


@dataclass(frozen=True)
class Contexto:
    """O que a IA pode saber além do texto (C2-4: nada do cadastro do cliente)."""

    passo: str
    produtos: tuple[str, ...]
    pagamentos: tuple[str, ...]


@dataclass(frozen=True)
class Interpretacao:
    """O que a IA disse, ainda SEM validar (os tipos podem vir errados)."""

    intencao: str
    produto: Any = None
    quantidade: Any = None
    endereco: Any = None  # dict com rua, numero, bairro, cidade, estado, complemento, referencia
    pagamento: Any = None
    troco: Any = None


class Interpretador(Protocol):
    def interpretar(self, texto: str, contexto: Contexto) -> Interpretacao | None:
        """Nunca lança: sem rede, sem chave, erro ou demora → None."""
        ...


@dataclass(frozen=True)
class PedidoInterpretado:
    """Só o que passou na validação."""

    intencao: str
    product_id: int | None = None
    quantidade: int | None = None
    endereco: Endereco | None = None  # só se rua, número, bairro, cidade e estado forem válidos
    campos_do_endereco: dict[str, str] = field(default_factory=dict)  # os campos válidos, mesmo incompleto
    pagamento: str | None = None  # tipo da API (ex.: "PIX")
    troco: Decimal | None = None


def validar_interpretacao(
    interpretacao: Interpretacao,
    catalogo: dict[int, ProdutoCatalogo],
    formas: dict[str, str],  # tipo da API -> rótulo mostrado ao cliente
    *,
    texto: str | None = None,  # o que o cliente digitou (para conferir o endereço, I5)
) -> PedidoInterpretado:
    produtos = list(catalogo.items())
    indice = opcao(interpretacao.produto, [p.nome for _, p in produtos]) if _texto(interpretacao.produto) else None
    product_id = produtos[indice][0] if indice is not None else None

    tipos = list(formas)
    escolha = opcao(interpretacao.pagamento, list(formas.values())) if _texto(interpretacao.pagamento) else None
    pagamento = tipos[escolha] if escolha is not None else None

    quantidade = _quantidade(interpretacao.quantidade)
    campos = _campos_do_endereco(interpretacao.endereco)
    if texto is not None:
        campos = {nome: valor for nome, valor in campos.items() if _foi_dito(nome, valor, texto)}
    troco = None
    if pagamento == DINHEIRO and product_id is not None and quantidade is not None:
        # S2: troco só com Dinheiro e para pelo menos o total (preço do CATÁLOGO).
        total = catalogo[product_id].preco * quantidade
        troco = _valor(interpretacao.troco)
        if troco is not None and troco < total:
            troco = None
    return PedidoInterpretado(
        intencao=_intencao(interpretacao.intencao),
        product_id=product_id,
        quantidade=quantidade,
        endereco=_endereco_completo(campos),
        campos_do_endereco=campos,
        pagamento=pagamento,
        troco=troco,
    )


_CAMPOS = {
    "rua": "logradouro",
    "numero": "numero",
    "bairro": "bairro",
    "cidade": "cidade",
    "estado": "estado",
    "complemento": "complemento",
    "referencia": "referencia",
}
_OBRIGATORIOS = ("logradouro", "numero", "bairro", "cidade", "estado")


def _intencao(valor: Any) -> str:
    """Intenção conhecida (sem diferenciar maiúsculas) ou OUTRA."""
    if isinstance(valor, str) and valor.strip().upper() in INTENCOES:
        return valor.strip().upper()
    return "OUTRA"


def _quantidade(valor: Any) -> int | None:
    """Inteiro de 1 a 10 (aceita o texto "3"); 2.5, 2.0, True e o resto → None."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, str) and valor.strip().isascii() and valor.strip().isdigit():
        valor = int(valor.strip())
    if isinstance(valor, int) and 1 <= valor <= MAX_QUANTIDADE:
        return valor
    return None


def _valor(valor: Any) -> Decimal | None:
    """Valor em reais vindo como texto ("R$ 250") ou número (250); bool e o resto → None."""
    if isinstance(valor, bool) or not isinstance(valor, (str, int, float)):
        return None
    return valor_em_reais(str(valor))


def _texto(valor: Any) -> bool:
    return isinstance(valor, str) and 0 < len(valor.strip()) <= MAX_TEXTO


def _campos_do_endereco(endereco: Any) -> dict[str, str]:
    """Campos válidos do endereço, já com os nomes do `Endereco` e o estado por extenso."""
    if not isinstance(endereco, dict):
        return {}
    campos = {}
    for chave, nome in _CAMPOS.items():
        valor = endereco.get(chave)
        if not _texto(valor):
            continue
        valor = " ".join(valor.split())
        if nome == "estado":
            valor = nome_do_estado(valor)
            if valor is None:
                continue
        if nome == "numero":
            valor = numero_do_endereco(valor)  # A2: "ap409" não é o número do prédio
            if valor is None:
                continue
        campos[nome] = valor
    return campos


def _foi_dito(nome: str, valor: str, texto: str) -> bool:
    """I5: a IA pode "completar" o endereço com o que o cliente NÃO disse.

    Um campo só vale se aparecer na mensagem como palavra(s) inteira(s),
    sem diferenciar maiúsculas, acentos e pontuação. O estado vale pela
    sigla ou pelo nome ("PI" ou "Piauí").
    """
    mensagem = _palavras(texto)
    if nome == "estado":
        siglas = [s for s, n in NOMES_POR_SIGLA.items() if n == valor]
        return any(_contem(mensagem, _palavras(v)) for v in [valor, *siglas])
    return _contem(mensagem, _palavras(valor))


def _palavras(texto: str) -> str:
    """Normalizado e só com letras/números separados por um espaço."""
    return " ".join(re.findall(r"[a-z0-9/]+", normalizar(texto)))


def _contem(mensagem: str, trecho: str) -> bool:
    return bool(trecho) and re.search(rf"(?<![a-z0-9/]){re.escape(trecho)}(?![a-z0-9/])", mensagem) is not None


def _endereco_completo(campos: dict[str, str]) -> Endereco | None:
    if not all(c in campos for c in _OBRIGATORIOS):
        return None
    return Endereco(**campos)


class InterpretadorFalso:
    """Interpretador para testes e demonstração sem rede.

    `respostas` mapeia o texto recebido para a `Interpretacao` a devolver;
    texto sem resposta → None (como se a IA não tivesse entendido).
    Guarda o que recebeu em `recebidos`, para conferir o que iria ao provedor.
    """

    def __init__(self, respostas: dict[str, Interpretacao | None] | None = None) -> None:
        self.respostas = dict(respostas or {})
        self.recebidos: list[tuple[str, Contexto]] = []

    def interpretar(self, texto: str, contexto: Contexto) -> Interpretacao | None:
        self.recebidos.append((texto, contexto))
        return self.respostas.get(texto)

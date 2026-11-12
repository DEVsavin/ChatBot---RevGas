"""Regras de entrada da conversa: entender o que o cliente digitou.

Funções puras: recebem texto e devolvem o valor entendido, ou None quando
não dá para entender (a conversa explica e repete a pergunta).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal


def _tem_digito_nao_ascii(texto: str) -> bool:
    """Dígitos de outros alfabetos ("１", "٨") contam como dígito em Python, mas não para a API (I1)."""
    return any(c.isdigit() and not c.isascii() for c in texto)


def telefone(texto: str) -> str | None:
    """Telefone no formato da API: 55 + DDD + número (12 ou 13 dígitos).

    Aceita formatação comum: "(86) 99999-0000", "+55 86 99999-0000".
    Sem o 55, prefixa (10 ou 11 dígitos). Outro tamanho → None.
    """
    if _tem_digito_nao_ascii(texto):
        return None
    digitos = "".join(c for c in texto if c.isdigit())
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos
    return None


DDD_FICTICIO = "00"  # não existe no Brasil: números de demonstração


def numero_ficticio(numero: str) -> bool:
    """Número de demonstração (DDD 00), no formato da API ("5500...")."""
    return numero[2:4] == DDD_FICTICIO


def telefone_legivel(numero: str) -> str:
    """"5586999990000" -> "(86) 99999-0000"; "558633330000" -> "(86) 3333-0000"."""
    ddd, resto = numero[2:4], numero[4:]
    return f"({ddd}) {resto[:-4]}-{resto[-4:]}"


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, espaços simples e sem pontuação nas pontas."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.casefold().split()).strip(" .,!?;:")


_SIM = frozenset({"sim", "s", "isso", "pode", "confirmo"})
_NAO = frozenset({"nao", "n"})


def sim_ou_nao(texto: str) -> bool | None:
    """True para sim, False para não, None se não for nem um nem outro."""
    resposta = normalizar(texto)
    if resposta in _SIM:
        return True
    if resposta in _NAO:
        return False
    return None


def opcao(texto: str, opcoes: list[str]) -> int | None:
    """Índice (a partir de 0) da opção escolhida pelo número ("2", "2)") ou pelo texto."""
    resposta = normalizar(texto).rstrip(")")
    if resposta.isdigit():
        numero = int(resposta)
        return numero - 1 if 1 <= numero <= len(opcoes) else None
    for indice, rotulo in enumerate(opcoes):
        if resposta == normalizar(rotulo):
            return indice
    return None


_SO_DECIMAL = re.compile(r"\d+([.,]\d{1,2})?", re.ASCII)  # 200 · 200,50 · 200.50
_COM_MILHAR = re.compile(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", re.ASCII)  # 1.200 · 1.200,00


def valor_em_reais(texto: str) -> Decimal | None:
    """Valor em reais > 0 com até 2 casas: "200", "200,00", "R$ 1.200,50"."""
    if _tem_digito_nao_ascii(texto):
        return None  # normalizar trocaria ou apagaria esses dígitos em silêncio
    limpo = normalizar(texto).replace("r$", "").replace(" ", "")
    if _COM_MILHAR.fullmatch(limpo):
        limpo = limpo.replace(".", "").replace(",", ".")
    elif _SO_DECIMAL.fullmatch(limpo):
        limpo = limpo.replace(",", ".")
    else:
        return None
    valor = Decimal(limpo)
    return valor if valor > 0 else None


# Comandos que valem em qualquer passo (lista inicial; será substituída no C2).
# Casam só como palavra ou frase INTEIRA, já normalizadas (sem acento).
_VAZAMENTO = ("vazamento", "vazando", "cheiro de gas", "cheirando gas")
_COMANDOS = {
    "CANCELAR_OU_DEVOLVER": ("cancelar", "cancela", "devolver", "devolucao", "estorno", "reembolso"),
    "ATENDENTE": ("atendente", "humano", "pessoa"),
    "SAIR": ("sair", "parar", "tchau"),
}


def _contem(texto: str, frases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(frase)}\b", texto) for frase in frases)


def comando(texto: str, *, livre: bool) -> str | None:
    """Comando global na mensagem, ou None.

    - Vazamento vale sempre, em qualquer lugar da mensagem (prioridade).
    - Nos passos de texto livre (`livre=True`: nome, endereço), os outros
      comandos só valem se forem a mensagem inteira ("Rua do Atendente, 10"
      é endereço). Nos demais passos, valem como palavra inteira.
    """
    mensagem = normalizar(texto)
    if _contem(mensagem, _VAZAMENTO):
        return "VAZAMENTO"
    for nome, palavras in _COMANDOS.items():
        if livre:
            if mensagem in palavras:
                return nome
        elif _contem(mensagem, palavras):
            return nome
    return None

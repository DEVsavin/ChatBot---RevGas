"""Estados brasileiros: aceita sigla ou nome e devolve o nome canônico.

Nas vendas observadas na API, `state_federation` vem por extenso
("Piauí"), não como sigla ("PI"). Por isso guardamos sempre o nome.
"""

from __future__ import annotations

import unicodedata

NOMES_POR_SIGLA = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal",
    "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco",
    "PI": "Piauí", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}


def _chave(texto: str) -> str:
    """Minúsculas, sem acento e com espaços simples: "  Piauí " -> "piaui"."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.casefold().split())


_CANONICO = {_chave(sigla): nome for sigla, nome in NOMES_POR_SIGLA.items()}
_CANONICO |= {_chave(nome): nome for nome in NOMES_POR_SIGLA.values()}


def nome_do_estado(texto: str) -> str | None:
    """"PI", "pi", "piaui" ou "Piauí" -> "Piauí". Desconhecido -> None."""
    return _CANONICO.get(_chave(texto))

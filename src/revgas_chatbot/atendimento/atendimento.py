"""Resultado comum a todos os fluxos: passar o atendimento para uma pessoa e mascarar dados sensíveis."""

from __future__ import annotations

import re
from dataclasses import dataclass

# 11 ou mais dígitos, aceitando espaço, ponto ou traço entre eles
# (CPF "123.456.789-01", cartão "4111 1111 1111 1111", chave PIX por telefone).
NUMERO_LONGO = re.compile(r"\d(?:[ .\-]?\d){10,}")
MASCARA = "[número omitido]"


def mascarar(texto: str) -> str:
    return NUMERO_LONGO.sub(MASCARA, texto)


@dataclass(frozen=True)
class TransferirAtendente:
    motivo: str  # para a equipe (nunca vai direto ao cliente)
    pedido_id: str | None = None
    venda_pode_existir: bool = False  # True em timeout/5xx: a equipe precisa conferir

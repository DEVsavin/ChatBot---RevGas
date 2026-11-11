"""Resultado comum a todos os fluxos: passar o atendimento para uma pessoa."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransferirAtendente:
    """O chatbot não consegue concluir com segurança: passa para uma pessoa da equipe.

    - `pedido_id`: o pedido do chatbot, quando houver (registro de venda).
    - `venda_pode_existir`: avisa o atendente para procurar a venda pela marca
      `[chatbot:<pedido_id>]` antes de criar outra.
    """

    motivo: str
    pedido_id: str | None = None
    venda_pode_existir: bool = False

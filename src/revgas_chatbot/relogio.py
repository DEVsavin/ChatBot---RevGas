"""Relógio real do programa (os testes usam o RelogioFalso da api_falsa)."""

from __future__ import annotations

import time
from datetime import datetime

from revgas_chatbot.fuso import FUSO_TERESINA


class RelogioDoSistema:
    def agora(self) -> datetime:
        return datetime.now(FUSO_TERESINA)  # sempre com fuso

    def dormir(self, segundos: float) -> None:
        time.sleep(segundos)

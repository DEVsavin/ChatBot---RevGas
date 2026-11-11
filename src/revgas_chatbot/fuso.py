"""Fuso horário da operação (Teresina, UTC-3, sem horário de verão)."""

from datetime import timedelta, timezone

FUSO_TERESINA = timezone(timedelta(hours=-3))

"""Porta para a API da RevGás.

`ApiRevGas` é a interface que o `RegistradorDeVendas` usa para falar com a
API. Há dois adaptadores:
- `ApiRevGasFalsa` (em memória), para testes e demonstração sem rede;
- o adaptador HTTP real (M4), usado só com autorização.

Contrato de cada operação:
- devolve `Resposta(status, dados)` para qualquer resposta HTTP, inclusive
  erros (4xx/5xx); quem interpreta o status é o `RegistradorDeVendas`;
- em listagens, `dados` vem no formato da API:
  `{"data": [...], "pagination": {"has_next": bool, ...}}`;
- em criação, `dados` é o objeto criado (sem o envelope `data`);
- lança `TempoEsgotado` quando a requisição foi enviada mas a resposta não
  chegou (timeout ou queda de rede): o servidor PODE ter processado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Resposta:
    status: int
    dados: Any = None


class TempoEsgotado(Exception):
    """A requisição saiu, mas a resposta não chegou. O resultado é incerto."""


class ApiRevGas(Protocol):
    def buscar_clientes(self, telefone: str) -> Resposta: ...

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta: ...

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta: ...

    def buscar_vendas(
        self, *, customer_id: int, channel: str | None, de: date, ate: date, pagina: int
    ) -> Resposta:
        """Vendas do cliente entre `de` e `ate` (datas).

        `channel=None` não filtra por canal (vendas por WhatsApp, telefone etc.).
        A doc diz que a lista vem ordenada da mais nova para a mais antiga,
        mas isso não foi confirmado na prática: quem chama não deve confiar
        nem na ordem nem nos filtros.
        """
        ...


class Relogio(Protocol):
    """Tempo injetado: permite testar esperas sem deixar os testes lentos."""

    def agora(self) -> datetime: ...  # sempre com fuso

    def dormir(self, segundos: float) -> None: ...

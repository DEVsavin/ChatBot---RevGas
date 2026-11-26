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

    def buscar_cliente(self, cliente_id: int) -> Resposta:
        """Detalha um cliente específico (`GET /customers/{id}`)."""
        ...

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta: ...

    def atualizar_cliente(self, cliente_id: int, dados: dict[str, Any]) -> Resposta:
        """Atualiza dados de um cliente existente (`PUT /customers/{id}`)."""
        ...

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta: ...

    def buscar_vendas(
        self, *, customer_id: int, channel: str | None, de: date, ate: date, pagina: int, page_size: int = 100
    ) -> Resposta:
        """Vendas do cliente entre `de` e `ate` (datas).

        `channel=None` não filtra por canal (vendas por WhatsApp, telefone etc.).
        A doc diz que a lista vem ordenada da mais nova para a mais antiga,
        mas isso não foi confirmado na prática: quem chama não deve confiar
        nem na ordem nem nos filtros.
        """
        ...

    def buscar_venda(self, venda_id: int) -> Resposta:
        """Detalha uma venda específica (`GET /sales/{id}`), incluindo status e logs."""
        ...

    def atualizar_venda(self, venda_id: int, corpo: dict[str, Any]) -> Resposta:
        """Atualiza uma venda existente (`PUT /sales/{id}`), ex.: anotar `observation`."""
        ...

    def buscar_produtos(
        self, *, establishment_id: int, pagina: int = 1, page_size: int = 100
    ) -> Resposta:
        """Lista produtos ativos do estabelecimento (`GET /products`)."""
        ...

    def buscar_acordos(
        self, *, establishment_id: int, pagina: int = 1, page_size: int = 100
    ) -> Resposta:
        """Lista acordos de preço do estabelecimento (`GET /deals`)."""
        ...


class Relogio(Protocol):
    """Tempo injetado: permite testar esperas sem deixar os testes lentos."""

    def agora(self) -> datetime: ...  # sempre com fuso

    def dormir(self, segundos: float) -> None: ...

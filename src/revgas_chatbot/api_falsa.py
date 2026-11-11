"""Adaptador em memória da `ApiRevGas`, para testes e demonstração sem rede.

Guarda clientes e vendas em listas e imita o formato das respostas da API.
Falhas podem ser roteirizadas por operação com `roteirizar(...)`:

    api.roteirizar("criar_venda", Resposta(500, {"message": "erro"}))
    api.roteirizar("criar_venda", TIMEOUT_DEPOIS_DE_CRIAR)
    api.roteirizar("buscar_clientes", lambda: ...)  # roda algo durante a chamada

Cada item do roteiro vale para UMA chamada; sem roteiro, a operação se
comporta normalmente.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from revgas_chatbot.api import Resposta, TempoEsgotado
from revgas_chatbot.fuso import FUSO_TERESINA

# Roteiros especiais de timeout:
TIMEOUT_SEM_CRIAR = "timeout_sem_criar"  # a requisição se perdeu antes de chegar
TIMEOUT_DEPOIS_DE_CRIAR = "timeout_depois_de_criar"  # o servidor criou, a resposta se perdeu

OPERACOES = frozenset({"buscar_clientes", "cadastrar_cliente", "criar_venda", "buscar_vendas"})


@dataclass(frozen=True)
class DepoisDeCriar:
    """Roteiro: a operação é feita normalmente, mas a resposta é outra (ex.: 504 de um proxy)."""

    resposta: Resposta


def _publico(venda: dict[str, Any]) -> dict[str, Any]:
    """Tira os campos internos da API falsa (começados com "_")."""
    return {k: v for k, v in venda.items() if not k.startswith("_")}


class RelogioFalso:
    """Relógio controlado pelo teste. `dormir` só avança o tempo."""

    def __init__(self, inicio: datetime | None = None) -> None:
        self._agora = inicio or datetime(2026, 11, 10, 14, 0, tzinfo=FUSO_TERESINA)
        self.esperas: list[float] = []

    def agora(self) -> datetime:
        return self._agora

    def dormir(self, segundos: float) -> None:
        self.esperas.append(segundos)
        self._agora += timedelta(seconds=segundos)

    def avancar(self, segundos: float) -> None:
        self._agora += timedelta(seconds=segundos)


@dataclass
class ApiRevGasFalsa:
    relogio: RelogioFalso = field(default_factory=RelogioFalso)
    clientes: list[dict[str, Any]] = field(default_factory=list)
    vendas: list[dict[str, Any]] = field(default_factory=list)
    # Instantes em que cada requisição chegou (para conferir o limite de 1 req/s).
    requisicoes: list[datetime] = field(default_factory=list)
    # Quantas buscas de vendas são necessárias até uma venda nova aparecer.
    atraso_de_visibilidade: int = 0
    tamanho_da_pagina: int = 20
    _roteiro: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _proximo_id: int = 1000
    _buscas_de_vendas: int = 0

    # --- controle pelo teste -------------------------------------------------

    def roteirizar(self, operacao: str, *saidas: Any) -> None:
        if operacao not in OPERACOES:  # erro de digitação não pode virar roteiro mudo
            raise ValueError(f"operação desconhecida: {operacao!r}")
        self._roteiro[operacao].extend(saidas)

    def semear_venda(self, corpo: dict[str, Any]) -> dict[str, Any]:
        """Cria uma venda direto no estado da API falsa (ex.: venda de outro pedido)."""
        return self._criar_venda(corpo).dados

    def vendas_criadas(self) -> list[dict[str, Any]]:
        return list(self.vendas)

    # --- operações da porta ----------------------------------------------------

    def buscar_clientes(self, telefone: str) -> Resposta:
        return self._executar("buscar_clientes", lambda: self._buscar_clientes(telefone))

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta:
        return self._executar("cadastrar_cliente", lambda: self._cadastrar_cliente(dados))

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta:
        return self._executar("criar_venda", lambda: self._criar_venda(corpo))

    def buscar_vendas(
        self, *, customer_id: int, channel: str, de: date, ate: date, pagina: int
    ) -> Resposta:
        return self._executar(
            "buscar_vendas",
            lambda: self._buscar_vendas(customer_id, channel, de, ate, pagina),
        )

    # --- implementação ---------------------------------------------------------

    def _executar(self, operacao: str, normal) -> Resposta:
        self.requisicoes.append(self.relogio.agora())
        roteiro = self._roteiro[operacao]
        saida = roteiro.popleft() if roteiro else None
        if saida == TIMEOUT_SEM_CRIAR:
            raise TempoEsgotado(operacao)
        if saida == TIMEOUT_DEPOIS_DE_CRIAR:
            normal()
            raise TempoEsgotado(operacao)
        if isinstance(saida, DepoisDeCriar):
            normal()
            return saida.resposta
        if isinstance(saida, Resposta):
            return saida
        if callable(saida):
            # Ação "no meio" da chamada (ex.: chega outra mensagem do cliente).
            saida()
        return normal()

    def _novo_id(self) -> int:
        self._proximo_id += 1
        return self._proximo_id

    def _buscar_clientes(self, telefone: str) -> Resposta:
        achados = [c for c in self.clientes if telefone in c.get("phones", [])]
        return Resposta(200, {"data": achados, "pagination": {"has_next": False}})

    def _cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta:
        cliente = {**dados, "id": self._novo_id()}
        self.clientes.append(cliente)
        return Resposta(201, cliente)

    def _criar_venda(self, corpo: dict[str, Any]) -> Resposta:
        total = sum((Decimal(str(p["value"])) * p["quantity"] for p in corpo["products"]), Decimal("0"))
        venda = {
            **corpo,
            "id": self._novo_id(),
            "status": "ACTIVE",
            "total": total,
            "created_at": self.relogio.agora().isoformat(),
            "_visivel_apos": self._buscas_de_vendas + self.atraso_de_visibilidade,
        }
        self.vendas.append(venda)
        return Resposta(201, _publico(venda))

    def _buscar_vendas(self, customer_id: int, channel: str, de: date, ate: date, pagina: int) -> Resposta:
        self._buscas_de_vendas += 1
        visiveis = [
            _publico(venda)
            for venda in self.vendas
            if venda["_visivel_apos"] < self._buscas_de_vendas
            and venda["customer_id"] == customer_id
            and venda["channel"] == channel
            and de <= datetime.fromisoformat(venda["created_at"]).date() <= ate
        ]
        visiveis.sort(key=lambda v: v["created_at"], reverse=True)  # mais recentes primeiro
        inicio = (pagina - 1) * self.tamanho_da_pagina
        fatia = visiveis[inicio : inicio + self.tamanho_da_pagina]
        tem_mais = inicio + self.tamanho_da_pagina < len(visiveis)
        return Resposta(200, {"data": fatia, "pagination": {"page": pagina, "has_next": tem_mais}})

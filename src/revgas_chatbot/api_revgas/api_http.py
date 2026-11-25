"""Adaptador HTTP da `ApiRevGas`: fala com a API de verdade (M4).

Fino de propósito: só traduz cada operação numa requisição e devolve a
resposta como veio. Quem interpreta o status (201, 422, 409, 429, 5xx...)
é o `RegistradorDeVendas`, e o limite de 1 req/s fica na `ApiCadenciada`.

- A chave vai no cabeçalho `Authorization` (sem "Bearer"), nunca na URL.
- Redirecionamento não é seguido: a chave nunca vai para outro endereço.
- Qualquer falha de rede vira `TempoEsgotado` (resultado incerto): o
  registrador nunca repete um POST às cegas.
- Nada vai para o log: nem a chave, nem dados do cliente.

Só é usado com autorização do usuário (chamadas reais no estabelecimento
compartilhado).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import date, datetime
from typing import Any, Callable

import httpx

from revgas_chatbot.api_revgas.api import Resposta, TempoEsgotado
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.venda import corpo_para_json

ESPERA_MAXIMA = 15.0  # segundos


class MonitorApi:
    """Registra em memória as últimas chamadas reais feitas à API RevGás (nunca guarda a chave)."""

    def __init__(self, limite: int = 100) -> None:
        self._eventos: deque[dict[str, Any]] = deque(maxlen=limite)
        self._trava = threading.Lock()
        self._seq = 0

    def registrar(
        self,
        *,
        metodo: str,
        url: str,
        params: dict[str, Any] | None,
        status: int | str,
        duracao_ms: int,
        resumo: str,
        enviado: Any = None,
        recebido: Any = None,
    ) -> None:
        with self._trava:
            self._seq += 1
            self._eventos.appendleft(
                {
                    "id": self._seq,
                    "hora": datetime.now(FUSO_TERESINA).strftime("%H:%M:%S"),
                    "metodo": metodo,
                    "url": url,
                    "params": params or {},
                    "status": status,
                    "duracao_ms": duracao_ms,
                    "resumo": resumo,
                    "enviado": enviado,
                    "recebido": recebido,
                }
            )

    def listar(self) -> list[dict[str, Any]]:
        with self._trava:
            return list(self._eventos)

    def limpar(self) -> None:
        with self._trava:
            self._eventos.clear()


def _resumir_resposta(metodo: str, caminho: str, status: int, dados: Any) -> str:
    if isinstance(dados, dict):
        if "data" in dados and isinstance(dados["data"], list):
            return f"{len(dados['data'])} registro(s) retornado(s)"
        alvo = dados.get("data") if isinstance(dados.get("data"), dict) else dados
        if isinstance(alvo, dict) and "id" in alvo:
            extra = f" · status {alvo['status']}" if alvo.get("status") else ""
            return f"ID {alvo['id']}{extra}"
        if isinstance(dados.get("message"), str):
            return dados["message"][:120]
    return f"HTTP {status} em {metodo} {caminho}"


def _preview_json(dados: Any) -> Any:
    """Versão compacta da resposta para exibição no painel (limita listas grandes)."""
    if isinstance(dados, dict):
        if isinstance(dados.get("data"), list):
            return {
                "total_na_pagina": len(dados["data"]),
                "amostra": dados["data"][:3],
                "pagination": dados.get("pagination"),
            }
        return dados
    return dados

# O httpx registra cada URL no log (nível INFO), e a busca de cliente leva o
# telefone na URL (RN10). Só avisos e erros dele passam.
for _nome in ("httpx", "httpcore"):
    logging.getLogger(_nome).setLevel(logging.WARNING)


class ApiRevGasHttp:
    def __init__(
        self,
        url_base: str,
        chave: str,
        *,
        transporte: httpx.BaseTransport | None = None,
        monitor: MonitorApi | None = None,
    ) -> None:
        if not url_base.strip() or not chave.strip():
            raise ValueError("a URL da API e a chave (REVGAS_API_KEY) são obrigatórias")
        self._url_base = url_base.strip().rstrip("/")
        self._monitor = monitor
        self._cliente = httpx.Client(
            base_url=self._url_base,
            headers={"Authorization": chave.strip(), "Accept": "application/json"},
            timeout=ESPERA_MAXIMA,
            follow_redirects=False,
            transport=transporte,
        )

    # --- operações da porta -----------------------------------------------------------

    def buscar_clientes(self, telefone: str) -> Resposta:
        return self._enviar("GET", "/customers", params={"phone": telefone})

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta:
        return self._criacao(self._enviar("POST", "/customers", json=dados))

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta:
        return self._criacao(self._enviar("POST", "/sales", json=corpo))

    def buscar_vendas(
        self, *, customer_id: int, channel: str | None, de: date, ate: date, pagina: int, page_size: int = 100
    ) -> Resposta:
        params: dict[str, Any] = {"customer_id": customer_id}
        if channel is not None:
            params["channel"] = channel
        params.update({"from": de.isoformat(), "to": ate.isoformat(), "page": pagina, "page_size": page_size})
        return self._enviar("GET", "/sales", params=params)

    def buscar_venda(self, venda_id: int) -> Resposta:
        resposta = self._enviar("GET", f"/sales/{int(venda_id)}")
        return self._sem_envelope(resposta) if resposta.status == 200 else resposta

    def atualizar_venda(self, venda_id: int, corpo: dict[str, Any]) -> Resposta:
        resposta = self._enviar("PUT", f"/sales/{int(venda_id)}", json=corpo)
        return self._sem_envelope(resposta) if resposta.status == 200 else resposta

    def buscar_produtos(
        self, *, establishment_id: int, pagina: int = 1, page_size: int = 100
    ) -> Resposta:
        params = {
            "establishment_id": establishment_id,
            "inactive": "false",
            "page": pagina,
            "page_size": page_size,
        }
        return self._enviar("GET", "/products", params=params)

    def buscar_acordos(
        self, *, establishment_id: int, pagina: int = 1, page_size: int = 100
    ) -> Resposta:
        params = {
            "establishment_id": establishment_id,
            "payment_type": "MONEY",
            "customer_isnull": "true",
            "tag_isnull": "true",
            "page": pagina,
            "page_size": page_size,
        }
        return self._enviar("GET", "/deals", params=params)

    # --- fora da porta: só para limpar a venda de teste (M4c, dúvida 8) ----------------

    def cancelar_venda(self, venda_id: int) -> Resposta:
        resposta = self._enviar("PUT", f"/sales/{int(venda_id)}/cancel", json={"motive_cancel_sale_id": None})
        return self._sem_envelope(resposta) if resposta.status == 200 else resposta

    # --- detalhes ---------------------------------------------------------------------

    def _enviar(self, metodo: str, caminho: str, **opcoes: Any) -> Resposta:
        enviado = None
        if "json" in opcoes:
            # Dinheiro é Decimal no chatbot: vai como número JSON (110.0), nunca como texto.
            corpo_str = corpo_para_json(opcoes.pop("json"))
            opcoes["content"] = corpo_str
            opcoes["headers"] = {"Content-Type": "application/json"}
            if self._monitor is not None:
                import json

                enviado = json.loads(corpo_str)
        params = dict(opcoes.get("params") or {}) if self._monitor is not None else None
        inicio = time.monotonic()
        try:
            resposta = self._cliente.request(metodo, caminho, **opcoes)
        except httpx.TransportError:
            if self._monitor is not None:
                self._monitor.registrar(
                    metodo=metodo,
                    url=f"{self._url_base}{caminho}",
                    params=params,
                    status="TIMEOUT",
                    duracao_ms=int((time.monotonic() - inicio) * 1000),
                    resumo="Sem resposta da API (tempo esgotado / rede)",
                    enviado=enviado,
                )
            raise TempoEsgotado(f"sem resposta da API em {metodo} {caminho.split('/')[1]}") from None
        duracao_ms = int((time.monotonic() - inicio) * 1000)
        try:
            dados = resposta.json()
        except ValueError:
            dados = None
        if self._monitor is not None:
            self._monitor.registrar(
                metodo=metodo,
                url=f"{self._url_base}{caminho}",
                params=params,
                status=resposta.status_code,
                duracao_ms=duracao_ms,
                resumo=_resumir_resposta(metodo, caminho, resposta.status_code, dados),
                enviado=enviado,
                recebido=_preview_json(dados),
            )
        return Resposta(resposta.status_code, dados)

    @classmethod
    def _criacao(cls, resposta: Resposta) -> Resposta:
        """Na criação (201), a porta devolve o objeto criado, sem o envelope `data`."""
        return cls._sem_envelope(resposta) if resposta.status == 201 else resposta

    @staticmethod
    def _sem_envelope(resposta: Resposta) -> Resposta:
        dados = resposta.dados
        if isinstance(dados, dict) and isinstance(dados.get("data"), dict):
            return Resposta(resposta.status, dados["data"])
        return resposta

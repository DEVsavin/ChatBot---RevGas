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
from datetime import date
from typing import Any

import httpx

from revgas_chatbot.api_revgas.api import Resposta, TempoEsgotado
from revgas_chatbot.vendas.venda import corpo_para_json

ESPERA_MAXIMA = 15.0  # segundos

# O httpx registra cada URL no log (nível INFO), e a busca de cliente leva o
# telefone na URL (RN10). Só avisos e erros dele passam.
for _nome in ("httpx", "httpcore"):
    logging.getLogger(_nome).setLevel(logging.WARNING)


class ApiRevGasHttp:
    def __init__(self, url_base: str, chave: str, *, transporte: httpx.BaseTransport | None = None) -> None:
        if not url_base.strip() or not chave.strip():
            raise ValueError("a URL da API e a chave (REVGAS_API_KEY) são obrigatórias")
        self._cliente = httpx.Client(
            base_url=url_base.strip().rstrip("/"),
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
        self, *, customer_id: int, channel: str | None, de: date, ate: date, pagina: int
    ) -> Resposta:
        params: dict[str, Any] = {"customer_id": customer_id}
        if channel is not None:
            params["channel"] = channel
        params.update({"from": de.isoformat(), "to": ate.isoformat(), "page": pagina})
        return self._enviar("GET", "/sales", params=params)

    # --- fora da porta: só para limpar a venda de teste (M4c, dúvida 8) ----------------

    def cancelar_venda(self, venda_id: int) -> Resposta:
        resposta = self._enviar("PUT", f"/sales/{int(venda_id)}/cancel", json={"motive_cancel_sale_id": None})
        return self._sem_envelope(resposta) if resposta.status == 200 else resposta

    # --- detalhes ---------------------------------------------------------------------

    def _enviar(self, metodo: str, caminho: str, **opcoes: Any) -> Resposta:
        if "json" in opcoes:
            # Dinheiro é Decimal no chatbot: vai como número JSON (110.0), nunca como texto.
            opcoes["content"] = corpo_para_json(opcoes.pop("json"))
            opcoes["headers"] = {"Content-Type": "application/json"}
        try:
            resposta = self._cliente.request(metodo, caminho, **opcoes)
        except httpx.TransportError:
            raise TempoEsgotado(f"sem resposta da API em {metodo} {caminho.split('/')[1]}") from None
        try:
            dados = resposta.json()
        except ValueError:
            dados = None
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

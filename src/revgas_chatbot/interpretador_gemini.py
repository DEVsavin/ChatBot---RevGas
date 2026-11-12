"""Adaptador do Gemini (Google AI Studio) para a porta `Interpretador` (C2d).

Uma chamada HTTP `generateContent` pedindo a resposta em JSON (schema da
`Interpretacao`). Qualquer problema (sem rede, chave inválida, limite da
cota, demora acima de 8 s, resposta bloqueada ou fora do formato) vira
`None`, e a conversa segue o roteiro do C1 (C2-3). Nunca lança.

Cota grátis: o Google pode usar as mensagens → só com clientes fictícios
(a montagem impede o uso com a API de verdade).

Nada do texto do cliente nem a chave vão para o log. A chave vai só no
cabeçalho `x-goog-api-key`, nunca na URL.

O campo `responseJsonSchema` do `generationConfig` e o caminho
`candidates[0].content.parts[0].text` da resposta foram confirmados num
teste real em 2026-11-11 (modelo gemini-3.5-flash-lite, frases fictícias).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from revgas_chatbot.interpretacao import Contexto, Interpretacao

URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
MODELO_PADRAO = "gemini-3.5-flash-lite"  # recomendado pela documentação para projetos novos
ESPERA_MAXIMA = 8.0  # segundos (C2-D2)

_log = logging.getLogger(__name__)

INSTRUCOES = """Você extrai dados de mensagens de clientes de uma revenda de gás de cozinha.
Responda só com o JSON pedido. Regras:
- Não invente nada: preencha um campo só se o cliente disse aquilo nesta mensagem.
- intencao: PEDIR (quer comprar ou está respondendo sobre o pedido), VAZAMENTO (cheiro de gás,
  gás escapando, vazamento), CANCELAR_OU_DEVOLVER, ATENDENTE (quer falar com uma pessoa),
  SAIR (quer encerrar) ou OUTRA.
- produto: um dos produtos da lista, escrito exatamente como na lista.
- pagamento: uma das formas de pagamento da lista, escrita exatamente como na lista.
- quantidade: número inteiro de unidades.
- troco: o valor em reais para o qual o cliente quer troco.
- endereco: só as partes que o cliente disse (rua, numero, bairro, cidade, estado, complemento, referencia)."""

_TEXTO = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "intencao": {
            "type": "string",
            "enum": ["PEDIR", "CANCELAR_OU_DEVOLVER", "ATENDENTE", "VAZAMENTO", "SAIR", "OUTRA"],
        },
        "produto": _TEXTO,
        "quantidade": {"type": "integer"},
        "endereco": {
            "type": "object",
            "properties": {c: _TEXTO for c in
                           ("rua", "numero", "bairro", "cidade", "estado", "complemento", "referencia")},
        },
        "pagamento": _TEXTO,
        "troco": {"type": "number"},
    },
    "required": ["intencao"],
}


class InterpretadorGemini:
    def __init__(
        self,
        chave: str,
        *,
        modelo: str = MODELO_PADRAO,
        transporte: httpx.BaseTransport | None = None,  # testes: httpx.MockTransport
    ) -> None:
        self._chave = chave
        self._url = URL.format(modelo=modelo)
        self._cliente = httpx.Client(transport=transporte, timeout=ESPERA_MAXIMA)

    def interpretar(self, texto: str, contexto: Contexto) -> Interpretacao | None:
        try:
            resposta = self._cliente.post(
                self._url,
                headers={"x-goog-api-key": self._chave},
                json=self._corpo(texto, contexto),
            )
        except httpx.HTTPError:
            _log.warning("IA sem resposta (rede ou demora); seguindo pelo roteiro")
            return None
        if resposta.status_code != 200:
            # Só o código: a mensagem de erro do Google pode repetir a chave.
            _log.warning("IA respondeu HTTP %s; seguindo pelo roteiro", resposta.status_code)
            return None
        return _interpretacao(resposta)

    def _corpo(self, texto: str, contexto: Contexto) -> dict[str, Any]:
        mensagem = (
            f"Passo da conversa: {contexto.passo}\n"
            f"Produtos: {', '.join(contexto.produtos)}\n"
            f"Formas de pagamento: {', '.join(contexto.pagamentos)}\n"
            f"Mensagem do cliente: {texto}"
        )
        return {
            "systemInstruction": {"parts": [{"text": INSTRUCOES}]},
            "contents": [{"role": "user", "parts": [{"text": mensagem}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": SCHEMA,
                "temperature": 0,
            },
        }


def _interpretacao(resposta: httpx.Response) -> Interpretacao | None:
    """Lê `candidates[0].content.parts[0].text` como JSON; qualquer desvio → None."""
    try:
        candidato = resposta.json()["candidates"][0]
        if candidato.get("finishReason") != "STOP":
            return None
        dados = json.loads(candidato["content"]["parts"][0]["text"])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(dados, dict) or "intencao" not in dados:
        return None
    return Interpretacao(
        intencao=dados.get("intencao"),
        produto=dados.get("produto"),
        quantidade=dados.get("quantidade"),
        endereco=dados.get("endereco"),
        pagamento=dados.get("pagamento"),
        troco=dados.get("troco"),
    )

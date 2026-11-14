"""Cliente do WAHA (WhatsApp HTTP API, rodando no Docker desta máquina).

Spec: `.scratch/whatsapp/spec-waha.md`. Duas coisas:
- `enviar(telefone, texto)`: manda uma mensagem de texto (`/api/sendText`),
  no mesmo `chatId` em que a pessoa escreveu (`lembrar`). Nunca reenvia.
- `numero_do_lid(lid)`: descobre o telefone de um identificador oculto
  (`...@lid`), se o WAHA souber.

O WAHA é sempre local (`http://127.0.0.1` ou `http://localhost`). A chave vai
no cabeçalho `X-Api-Key`. Nada de chave, número ou texto no log.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

import httpx

from revgas_chatbot.conversa.entrada import telefone

ESPERA_MAXIMA = 10.0  # segundos

_log = logging.getLogger(__name__)

# O httpx registra cada URL no log (nível INFO); só avisos e erros dele passam.
for _nome in ("httpx", "httpcore"):
    logging.getLogger(_nome).setLevel(logging.WARNING)


def _url_local(url: str) -> str:
    partes = urlsplit(url.strip())
    if partes.scheme != "http" or partes.hostname not in ("127.0.0.1", "localhost") or partes.path not in ("", "/"):
        raise ValueError("WAHA_URL precisa ser local, ex.: http://127.0.0.1:3000")
    return url.strip().rstrip("/")


def digitos_do_chat(chat_id: str) -> str | None:
    """'5586...@c.us' ou '5586...@s.whatsapp.net' → '5586...'; outro formato → None."""
    for sufixo in ("@c.us", "@s.whatsapp.net"):
        if chat_id.endswith(sufixo):
            numero = chat_id[: -len(sufixo)]
            return numero if numero.isdigit() else None
    return None


class ClienteWaha:
    def __init__(self, url: str, chave: str, sessao: str, *, transporte: httpx.BaseTransport | None = None) -> None:
        if not chave.strip():
            raise ValueError("WAHA_API_KEY é obrigatória")
        self._sessao = sessao
        self._cliente = httpx.Client(
            base_url=_url_local(url),
            headers={"X-Api-Key": chave.strip()},
            timeout=ESPERA_MAXIMA,
            follow_redirects=False,
            transport=transporte,
        )
        self._chats: dict[str, str] = {}  # telefone → chatId em que a pessoa escreveu

    def lembrar(self, telefone_: str, chat_id: str) -> None:
        self._chats[telefone_] = chat_id

    def enviar(self, para: str, texto: str) -> bool:
        corpo = {"session": self._sessao, "chatId": self._chats.get(para, f"{para}@c.us"), "text": texto}
        try:
            resposta = self._cliente.post("/api/sendText", json=corpo)
        except httpx.HTTPError as erro:
            _log.warning("envio pelo WAHA falhou: %s", type(erro).__name__)
            return False
        if resposta.status_code in (200, 201):
            return True
        _log.warning("envio pelo WAHA recusado: HTTP %s", resposta.status_code)
        return False

    def numero_do_lid(self, lid: str) -> str | None:
        """Telefone (55 + DDD + número) de um `...@lid`, ou None se o WAHA não souber."""
        try:
            resposta = self._cliente.get(f"/api/{quote(self._sessao, safe='')}/lids/{quote(lid, safe='')}")
            dados = resposta.json() if resposta.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        pn = dados.get("pn") if isinstance(dados, dict) else None
        numero = digitos_do_chat(pn) if isinstance(pn, str) else None
        return numero if numero and telefone(numero) == numero else None

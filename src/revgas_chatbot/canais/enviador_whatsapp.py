"""Envio das respostas pelo WhatsApp (Graph API da Meta), WA2.

`EnviadorWhatsApp.enviar(para, texto)` manda UMA mensagem de texto e diz se
deu certo. Nunca reenvia (evita mensagem duplicada), com UMA exceção (I2):
o erro 131030 ("destinatário fora da lista") quer dizer que nada foi
entregue; aí tenta uma vez a outra forma do número (com/sem o 9), porque o
WhatsApp às vezes entrega o celular brasileiro sem o 9 e o destinatário de
teste foi cadastrado com ele (ou o contrário). A forma que funcionou fica
lembrada para aquele número.

Nada de conteúdo vai para o log: nem token, nem número, nem texto.
"""

from __future__ import annotations

import logging

import httpx

VERSAO = "v26.0"  # Graph API, fixa (v26.0 lançada em 29/07/2026)
URL_BASE = "https://graph.facebook.com"
ESPERA_MAXIMA = 10.0  # segundos
FORA_DA_LISTA = 131030

_log = logging.getLogger(__name__)

# O httpx registra cada URL no log (nível INFO); só avisos e erros dele passam.
for _nome in ("httpx", "httpcore"):
    logging.getLogger(_nome).setLevel(logging.WARNING)


def outra_forma(numero: str) -> str | None:
    """O mesmo celular com/sem o 9: 13 dígitos -> 12; 12 dígitos -> 13. Fixo -> None."""
    if len(numero) == 13 and numero[4] == "9":
        return numero[:4] + numero[5:]
    if len(numero) == 12 and numero[4] not in "2345":  # 2 a 5: telefone fixo, sem o 9
        return numero[:4] + "9" + numero[4:]
    return None


class EnviadorWhatsApp:
    def __init__(self, token: str, phone_number_id: str, *, transporte: httpx.BaseTransport | None = None) -> None:
        if not token.strip() or not phone_number_id.strip():
            raise ValueError("WHATSAPP_TOKEN e WHATSAPP_PHONE_NUMBER_ID são obrigatórios")
        self._cliente = httpx.Client(
            base_url=f"{URL_BASE}/{VERSAO}/{phone_number_id.strip()}",
            headers={"Authorization": f"Bearer {token.strip()}"},
            timeout=ESPERA_MAXIMA,
            follow_redirects=False,
            transport=transporte,
        )
        self._forma_que_funciona: dict[str, str] = {}

    def enviar(self, para: str, texto: str) -> bool:
        destino = self._forma_que_funciona.get(para, para)
        status, codigo = self._postar(destino, texto)
        if status == 200:
            return True
        alternativa = outra_forma(destino)
        if codigo == FORA_DA_LISTA and alternativa is not None:
            status, codigo = self._postar(alternativa, texto)
            if status == 200:
                self._forma_que_funciona[para] = alternativa
                return True
        self._registrar_falha(status, codigo)
        return False

    def _postar(self, para: str, texto: str) -> tuple[int | None, int | None]:
        """(status HTTP, código de erro da Meta). Falha de rede: (None, None)."""
        corpo = {"messaging_product": "whatsapp", "to": para, "type": "text", "text": {"body": texto}}
        try:
            resposta = self._cliente.post("/messages", json=corpo)
        except httpx.HTTPError as erro:
            _log.warning("envio pelo WhatsApp falhou: %s", type(erro).__name__)
            return None, None
        if resposta.status_code == 200:
            return 200, None
        try:
            codigo = resposta.json().get("error", {}).get("code")
        except (ValueError, AttributeError):
            codigo = None
        return resposta.status_code, codigo if isinstance(codigo, int) else None

    @staticmethod
    def _registrar_falha(status: int | None, codigo: int | None) -> None:
        if status is None:
            return  # já registrado em _postar
        if status == 401:
            _log.warning("token do WhatsApp recusado (401): gere outro ou use o permanente (W13)")
        else:
            _log.warning("envio pelo WhatsApp recusado: HTTP %s, código %s", status, codigo)

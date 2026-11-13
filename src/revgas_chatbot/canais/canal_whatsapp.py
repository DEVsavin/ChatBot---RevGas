"""Canal WhatsApp: o webhook que recebe as mensagens da Meta.

Spec: `.scratch/whatsapp/spec.md`. Este servidor é o ÚNICO que vai para a
internet (pelo ngrok), por isso tem só a rota `/webhook` (W5): a página do
chat e a fila da equipe nunca passam pelo túnel.

Antes de ler qualquer coisa do corpo, o `POST /webhook` confere:
1. o cabeçalho `Host` (o domínio do ngrok ou esta máquina);
2. o tamanho (até 256 KB);
3. a assinatura `X-Hub-Signature-256` (HMAC-SHA256 do corpo cru com o
   segredo do app, comparada em tempo constante).
Só então separa as mensagens e entrega cada uma, uma vez só (W7), a
`receber`. Nada do conteúdo vai para o log.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from revgas_chatbot.conversa.entrada import telefone

PORTA = 8001  # WA-D1 (a 8000 é do chat web)
MAX_CORPO = 256 * 1024  # bytes
IDS_LEMBRADOS = 5000  # W7: ids de mensagens já recebidas (em memória)


@dataclass(frozen=True)
class MensagemRecebida:
    telefone: str  # wa_id como chegou (55 + DDD + número, às vezes sem o 9)
    id: str  # wamid
    texto: str | None  # None = não é texto (áudio, imagem, localização...)


def _assinatura_valida(corpo: bytes, cabecalho: str | None, segredo: str) -> bool:
    if not cabecalho or not cabecalho.startswith("sha256="):
        return False
    esperada = hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(cabecalho[len("sha256="):].encode(), esperada.encode())


def _iguais(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


def _mensagens(dados: Any, phone_number_id: str) -> Iterator[MensagemRecebida]:
    """Separa as mensagens do evento da Meta. Ignora o que não é para nós."""
    if not isinstance(dados, dict) or dados.get("object") != "whatsapp_business_account":
        return
    for entrada in dados.get("entry") or []:
        if not isinstance(entrada, dict):
            continue
        for mudanca in entrada.get("changes") or []:
            if not isinstance(mudanca, dict) or mudanca.get("field") != "messages":
                continue
            valor = mudanca.get("value")
            if not isinstance(valor, dict):
                continue
            metadados = valor.get("metadata")
            if not isinstance(metadados, dict) or metadados.get("phone_number_id") != phone_number_id:
                continue  # não é o nosso número
            # `statuses` (entregue, lida...) não interessam (W9).
            for m in valor.get("messages") or []:
                recebida = _mensagem(m)
                if recebida is not None:
                    yield recebida


def _mensagem(m: Any) -> MensagemRecebida | None:
    if not isinstance(m, dict):
        return None
    de, id_, tipo = m.get("from"), m.get("id"), m.get("type")
    if not isinstance(de, str) or not isinstance(id_, str) or not id_:
        return None
    if telefone(de) != de:
        return None  # só números do Brasil, já no formato 55 + DDD + número
    if tipo == "reaction":
        return None  # um emoji de reação não pede resposta
    if tipo == "text":
        corpo = (m.get("text") or {}).get("body") if isinstance(m.get("text"), dict) else None
        if not isinstance(corpo, str):
            return None
        return MensagemRecebida(telefone=de, id=id_, texto=corpo)
    return MensagemRecebida(telefone=de, id=id_, texto=None)


class _IdsVistos:
    """Os últimos N ids recebidos. `novo(id)` marca e diz se ainda não tinha visto."""

    def __init__(self, limite: int) -> None:
        self._limite = limite
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._trava = threading.Lock()

    def novo(self, id_: str) -> bool:
        with self._trava:
            if id_ in self._ids:
                return False
            self._ids[id_] = None
            if len(self._ids) > self._limite:
                self._ids.popitem(last=False)
            return True


def criar_app_whatsapp(
    *,
    receber: Callable[[MensagemRecebida], None],
    app_secret: str,
    verify_token: str,
    phone_number_id: str,
    dominio: str,
    porta: int = PORTA,
    ids_lembrados: int = IDS_LEMBRADOS,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts_permitidos = {dominio, f"127.0.0.1:{porta}", f"localhost:{porta}"}
    vistos = _IdsVistos(ids_lembrados)

    @app.middleware("http")
    async def so_hosts_conhecidos(request: Request, proximo):
        if request.headers.get("host") not in hosts_permitidos:
            return JSONResponse({"erro": "Endereço não permitido."}, status_code=400)
        return await proximo(request)

    @app.get("/webhook")
    def verificar(request: Request):
        p = request.query_params
        if p.get("hub.mode") == "subscribe" and _iguais(p.get("hub.verify_token"), verify_token):
            return PlainTextResponse(p.get("hub.challenge", ""))
        return Response(status_code=403)

    @app.post("/webhook")
    async def receber_evento(request: Request):
        tamanho = request.headers.get("content-length")
        if tamanho is not None and (not tamanho.isdigit() or int(tamanho) > MAX_CORPO):
            return Response(status_code=413)
        corpo = bytearray()
        async for pedaco in request.stream():
            corpo += pedaco
            if len(corpo) > MAX_CORPO:
                return Response(status_code=413)
        corpo = bytes(corpo)

        if not _assinatura_valida(corpo, request.headers.get("x-hub-signature-256"), app_secret):
            return Response(status_code=403)

        # Daqui em diante, sempre 200: o evento é da Meta, e um erro a faria reenviar sem fim.
        try:
            dados = json.loads(corpo)
        except ValueError:
            return Response(status_code=200)
        for mensagem in _mensagens(dados, phone_number_id):
            if vistos.novo(mensagem.id):  # W7: marcado antes de processar, sob trava
                receber(mensagem)
        return Response(status_code=200)

    return app

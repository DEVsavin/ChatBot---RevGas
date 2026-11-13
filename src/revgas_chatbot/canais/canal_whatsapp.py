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
`receber`, que só enfileira. O `AtendimentoWhatsApp` atende a fila num
único trabalhador, na ordem (S5, W8): conversa → respostas → envio.
Nada do conteúdo vai para o log.

    uv run --env-file .env python -m revgas_chatbot.canais.canal_whatsapp
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from revgas_chatbot.conversa.entrada import comando, numero_ficticio, telefone

HOST = "127.0.0.1"  # só esta máquina; o ngrok leva o tráfego até aqui (W5)
MAX_MENSAGEM = 500  # mesmo limite da web
MAX_EQUIPE = 5  # destinatários de teste da Meta (W12)
MIN_VERIFY_TOKEN = 32

NAO_TEXTO = "Por enquanto só entendo mensagens de texto."
LONGA = "Sua mensagem ficou longa demais. Pode resumir em até 500 caracteres?"
PROBLEMA = "Tive um problema aqui. Pode mandar de novo?"

_log = logging.getLogger(__name__)

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


# --- atendimento: um trabalhador, uma mensagem por vez -------------------------------------


class Enviador(Protocol):
    def enviar(self, para: str, texto: str) -> bool: ...


class _Conversa(Protocol):
    encerrada: bool

    def iniciar(self) -> list[str]: ...

    def responder(self, texto: str) -> list[str]: ...


_PARAR = object()


class AtendimentoWhatsApp:
    """Atende as mensagens recebidas, uma de cada vez, na ordem de chegada."""

    def __init__(self, criar_conversa: Callable[[str], _Conversa], enviador: Enviador) -> None:
        self._criar_conversa = criar_conversa
        self._enviador = enviador
        self._fila: queue.Queue = queue.Queue()
        self._conversas: dict[str, _Conversa] = {}  # W8: uma por telefone
        self._trabalhador: threading.Thread | None = None

    def receber(self, mensagem: MensagemRecebida) -> None:
        """Chamado pela rota do webhook: só enfileira (rápido e sem exceção)."""
        self._fila.put_nowait(mensagem)

    def ligar(self) -> None:
        self._trabalhador = threading.Thread(target=self._trabalhar, name="whatsapp", daemon=True)
        self._trabalhador.start()

    def desligar(self) -> None:
        if self._trabalhador is not None:
            self._fila.put_nowait(_PARAR)
            self._trabalhador.join()
            self._trabalhador = None

    def esperar(self, limite: float = 5.0) -> None:
        """Espera a fila esvaziar (testes). Trabalhador parado → erro em vez de travar."""
        terminou = self._fila.all_tasks_done
        with terminou:
            if not terminou.wait_for(lambda: self._fila.unfinished_tasks == 0, timeout=limite):
                raise TimeoutError("o atendimento do WhatsApp não terminou")

    def _trabalhar(self) -> None:
        while True:
            mensagem = self._fila.get()
            try:
                if mensagem is _PARAR:
                    return
                self._atender(mensagem)
            except Exception as erro:  # noqa: BLE001 - I3: o trabalhador nunca morre
                _log.error("falha ao atender uma mensagem do WhatsApp: %s", type(erro).__name__)
                try:
                    self._enviador.enviar(mensagem.telefone, PROBLEMA)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._fila.task_done()

    def _atender(self, m: MensagemRecebida) -> None:
        if m.texto is None:
            self._enviar(m.telefone, [NAO_TEXTO])  # W9
            return
        if len(m.texto) > MAX_MENSAGEM:
            self._enviar(m.telefone, [LONGA])
            return
        conversa = self._conversas.get(m.telefone)
        if conversa is None or conversa.encerrada:
            conversa = self._criar_conversa(m.telefone)
            respostas = conversa.iniciar()
            self._conversas[m.telefone] = conversa
            # I1: vazamento ou pedido de atendente logo na primeira mensagem → atende já.
            if comando(m.texto, livre=False) in ("VAZAMENTO", "ATENDENTE"):
                respostas = conversa.responder(m.texto)
        else:
            respostas = conversa.responder(m.texto)
        self._enviar(m.telefone, respostas)

    def _enviar(self, para: str, respostas: list[str]) -> None:
        for texto in respostas:
            if not self._enviador.enviar(para, texto):
                return  # a ordem importa: sem esta, as seguintes não fazem sentido


# --- IA só para a equipe (W2) ----------------------------------------------------------------


def _mesmo_celular(numero: str) -> str:
    """55 + DDD + os últimos 8 dígitos: iguala as formas com e sem o 9."""
    return numero[:4] + numero[-8:]


def regra_da_ia(numeros_equipe: list[str] | tuple[str, ...]) -> Callable[[str], bool]:
    """IA permitida para números fictícios (DDD 00) ou da equipe (com ou sem o 9)."""
    equipe = set()
    for bruto in numeros_equipe:
        numero = telefone(bruto)
        if numero is None:
            raise ValueError("número inválido na lista da equipe")
        equipe.add(_mesmo_celular(numero))

    def permitida(numero: str) -> bool:
        return numero_ficticio(numero) or _mesmo_celular(numero) in equipe

    return permitida


# --- configuração e comando ------------------------------------------------------------------


class ConfiguracaoInvalida(ValueError):
    """Mensagem com o NOME da variável, nunca o valor."""


@dataclass(frozen=True)
class Configuracao:
    token: str = field(repr=False)
    phone_number_id: str
    app_secret: str = field(repr=False)
    verify_token: str = field(repr=False)
    numeros_equipe: tuple[str, ...] = field(repr=False)
    dominio: str


_OBRIGATORIAS = (
    "WHATSAPP_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_NUMEROS_EQUIPE",
    "WHATSAPP_DOMINIO",
)


def configuracao(ambiente: Mapping[str, str]) -> Configuracao:
    valores = {nome: (ambiente.get(nome) or "").strip() for nome in _OBRIGATORIAS}
    faltam = [nome for nome, valor in valores.items() if not valor]
    if faltam:
        raise ConfiguracaoInvalida("faltam no .env: " + ", ".join(faltam))
    if len(valores["WHATSAPP_VERIFY_TOKEN"]) < MIN_VERIFY_TOKEN:
        raise ConfiguracaoInvalida(f"WHATSAPP_VERIFY_TOKEN precisa ter pelo menos {MIN_VERIFY_TOKEN} caracteres")
    brutos = [n for n in valores["WHATSAPP_NUMEROS_EQUIPE"].split(",") if n.strip()]
    numeros = tuple(telefone(n) for n in brutos)
    if not numeros or len(numeros) > MAX_EQUIPE or None in numeros:
        raise ConfiguracaoInvalida(
            f"WHATSAPP_NUMEROS_EQUIPE: de 1 a {MAX_EQUIPE} telefones com DDD, separados por vírgula"
        )
    dominio = valores["WHATSAPP_DOMINIO"]
    if "/" in dominio or ":" in dominio:
        raise ConfiguracaoInvalida("WHATSAPP_DOMINIO: só o domínio, sem https:// e sem barra")
    return Configuracao(
        token=valores["WHATSAPP_TOKEN"],
        phone_number_id=valores["WHATSAPP_PHONE_NUMBER_ID"],
        app_secret=valores["WHATSAPP_APP_SECRET"],
        verify_token=valores["WHATSAPP_VERIFY_TOKEN"],
        numeros_equipe=numeros,  # type: ignore[arg-type]
        dominio=dominio,
    )


def main(
    argv: list[str] | None = None,
    *,
    rodar: Callable[..., None] | None = None,  # testes: um falso no lugar do uvicorn
    pasta_dados: Path = Path("dados"),
    ambiente: Mapping[str, str] | None = None,
) -> None:
    """Liga o webhook do WhatsApp em http://127.0.0.1:<porta>/webhook."""
    import os

    from revgas_chatbot.canais.enviador_whatsapp import EnviadorWhatsApp
    from revgas_chatbot.montagem import montar

    parser = argparse.ArgumentParser(description="Webhook do WhatsApp (só nesta máquina; o ngrok expõe)")
    parser.add_argument("--porta", type=int, default=PORTA, help=f"porta (padrão {PORTA})")
    argumentos = parser.parse_args(argv)

    ambiente = os.environ if ambiente is None else ambiente
    try:
        config = configuracao(ambiente)
    except ConfiguracaoInvalida as erro:
        raise SystemExit(f"WhatsApp não iniciou: {erro}") from None
    if rodar is None:
        import uvicorn

        rodar = uvicorn.run

    programa = montar(pasta_dados, ambiente=ambiente, ia_permitida=regra_da_ia(config.numeros_equipe))
    atendimento = AtendimentoWhatsApp(
        lambda tel: programa(tel, numero_confiavel=True),  # W4: o número vem da Meta
        EnviadorWhatsApp(config.token, config.phone_number_id),
    )
    app = criar_app_whatsapp(
        receber=atendimento.receber,
        app_secret=config.app_secret,
        verify_token=config.verify_token,
        phone_number_id=config.phone_number_id,
        dominio=config.dominio,
        porta=argumentos.porta,
    )
    print(
        f"WhatsApp: webhook em http://{HOST}:{argumentos.porta}/webhook "
        f"(ligue o ngrok para https://{config.dominio})",
        flush=True,
    )
    print("IA: " + ("ligada só para os números da equipe" if programa.ia_ligada else "desligada"), flush=True)
    atendimento.ligar()
    try:
        # access_log=False: nada de caminhos e endereços no log (W10).
        rodar(app, host=HOST, port=argumentos.porta, access_log=False)
    finally:
        atendimento.desligar()


if __name__ == "__main__":
    main()

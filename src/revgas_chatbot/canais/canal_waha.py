"""Canal WhatsApp pelo WAHA: o webhook que recebe as mensagens do chip.

Spec: `.scratch/whatsapp/spec-waha.md`. O WAHA roda no Docker desta máquina
(`docker-compose.waha.yml`) e avisa cada mensagem nova por webhook. Este
servidor escuta só em 127.0.0.1:8002 e tem só a rota `/webhook`.

Antes de ler o corpo: `Host`, tamanho (até 256 KB) e assinatura
`X-Webhook-Hmac` (HMAC-SHA512 do corpo cru com `WAHA_HMAC_KEY`). Depois, só
passa mensagem de texto de uma pessoa (nada de grupo, status, canal ou do
próprio chip), de número brasileiro atendido, recente e ainda não vista.
O atendimento fica em `atendimento_whatsapp.py`.

    ./scripts/whatsapp-waha.sh            (sobe o WAHA e este servidor)
    uv run --env-file .env python -m revgas_chatbot.canais.canal_waha [--venda-real]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from revgas_chatbot.api_revgas.api import Relogio
from revgas_chatbot.canais.atendimento_whatsapp import AtendimentoWhatsApp, IdsVistos, MensagemRecebida
from revgas_chatbot.canais.cliente_waha import ClienteWaha, _url_local, digitos_do_chat
from revgas_chatbot.conversa.entrada import telefone
from revgas_chatbot.montagem import (
    AVISO_VENDA_REAL,
    ConfiguracaoInvalida,
    montar_do_ambiente,
    numeros_da_equipe,
    regra_da_ia,
)

__all__ = ["ConfiguracaoInvalida", "configuracao_waha", "criar_app_waha", "main"]

HOST = "127.0.0.1"
PORTA = 8002  # D-WAHA-2 (8000 = chat web)
MAX_CORPO = 256 * 1024
IDADE_MAXIMA = 10 * 60  # segundos: mensagem mais velha que isso é ignorada
MIN_HMAC = 32
PAINEL = "http://127.0.0.1:3000/dashboard"

_IGNORADOS = ("@g.us", "@broadcast", "@newsletter")  # grupo, status/lista, canal

_log = logging.getLogger(__name__)


class _Cliente(Protocol):
    def lembrar(self, telefone_: str, chat_id: str) -> None: ...

    def numero_do_lid(self, lid: str) -> str | None: ...


def criar_app_waha(
    *,
    receber: Callable[[MensagemRecebida], None],
    cliente: _Cliente,
    hmac_key: str,
    sessao: str,
    atende: Callable[[str], bool],
    relogio: Relogio,
    porta: int = PORTA,
    ids_lembrados: int = 5000,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hosts_permitidos = {f"host.docker.internal:{porta}", f"127.0.0.1:{porta}", f"localhost:{porta}"}
    vistos = IdsVistos(ids_lembrados)

    @app.middleware("http")
    async def so_hosts_conhecidos(request: Request, proximo):
        if request.headers.get("host") not in hosts_permitidos:
            return JSONResponse({"erro": "Endereço não permitido."}, status_code=400)
        return await proximo(request)

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

        esperada = hmac.new(hmac_key.encode(), corpo, hashlib.sha512).hexdigest()
        if not hmac.compare_digest((request.headers.get("x-webhook-hmac") or "").encode(), esperada.encode()):
            return Response(status_code=403)

        # Daqui em diante, sempre 200: um erro faria o WAHA reenviar.
        try:
            dados = json.loads(corpo)
        except ValueError:
            return Response(status_code=200)
        mensagem = _mensagem(dados, sessao, cliente, atende, relogio)
        if mensagem is not None and vistos.novo(mensagem.id):  # W7: marcado antes de processar
            receber(mensagem)
        return Response(status_code=200)

    return app


def _mensagem(
    dados: Any, sessao: str, cliente: _Cliente, atende: Callable[[str], bool], relogio: Relogio
) -> MensagemRecebida | None:
    if not isinstance(dados, dict) or dados.get("event") != "message" or dados.get("session") != sessao:
        return None
    p = dados.get("payload")
    if not isinstance(p, dict) or p.get("fromMe") is not False:
        return None  # sem "fromMe: false" explícito, pode ser o próprio chip: ignora
    chat_id, id_ = p.get("from"), p.get("id")
    if not isinstance(chat_id, str) or not isinstance(id_, str) or not id_:
        return None
    if chat_id.endswith(_IGNORADOS):
        return None
    if chat_id.endswith("@lid"):
        numero = cliente.numero_do_lid(chat_id)
        if numero is None:
            _log.info("mensagem de um lid sem número conhecido: ignorada")
            return None
    else:
        numero = digitos_do_chat(chat_id)
    if numero is None or telefone(numero) != numero or not atende(numero):
        return None
    instante = p.get("timestamp")
    if not isinstance(instante, (int, float)):
        return None
    idade = relogio.agora().timestamp() - instante
    if idade > IDADE_MAXIMA:
        return None  # ao reconectar, o WAHA pode entregar mensagens antigas
    cliente.lembrar(numero, chat_id)  # a resposta vai para o mesmo chat
    corpo = p.get("body")
    texto = corpo if isinstance(corpo, str) and corpo.strip() and not p.get("hasMedia") else None
    return MensagemRecebida(telefone=numero, id=id_, texto=texto)


# --- configuração e comando ------------------------------------------------------------------


@dataclass(frozen=True)
class ConfiguracaoWaha:
    url: str
    sessao: str
    chave: str = field(repr=False)
    hmac_key: str = field(repr=False)
    atender: str  # "equipe" ou "todos" (D-WAHA-1)
    numeros_equipe: tuple[str, ...] = field(repr=False)

    def atende(self, numero: str) -> bool:
        return self.atender == "todos" or regra_da_ia(self.numeros_equipe)(numero)


def configuracao_waha(ambiente: Mapping[str, str]) -> ConfiguracaoWaha:
    valor = {n: (ambiente.get(n) or "").strip() for n in ("WAHA_API_KEY", "WAHA_HMAC_KEY")}
    faltam = [nome for nome, v in valor.items() if not v]
    if faltam:
        raise ConfiguracaoInvalida("faltam no .env: " + ", ".join(faltam))
    if len(valor["WAHA_HMAC_KEY"]) < MIN_HMAC:
        raise ConfiguracaoInvalida(f"WAHA_HMAC_KEY precisa ter pelo menos {MIN_HMAC} caracteres")
    url = (ambiente.get("WAHA_URL") or "").strip() or "http://127.0.0.1:3000"
    try:
        url = _url_local(url)
    except ValueError:
        raise ConfiguracaoInvalida("WAHA_URL precisa ser local, ex.: http://127.0.0.1:3000") from None
    atender = (ambiente.get("WAHA_ATENDER") or "").strip().lower() or "equipe"
    if atender not in ("equipe", "todos"):
        raise ConfiguracaoInvalida("WAHA_ATENDER: use equipe ou todos")
    numeros = numeros_da_equipe(ambiente)
    if atender == "equipe" and not numeros:
        raise ConfiguracaoInvalida("faltam no .env: WHATSAPP_NUMEROS_EQUIPE (com WAHA_ATENDER=equipe)")
    return ConfiguracaoWaha(
        url=url,
        sessao=(ambiente.get("WAHA_SESSAO") or "").strip() or "default",
        chave=valor["WAHA_API_KEY"],
        hmac_key=valor["WAHA_HMAC_KEY"],
        atender=atender,
        numeros_equipe=numeros,
    )


def main(
    argv: list[str] | None = None,
    *,
    rodar: Callable[..., None] | None = None,  # testes: um falso no lugar do uvicorn
    pasta_dados: Path = Path("dados"),
    ambiente: Mapping[str, str] | None = None,
) -> None:
    """Liga o webhook do WAHA em http://127.0.0.1:8002/webhook."""
    import os

    from revgas_chatbot.relogio import RelogioDoSistema

    parser = argparse.ArgumentParser(description="Webhook do WAHA (WhatsApp pelo chip, só nesta máquina)")
    parser.add_argument("--porta", type=int, default=PORTA, help=f"porta (padrão {PORTA})")
    parser.add_argument("--venda-real", action="store_true",
                        help="cria vendas DE VERDADE no estabelecimento 2580 (só o usuário liga; M5)")
    argumentos = parser.parse_args(argv)

    ambiente = os.environ if ambiente is None else ambiente
    try:
        config = configuracao_waha(ambiente)
        if argumentos.venda_real and config.atender != "equipe":
            # I2: com "todos", qualquer contato do chip criaria venda real.
            raise ConfiguracaoInvalida("--venda-real só funciona com WAHA_ATENDER=equipe")
        # I3: a IA continua só para fictícios e equipe, qualquer que seja WAHA_ATENDER.
        programa = montar_do_ambiente(pasta_dados, ambiente, venda_real=argumentos.venda_real)
    except ConfiguracaoInvalida as erro:
        raise SystemExit(f"WAHA não iniciou: {erro}") from None
    if rodar is None:
        import uvicorn

        rodar = uvicorn.run

    cliente = ClienteWaha(config.url, config.chave, config.sessao)
    atendimento = AtendimentoWhatsApp(lambda tel: programa(tel, numero_confiavel=True), cliente)
    app = criar_app_waha(
        receber=atendimento.receber,
        cliente=cliente,
        hmac_key=config.hmac_key,
        sessao=config.sessao,
        atende=config.atende,
        relogio=RelogioDoSistema(),
        porta=argumentos.porta,
    )
    print(f"WAHA: webhook em http://{HOST}:{argumentos.porta}/webhook", flush=True)
    print(f"QR code e status da sessão: {PAINEL} (usuário e senha: WAHA_DASHBOARD_* no .env)", flush=True)
    print("Atende: " + ("só os números da equipe" if config.atender == "equipe" else "qualquer número"), flush=True)
    print("IA: " + ("ligada só para os números da equipe" if programa.ia_ligada else "desligada"), flush=True)
    if programa.venda_real:
        print(f"*** {AVISO_VENDA_REAL} ***", flush=True)
    atendimento.ligar()
    try:
        rodar(app, host=HOST, port=argumentos.porta, access_log=False)  # sem log de acesso (W10)
    finally:
        atendimento.desligar()


if __name__ == "__main__":
    main()

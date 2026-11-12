"""Canal web: o chat no navegador, só nesta máquina (127.0.0.1).

O servidor só leva e traz texto entre a página e a `Conversa` (o motor é o
mesmo do terminal). Regras do canal (`.scratch/canal/spec.md`):

- só local: o cabeçalho `Host` precisa ser 127.0.0.1:<porta> ou
  localhost:<porta>, em TODAS as rotas (contra "DNS rebinding": um site
  malicioso ler a fila ou mandar mensagens);
- `POST` só com `Content-Type: application/json` (um formulário de outro
  site não consegue enviar sem a permissão do navegador);
- uma mensagem de cada vez (um cadeado em volta da conversa);
- número fictício (DDD 00) = número do canal, com IA; qualquer outro =
  número digitado (não revela o cadastro) e SEM a IA da cota grátis;
- nenhum texto do cliente vai para o log.
"""

from __future__ import annotations

import secrets
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from revgas_chatbot.conversa import Conversa
from revgas_chatbot.entrada import numero_ficticio, telefone
from revgas_chatbot.montagem import Programa

PORTA = 8000  # W-D1
MAX_MENSAGEM = 500  # W-D2

AVISO_SEM_IA = (
    "IA desligada para este número: na demonstração, a IA só funciona com os números fictícios."
)


class NovaConversa(BaseModel):
    telefone: str


class Mensagem(BaseModel):
    texto: str


def _erro(status: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"erro": mensagem}, status_code=status)


def criar_app(programa: Programa, *, porta: int = PORTA) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # sem páginas extras
    hosts_permitidos = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
    cadeado = threading.Lock()  # uma mensagem de cada vez (CN-3)
    sessoes: dict[str, Conversa] = {}

    @app.middleware("http")
    async def so_local(request: Request, proximo):
        if request.headers.get("host") not in hosts_permitidos:
            return _erro(400, "Endereço não permitido.")
        if request.method == "POST":
            tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if tipo != "application/json":
                return _erro(415, "Envie os dados como JSON.")
        return await proximo(request)

    @app.post("/conversas")
    def nova_conversa(corpo: NovaConversa):
        numero = telefone(corpo.telefone)
        if numero is None:
            return _erro(400, "Número inválido. Use DDD e número, ex.: (00) 90000-0001.")
        ficticio = numero_ficticio(numero)
        com_ia = programa.ia_ligada and ficticio
        conversa = programa(numero, numero_confiavel=ficticio, com_ia=com_ia)
        with cadeado:
            mensagens = conversa.iniciar()
        sessao = secrets.token_urlsafe(24)  # 192 bits aleatórios: não dá para adivinhar
        sessoes[sessao] = conversa
        return {
            "sessao": sessao,
            "mensagens": mensagens,
            "ia_ligada": com_ia,
            "aviso": AVISO_SEM_IA if programa.ia_ligada and not com_ia else None,
        }

    @app.post("/conversas/{sessao}/mensagens")
    def nova_mensagem(sessao: str, corpo: Mensagem):
        conversa = sessoes.get(sessao)
        if conversa is None:
            return _erro(404, "Conversa não encontrada. Comece de novo.")
        if len(corpo.texto) > MAX_MENSAGEM:
            return _erro(400, f"Mensagem longa demais (máximo {MAX_MENSAGEM} caracteres).")
        with cadeado:
            return {"mensagens": conversa.responder(corpo.texto)}

    return app

"""Canal web: o chat no navegador, só nesta máquina (127.0.0.1).

O servidor só leva e traz texto entre a página e a `Conversa` (o motor é o
mesmo do terminal). Regras do canal (`.scratch/canal/spec.md`):

- só local: o cabeçalho `Host` precisa ser 127.0.0.1:<porta> ou
  localhost:<porta>, em TODAS as rotas (contra "DNS rebinding": um site
  malicioso ler a fila ou mandar mensagens);
- `POST` só com `Content-Type: application/json` (um formulário de outro
  site não consegue enviar sem a permissão do navegador);
- uma mensagem de cada vez (um cadeado em volta da conversa);
- número fictício (DDD 00) = número do canal; qualquer outro = número
  digitado (não revela o cadastro);
- venda real (M5) só com `--venda-real`: a página avisa;
- nenhum texto do cliente vai para o log.
"""

from __future__ import annotations

import argparse
import secrets
import threading
from pathlib import Path
from typing import Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from revgas_chatbot.conversa.conversa import Conversa
from revgas_chatbot.conversa.entrada import comando, numero_ficticio, pedido_de_devolucao, telefone
from revgas_chatbot.montagem import (
    AVISO_ENTREGADORES,
    AVISO_VENDA_REAL,
    ConfiguracaoInvalida,
    Programa,
    confirmar_aviso,
    montar_do_ambiente,
)

PORTA = 8000  # W-D1
MAX_MENSAGEM = 500  # W-D2

HOST = "127.0.0.1"  # só esta máquina; não há opção para mudar (CN-3, CN-4)
PAGINA_DO_CHAT = (Path(__file__).with_name("pagina_chat.html")).read_text(encoding="utf-8")


class NovaConversa(BaseModel):
    telefone: str


class Mensagem(BaseModel):
    texto: str


def _erro(status: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"erro": mensagem}, status_code=status)


def criar_app(programa: Programa, *, porta: int = PORTA, pasta_dados: Path = Path("dados")) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # sem páginas extras
    hosts_permitidos = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
    cadeado = threading.Lock()  # uma mensagem de cada vez (CN-3)
    sessoes: dict[str, dict] = {}

    @app.middleware("http")
    async def so_local(request: Request, proximo):
        if request.headers.get("host") not in hosts_permitidos:
            return _erro(400, "Endereço não permitido.")
        if request.method == "POST":
            tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if tipo != "application/json":
                return _erro(415, "Envie os dados como JSON.")
        return await proximo(request)

    @app.get("/", response_class=HTMLResponse)
    def pagina_do_chat():
        return PAGINA_DO_CHAT

    @app.get("/api-logs")
    def listar_logs_api():
        return {"venda_real": programa.venda_real, "logs": programa.monitor_api.listar()}

    @app.post("/api-logs/limpar")
    def limpar_logs_api():
        programa.monitor_api.limpar()
        return {"ok": True}

    @app.post("/conversas")
    def nova_conversa(corpo: NovaConversa):
        numero = telefone(corpo.telefone)
        if numero is None:
            return _erro(400, "Número inválido. Use DDD e número, ex.: (00) 90000-0001.")
        ficticio = numero_ficticio(numero)
        conversa = programa(numero, numero_confiavel=ficticio or programa.venda_real)
        sessao = secrets.token_urlsafe(24)  # 192 bits aleatórios: não dá para adivinhar
        sessoes[sessao] = {"conversa": conversa, "iniciada": False}
        avisos = [AVISO_VENDA_REAL] if programa.venda_real else []
        if programa.avisar_entregadores:
            avisos.append(AVISO_ENTREGADORES)
        return {
            "sessao": sessao,
            "mensagens": [],
            "venda_real": programa.venda_real,
            "aviso": " ".join(avisos) or None,
        }

    @app.post("/conversas/{sessao}/mensagens")
    def nova_mensagem(sessao: str, corpo: Mensagem):
        estado = sessoes.get(sessao)
        if estado is None:
            return _erro(404, "Conversa não encontrada. Comece de novo.")
        if len(corpo.texto) > MAX_MENSAGEM:
            return _erro(400, f"Mensagem longa demais (máximo {MAX_MENSAGEM} caracteres).")
        conversa: Conversa = estado["conversa"]
        with cadeado:
            if not estado["iniciada"]:
                estado["iniciada"] = True
                cmd = comando(corpo.texto, livre=False)
                if cmd in (
                    "VAZAMENTO",
                    "ATENDENTE",
                    "MEUS_PEDIDOS",
                    "ACOMPANHAR_PEDIDO",
                    "CONSULTAR_PRECO",
                    "REPETIR_PEDIDO",
                ) or pedido_de_devolucao(corpo.texto):
                    mensagens = conversa.responder(corpo.texto)
                else:
                    mensagens = conversa.iniciar()
            else:
                mensagens = conversa.responder(corpo.texto)
            return {"mensagens": mensagens}

    return app


def main(
    argv: list[str] | None = None,
    *,
    rodar: Callable[..., None] | None = None,  # testes: um falso no lugar do uvicorn
    pasta_dados: Path = Path("dados"),
    ambiente: Mapping[str, str] | None = None,
    ler: Callable[[str], str] = input,  # testes: a resposta ao "digite AVISAR"
) -> None:
    """Liga o chat web em http://127.0.0.1:<porta> (só nesta máquina)."""
    parser = argparse.ArgumentParser(description="Chat web de teste (só nesta máquina)")
    parser.add_argument("--porta", type=int, default=PORTA, help=f"porta (padrão {PORTA})")
    parser.add_argument("--venda-real", action="store_true",
                        help="cria vendas DE VERDADE no estabelecimento 2580 (só o usuário liga; M5)")
    parser.add_argument("--avisar-entregadores", action="store_true",
                        help="só com --venda-real: AVISA os entregadores (broadcast NEW_SALE); pede AVISAR")
    argumentos = parser.parse_args(argv)

    if rodar is None:
        import uvicorn

        rodar = uvicorn.run
    import os

    ambiente = os.environ if ambiente is None else ambiente
    try:
        programa = montar_do_ambiente(pasta_dados, ambiente, venda_real=argumentos.venda_real,
                                      avisar_entregadores=argumentos.avisar_entregadores)
        if programa.avisar_entregadores:
            confirmar_aviso(ler, lambda t: print(t, flush=True))
    except ConfiguracaoInvalida as erro:
        raise SystemExit(f"Chat web não iniciou: {erro}") from None
    app = criar_app(programa, porta=argumentos.porta, pasta_dados=pasta_dados)
    if programa.venda_real:
        print(f"*** {AVISO_VENDA_REAL} ***", flush=True)
    print(f"Chat de teste em http://{HOST}:{argumentos.porta}  (Ctrl+C para parar)", flush=True)
    # access_log=False: o caminho das mensagens tem o identificador da sessão.
    rodar(app, host=HOST, port=argumentos.porta, access_log=False)


if __name__ == "__main__":
    main()

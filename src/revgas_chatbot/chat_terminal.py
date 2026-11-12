"""Chat de teste no terminal: só repassa texto entre a pessoa e a Conversa.

    uv run python -m revgas_chatbot.chat_terminal [--telefone 5500900000001]

Para ligar a IA (Gemini, cota grátis), rode com a chave no ambiente:

    uv run --env-file .env python -m revgas_chatbot.chat_terminal --telefone 5500900000001

O telefone simula o número que o WhatsApp entregaria. A fila de
atendimento e a página `fila.html` ficam em `dados/` (fora do git).
Ctrl+D (ou Ctrl+C) sai do chat.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping, TextIO

from revgas_chatbot.api import ApiRevGas, Relogio
from revgas_chatbot.entrada import telefone
from revgas_chatbot.montagem import montar


def _ler(entrada: TextIO, saida: TextIO, rotulo: str) -> str | None:
    saida.write(rotulo)
    saida.flush()
    linha = entrada.readline()
    if not linha:  # fim da entrada (Ctrl+D)
        saida.write("\n")
        return None
    return linha.rstrip("\n")


def main(
    argv: list[str] | None = None,
    entrada: TextIO = sys.stdin,
    saida: TextIO = sys.stdout,
    *,
    relogio: Relogio | None = None,
    api: ApiRevGas | None = None,
    pasta_dados: Path = Path("dados"),
    ambiente: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Chat de teste do atendimento")
    parser.add_argument("--telefone", help="número simulado do WhatsApp, ex.: 5500900000001")
    argumentos = parser.parse_args(argv)

    numero = telefone(argumentos.telefone) if argumentos.telefone else None
    while numero is None:
        digitado = _ler(entrada, saida, "Telefone do WhatsApp simulado (ex.: 5500900000001): ")
        if digitado is None:
            return 0
        numero = telefone(digitado)
        if numero is None:
            saida.write("Número inválido. Use DDD e número, ex.: (00) 90000-0001.\n")

    programa = montar(pasta_dados, relogio=relogio, api=api, ambiente=ambiente)
    conversa = programa(numero)
    saida.write("(Chat de teste. Ctrl+D para sair.)\n")
    if programa.ia_ligada:
        saida.write("(IA ligada: Gemini, cota grátis. Use só os clientes fictícios.)\n")
    else:
        saida.write("(IA desligada: sem GEMINI_API_KEY. O chat segue o roteiro.)\n")
    mensagens = conversa.iniciar()
    try:
        while True:
            for mensagem in mensagens:
                saida.write(f"Chatbot: {mensagem}\n")
            digitado = _ler(entrada, saida, "Você: ")
            if digitado is None:
                return 0
            mensagens = conversa.responder(digitado)
    except KeyboardInterrupt:
        saida.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())

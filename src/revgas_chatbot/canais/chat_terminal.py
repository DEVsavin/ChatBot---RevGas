"""Chat de teste no terminal: só repassa texto entre a pessoa e a Conversa.

    uv run python -m revgas_chatbot.canais.chat_terminal [--telefone 5500900000001]

Para ligar a IA (Gemini, cota grátis), rode com a chave no ambiente:

    uv run --env-file .env python -m revgas_chatbot.canais.chat_terminal --telefone 5500900000001

Venda real (M5, só o usuário liga): acrescente `--venda-real`.

O telefone simula o número que o WhatsApp entregaria. A fila de
atendimento e a página `fila.html` ficam em `dados/` (fora do git).
Ctrl+D (ou Ctrl+C) sai do chat.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Mapping, TextIO

from revgas_chatbot.api_revgas.api import ApiRevGas, Relogio
from revgas_chatbot.conversa.entrada import telefone
from revgas_chatbot.montagem import (
    AVISO_ENTREGADORES,
    AVISO_VENDA_REAL,
    ConfiguracaoInvalida,
    confirmar_aviso,
    config_venda_real,
    montar,
    numeros_da_equipe,
    regra_da_ia,
)


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
    parser.add_argument("--venda-real", action="store_true",
                        help="cria vendas DE VERDADE no estabelecimento 2580 (só o usuário liga; M5)")
    parser.add_argument("--avisar-entregadores", action="store_true",
                        help="só com --venda-real: AVISA os entregadores (broadcast NEW_SALE); pede AVISAR")
    argumentos = parser.parse_args(argv)
    ambiente = os.environ if ambiente is None else ambiente
    try:
        if argumentos.avisar_entregadores and not argumentos.venda_real:
            raise ConfiguracaoInvalida("--avisar-entregadores só funciona junto com --venda-real")
        config = config_venda_real(ambiente) if argumentos.venda_real else None
        if config is not None and argumentos.avisar_entregadores:
            confirmar_aviso(lambda t: _ler(entrada, saida, t) or "", lambda t: saida.write(t + "\n"))
            config = replace(config, avisar_entregadores=True)
        regra = regra_da_ia(numeros_da_equipe(ambiente))
    except ConfiguracaoInvalida as erro:
        raise SystemExit(f"Chat não iniciou: {erro}") from None

    numero = telefone(argumentos.telefone) if argumentos.telefone else None
    while numero is None:
        digitado = _ler(entrada, saida, "Telefone do WhatsApp simulado (ex.: 5500900000001): ")
        if digitado is None:
            return 0
        numero = telefone(digitado)
        if numero is None:
            saida.write("Número inválido. Use DDD e número, ex.: (00) 90000-0001.\n")

    programa = montar(pasta_dados, relogio=relogio, api=api, ambiente=ambiente, venda_real=config, ia_permitida=regra)
    conversa = programa(numero)
    saida.write("(Chat de teste. Ctrl+D para sair.)\n")
    if programa.venda_real:
        saida.write(f"*** {AVISO_VENDA_REAL} ***\n")
    if programa.avisar_entregadores:
        saida.write(f"*** {AVISO_ENTREGADORES} ***\n")
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

"""Comando do medidor da IA (fora do pytest: usa rede e a cota grátis do Gemini).

    uv run --env-file .env python -m revgas_chatbot.ia.avaliar [--modelo gemini-3.5-flash] [--salvar]

Manda as frases fictícias de `tests/ia/frases_avaliacao.json` à IA, uma a cada
4 s, e imprime "N de M certas" (ajuste e prova) e os erros. Nunca toca na API
da RevGás nem na fila. Spec: `.scratch/ia/spec-medidor.md`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable, Mapping

from revgas_chatbot.api_revgas.api import Relogio
from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.conversa.conversa import ROTULOS_DE_PAGAMENTO
from revgas_chatbot.ia.avaliacao import ArquivoInvalido, avaliar, carregar_frases, relatorio, salvar
from revgas_chatbot.ia.interpretacao import Interpretador
from revgas_chatbot.ia.interpretador_gemini import MODELO_PADRAO, InterpretadorGemini
from revgas_chatbot.montagem import PASTA_CONFIG

FRASES = PASTA_CONFIG.parent / "tests" / "ia" / "frases_avaliacao.json"


def main(
    argv: list[str] | None = None,
    *,
    ambiente: Mapping[str, str] | None = None,
    interpretador: Interpretador | None = None,  # testes: o InterpretadorFalso
    relogio: Relogio | None = None,
    pasta_dados: Path = Path("dados"),
    escrever: Callable[[str], None] = print,
) -> None:
    parser = argparse.ArgumentParser(description="Medidor da IA: quantas frases fictícias ela entende")
    parser.add_argument("--modelo", default=MODELO_PADRAO, help=f"modelo do Gemini (padrão {MODELO_PADRAO})")
    parser.add_argument("--salvar", action="store_true", help="grava o resultado em dados/ia-avaliacao/")
    parser.add_argument("--frases", type=Path, default=FRASES, help="arquivo de frases (JSON)")
    argumentos = parser.parse_args(argv)

    ambiente = os.environ if ambiente is None else ambiente
    chave = (ambiente.get("GEMINI_API_KEY") or "").strip()
    if not chave:
        escrever("Falta GEMINI_API_KEY no .env. Rode com: uv run --env-file .env python -m revgas_chatbot.ia.avaliar")
        raise SystemExit(1)
    try:
        frases = carregar_frases(argumentos.frases)
    except ArquivoInvalido as erro:
        escrever(f"Arquivo de frases com problema: {erro}")
        raise SystemExit(1) from None

    if relogio is None:
        from revgas_chatbot.relogio import RelogioDoSistema

        relogio = RelogioDoSistema()
    if interpretador is None:
        interpretador = InterpretadorGemini(chave, modelo=argumentos.modelo)
    catalogo = carregar_catalogo(PASTA_CONFIG / "catalogo.json")
    tipos = carregar_pagamentos(PASTA_CONFIG / "pagamentos.json")
    formas = {t: ROTULOS_DE_PAGAMENTO[t] for t in ROTULOS_DE_PAGAMENTO if t in tipos}  # como na conversa

    escrever(f"Medindo {len(frases)} frases com {argumentos.modelo} (uma a cada 4 s)...")
    resultado = avaliar(frases, interpretador, catalogo=catalogo, formas=formas, relogio=relogio,
                        modelo=argumentos.modelo)
    escrever(relatorio(resultado))
    if argumentos.salvar:
        escrever(f"Resultado salvo em {salvar(resultado, pasta_dados, relogio.agora())}")


if __name__ == "__main__":
    main()

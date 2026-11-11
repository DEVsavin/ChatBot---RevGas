"""Página HTML da fila de atendimento (só leitura, para a equipe).

A página tem dados pessoais: é gravada na mesma pasta da fila (`dados/`,
fora do git) e nunca deve ser publicada. Se um dia for servida por HTTP,
precisa de login.

Todo texto é escapado: o que o cliente digitou nunca vira HTML.

A página não se atualiza sozinha: o atendente recarrega (F5) para ver as
novidades.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from revgas_chatbot.fuso import FUSO_TERESINA

if TYPE_CHECKING:
    from revgas_chatbot.fila import Solicitacao

URGENTE = "URGENTE"  # mesmo valor de fila.URGENTE (import direto seria circular)


def _artigo(solicitacao: Solicitacao) -> str:
    s = solicitacao
    horario = s.criada_em.astimezone(FUSO_TERESINA).strftime("%d/%m/%Y %H:%M")
    urgente = s.prioridade == URGENTE
    selo = f'<strong class="selo">{URGENTE}</strong> ' if urgente else ""
    linhas = [
        f"<h2>{selo}{escape(s.protocolo)} · {escape(s.tipo)}</h2>",
        f"<p>Aberta em {escape(horario)}</p>",
        f"<p>Telefone: {escape(s.telefone)}</p>",
    ]
    if s.nome:
        linhas.append(f"<p>Nome: {escape(s.nome)}</p>")
    linhas.append(f"<p>{escape(s.resumo)}</p>")
    if s.texto_cliente:
        linhas.append(f"<blockquote>Cliente: {escape(s.texto_cliente)}</blockquote>")
    if s.dados:
        itens = "".join(
            f"<li>{escape(str(chave))}: {escape(str(valor))}</li>" for chave, valor in s.dados.items()
        )
        linhas.append(f"<ul>{itens}</ul>")
    classes = "solicitacao urgente" if urgente else "solicitacao"
    return f'<article class="{classes}">\n' + "\n".join(linhas) + "\n</article>"


def pagina_da_fila(abertas: list[Solicitacao]) -> str:
    artigos = "\n".join(_artigo(s) for s in abertas) or "<p>Nenhuma solicitação aberta.</p>"
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Fila de atendimento</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 1rem auto; padding: 0 1rem; }}
  .solicitacao {{ border: 1px solid #999; border-radius: 6px; padding: 0.5rem 1rem; margin: 1rem 0; }}
  .urgente {{ border: 3px solid #b00020; background: #fdecee; }}
  .selo {{ color: #fff; background: #b00020; padding: 0 0.4rem; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Fila de atendimento</h1>
{artigos}
</body>
</html>
"""

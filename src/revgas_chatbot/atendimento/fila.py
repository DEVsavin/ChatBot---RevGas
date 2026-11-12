"""Fila de atendimento: solicitações que uma pessoa da equipe precisa resolver.

O arquivo `fila.jsonl` só recebe acréscimos: cada linha é um evento
(`ABERTA` ou `CONCLUIDA`) de um protocolo, e o estado atual de um protocolo
é o seu último evento. Nada é reescrito, então uma queda no meio não
corrompe o que já foi gravado.

Uma linha inválida (escrita interrompida por uma queda) é ignorada na
leitura; a gravação seguinte começa numa linha nova.

O arquivo tem dados pessoais: fica em `dados/`, fora do git.

Limitação do protótipo: um processo e uma thread. Dois processos gravando
ao mesmo tempo podem gerar o mesmo protocolo.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from revgas_chatbot.api_revgas.api import Relogio
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.atendimento.pagina_fila import pagina_da_fila


URGENTE = "URGENTE"
NORMAL = "NORMAL"

# Tipo -> (prefixo do protocolo, prioridade).
# Devolução usa DEV; transferência de venda e comprovante, ATD (D1).
# Só vazamento é urgente: a prioridade vem do tipo, nunca de quem abre.
TIPOS = {
    "CANCELAMENTO": ("DEV", NORMAL),
    "ESTORNO": ("DEV", NORMAL),
    "PROBLEMA_PRODUTO": ("DEV", NORMAL),
    "ALTERACAO": ("DEV", NORMAL),
    "VASILHAME": ("DEV", NORMAL),
    "VAZAMENTO": ("DEV", URGENTE),
    "VENDA": ("ATD", NORMAL),
    "COMPROVANTE_PIX": ("ATD", NORMAL),
    "ATENDIMENTO": ("ATD", NORMAL),  # transferências da conversa (C1-D1)
}

# 11 ou mais dígitos, aceitando um espaço, ponto ou traço entre eles
# (CPF "123.456.789-01", cartão "4111 1111 1111 1111", chave PIX por telefone).
NUMERO_LONGO = re.compile(r"\d(?:[ .\-]?\d){10,}")
MASCARA = "[número omitido]"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Solicitacao:
    protocolo: str
    tipo: str
    prioridade: str
    telefone: str
    resumo: str
    criada_em: datetime
    nome: str | None = None
    dados: dict[str, Any] | None = None
    texto_cliente: str | None = None


def mascarar(texto: str) -> str:
    return NUMERO_LONGO.sub(MASCARA, texto)


class FilaDeAtendimento:
    def __init__(self, pasta: Path, relogio: Relogio) -> None:
        self._arquivo = Path(pasta) / "fila.jsonl"
        self._pagina = Path(pasta) / "fila.html"
        self._relogio = relogio
        # A página pode ter ficado velha (falha anterior, arquivo apagado).
        self._atualizar_pagina()

    def abrir(
        self,
        tipo: str,
        telefone: str,
        resumo: str,
        *,
        nome: str | None = None,
        dados: dict[str, Any] | None = None,
        texto_cliente: str | None = None,
    ) -> str:
        """Abre uma solicitação e devolve o protocolo.

        - `resumo` e `dados` são montados pelo sistema e guardados como vieram
          (podem ter ids e a marca `[chatbot:...]`, que não podem ser alterados).
        - `texto_cliente` é o que o cliente digitou: números longos (CPF,
          cartão, chave PIX) são mascarados ANTES de gravar (LGPD, D4).
          Texto do cliente nunca deve ir em `resumo` ou `dados`.

        Toda validação acontece antes de gravar: depois que o evento é
        gravado, `abrir` não lança (uma nova tentativa duplicaria).
        """
        if tipo not in TIPOS:
            raise ValueError(f"tipo de solicitação desconhecido: {tipo!r}")
        if dados is not None and not isinstance(dados, dict):
            raise ValueError("dados deve ser um dicionário")
        prefixo, prioridade = TIPOS[tipo]
        agora = self._relogio.agora()  # um instante só: protocolo e criado_em batem
        data = agora.astimezone(FUSO_TERESINA).strftime("%Y%m%d")
        inicio = f"{prefixo}-{data}-"
        protocolo = f"{inicio}{self._maior_numero(inicio) + 1}"
        self._gravar(
            {
                "evento": "ABERTA",
                "protocolo": protocolo,
                "tipo": tipo,
                "prioridade": prioridade,
                "telefone": telefone,
                "nome": nome,
                "resumo": resumo,
                "dados": dados,
                "texto_cliente": mascarar(texto_cliente) if texto_cliente is not None else None,
                "criado_em": agora.isoformat(),
            }
        )
        return protocolo

    def listar_abertas(self) -> list[Solicitacao]:
        """Abertas: urgentes primeiro, depois as mais antigas."""
        return sorted(
            self._abertas().values(),
            key=lambda s: (s.prioridade != URGENTE, s.criada_em),
        )

    def concluir(self, protocolo: str, observacao: str | None = None) -> None:
        if protocolo not in self._abertas():
            raise ValueError(f"não há solicitação aberta com o protocolo {protocolo!r}")
        self._gravar(
            {"evento": "CONCLUIDA", "protocolo": protocolo, "observacao": observacao,
             "criado_em": self._relogio.agora().isoformat()}
        )

    # --- leitura do arquivo ----------------------------------------------------

    def _eventos(self) -> list[dict]:
        """Eventos legíveis do arquivo. Linha truncada ou que não é objeto: ignorada."""
        if not self._arquivo.exists():
            return []
        eventos = []
        with self._arquivo.open(encoding="utf-8") as arquivo:
            for linha in arquivo:
                if not linha.strip():
                    continue
                try:
                    evento = json.loads(linha)
                except json.JSONDecodeError:
                    continue  # escrita interrompida: o evento nunca chegou a existir
                if isinstance(evento, dict):
                    eventos.append(evento)
        return eventos

    def _maior_numero(self, inicio: str) -> int:
        """Maior n já usado com esse prefixo e data, em QUALQUER evento.

        Conta até eventos inválidos: um protocolo que o cliente já recebeu
        nunca pode ser reaproveitado, mesmo que a linha dele tenha estragado.
        """
        maior = 0
        for e in self._eventos():
            protocolo = e.get("protocolo")
            if isinstance(protocolo, str) and protocolo.startswith(inicio):
                numero = protocolo[len(inicio):]
                if numero.isdigit():
                    maior = max(maior, int(numero))
        return maior

    def _abertas(self) -> dict[str, Solicitacao]:
        """Estado atual: o último evento de cada protocolo decide se está aberto."""
        abertas: dict[str, Solicitacao] = {}
        for e in self._eventos():
            solicitacao = _solicitacao(e)
            if solicitacao is not None:
                abertas[solicitacao.protocolo] = solicitacao
            elif e.get("evento") == "CONCLUIDA" and isinstance(e.get("protocolo"), str):
                abertas.pop(e["protocolo"], None)
        return abertas

    # --- gravação ----------------------------------------------------------------

    def _gravar(self, evento: dict) -> None:
        linha = json.dumps(evento, ensure_ascii=False) + "\n"  # falha aqui = nada gravado
        self._arquivo.parent.mkdir(parents=True, exist_ok=True)
        if self._ultima_linha_incompleta():
            linha = "\n" + linha  # não grudar no resto de uma escrita interrompida
        with self._arquivo.open("a", encoding="utf-8") as arquivo:
            arquivo.write(linha)
        self._atualizar_pagina()

    def _ultima_linha_incompleta(self) -> bool:
        if not self._arquivo.exists() or self._arquivo.stat().st_size == 0:
            return False
        with self._arquivo.open("rb") as arquivo:
            arquivo.seek(-1, 2)
            return arquivo.read(1) != b"\n"

    def _atualizar_pagina(self) -> None:
        """Regrava a página, de forma atômica (arquivo temporário + troca).

        Qualquer falha aqui é engolida: a solicitação já foi gravada, e um
        erro levaria quem chamou a abrir de novo (duplicando). A página volta
        a ficar certa na próxima mudança ou ao criar a fila de novo.
        """
        try:
            self._pagina.parent.mkdir(parents=True, exist_ok=True)
            temporario = self._pagina.with_name(self._pagina.name + ".tmp")
            temporario.write_text(pagina_da_fila(self.listar_abertas()), encoding="utf-8")
            os.replace(temporario, self._pagina)
        except Exception:  # noqa: BLE001 - ver docstring
            # Sem dados pessoais e sem traceback no log: só o fato.
            _log.warning("não foi possível atualizar a página da fila")


_CAMPOS_DE_ABERTURA = ("protocolo", "tipo", "prioridade", "telefone", "resumo", "criado_em")


def _solicitacao(evento: dict) -> Solicitacao | None:
    """Solicitação de um evento ABERTA válido; None para qualquer outro evento."""
    if evento.get("evento") != "ABERTA":
        return None
    if not all(isinstance(evento.get(c), str) for c in _CAMPOS_DE_ABERTURA):
        return None
    dados = evento.get("dados")
    try:
        criada_em = datetime.fromisoformat(evento["criado_em"])
    except ValueError:
        return None
    return Solicitacao(
        protocolo=evento["protocolo"],
        tipo=evento["tipo"],
        prioridade=evento["prioridade"],
        telefone=evento["telefone"],
        resumo=evento["resumo"],
        criada_em=criada_em,
        nome=evento.get("nome"),
        dados=dados if isinstance(dados, dict) else None,
        texto_cliente=evento.get("texto_cliente"),
    )

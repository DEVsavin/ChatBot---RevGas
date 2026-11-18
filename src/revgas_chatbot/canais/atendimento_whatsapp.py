"""Atendimento pelo WhatsApp: uma mensagem por vez, uma conversa por telefone.

Usado pelo canal do WAHA (`canal_waha.py`), que recebe as mensagens e só as
põe na fila (`receber`). Aqui um único trabalhador atende a fila na ordem
de chegada (S5, W8): conversa → respostas → envio. Regras que valem para
qualquer canal de WhatsApp: só texto (W9), limite de 500 caracteres,
vazamento ou atendente logo na primeira mensagem (I1), e um erro numa
conversa nunca derruba o atendimento (I3). Nada do conteúdo vai para o log.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Protocol

from revgas_chatbot.conversa.entrada import comando, pedido_de_devolucao

MAX_MENSAGEM = 500  # mesmo limite da web
IDS_LEMBRADOS = 5000  # W7: ids de mensagens já recebidas (em memória)

NAO_TEXTO = "Por enquanto só entendo mensagens de texto."
LONGA = "Sua mensagem ficou longa demais. Pode resumir em até 500 caracteres?"
PROBLEMA = "Tive um problema aqui. Pode mandar de novo?"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MensagemRecebida:
    telefone: str  # wa_id como chegou (55 + DDD + número, às vezes sem o 9)
    id: str  # wamid
    texto: str | None  # None = não é texto (áudio, imagem, localização...)


class IdsVistos:
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
            # I1: vazamento, atendente ou devolução logo na primeira mensagem → atende já.
            if comando(m.texto, livre=False) in ("VAZAMENTO", "ATENDENTE") or pedido_de_devolucao(m.texto):
                respostas = conversa.responder(m.texto)
        else:
            respostas = conversa.responder(m.texto)
        self._enviar(m.telefone, respostas)

    def _enviar(self, para: str, respostas: list[str]) -> None:
        for texto in respostas:
            if not self._enviador.enviar(para, texto):
                return  # a ordem importa: sem esta, as seguintes não fazem sentido

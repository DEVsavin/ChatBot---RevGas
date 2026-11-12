"""Testes do C1f: o chat de teste no terminal, de ponta a ponta.

Seam combinado: chat_terminal.main(argv, entrada, saida), usando a montagem
real com a API falsa e uma pasta temporária. O relógio falso e a API falsa
entram pela main só para o teste não esperar segundos de verdade.
"""

import io
import shutil
from datetime import datetime

import pytest

from revgas_chatbot.api_falsa import RelogioFalso
from revgas_chatbot.chat_terminal import main
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.montagem import PASTA_CONFIG, api_de_demonstracao, montar

MARIA = "5500900000001"  # cliente de demonstração (DDD 00: fictício)


def rodar(argv, digitado, tmp_path):
    relogio = RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA))
    api = api_de_demonstracao(relogio)  # a mesma API falsa da demo, com os clientes fictícios
    saida = io.StringIO()
    codigo = main(argv, io.StringIO(digitado), saida, relogio=relogio, api=api, pasta_dados=tmp_path)
    return codigo, saida.getvalue(), api


def test_pedido_completo_pelo_terminal(tmp_path):
    codigo, saida, api = rodar(["--telefone", MARIA], "sim\n1\n2\nsim\n1\n1\n", tmp_path)

    assert codigo == 0
    assert "Chatbot: Olá, Maria!" in saida
    assert "Chatbot: Pedido registrado! Número" in saida
    assert "Você: " in saida
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] is not None


def test_sem_telefone_o_chat_pergunta_qual_simular(tmp_path):
    codigo, saida, _ = rodar([], "abc\n(00) 90000-0001\nsim\n", tmp_path)

    assert codigo == 0
    assert "Telefone do WhatsApp simulado" in saida
    assert "Número inválido" in saida
    assert "Chatbot: Olá, Maria!" in saida


def test_fim_da_entrada_encerra_sem_erro(tmp_path):
    codigo, saida, _ = rodar(["--telefone", MARIA], "sim\n", tmp_path)

    assert codigo == 0
    assert "Chatbot: Olá, Maria!" in saida


def test_vazamento_aparece_na_pagina_da_fila(tmp_path):
    codigo, saida, _ = rodar(["--telefone", MARIA], "cheiro de gás\n", tmp_path)

    assert codigo == 0
    assert "Bombeiros" in saida  # orientações do config/seguranca.txt
    pagina = (tmp_path / "fila.html").read_text(encoding="utf-8")
    assert "VAZAMENTO" in pagina and "URGENTE" in pagina


def test_todas_as_requisicoes_respeitam_1_segundo(tmp_path):
    _, _, api = rodar(["--telefone", MARIA], "sim\n1\n2\nsim\n1\n1\n", tmp_path)

    instantes = api.requisicoes
    assert len(instantes) == 3
    assert all((b - a).total_seconds() >= 1 for a, b in zip(instantes, instantes[1:]))


# --- I3: sem orientações de segurança válidas, o programa não inicia ----------------


@pytest.mark.parametrize("conteudo", ["", "   \n", "Saia do local e abra as janelas."])  # vazio; sem 193
def test_orientacoes_de_seguranca_vazias_ou_sem_193_impedem_a_montagem(tmp_path, conteudo):
    config = tmp_path / "config"
    shutil.copytree(PASTA_CONFIG, config)
    (config / "seguranca.txt").write_text(conteudo, encoding="utf-8")

    with pytest.raises(ValueError, match="seguranca.txt"):
        montar(tmp_path / "dados", pasta_config=config)

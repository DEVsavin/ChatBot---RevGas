"""Testes do C1f: o chat de teste no terminal, de ponta a ponta.

Seam combinado: chat_terminal.main(argv, entrada, saida), usando a montagem
real com a API falsa e uma pasta temporária. O relógio falso e a API falsa
entram pela main só para o teste não esperar segundos de verdade.
"""

import io
import shutil
from datetime import datetime

import pytest

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.chat_terminal import main
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.montagem import PASTA_CONFIG, api_de_demonstracao, montar

MARIA = "5500900000001"  # cliente de demonstração (DDD 00: fictício)


def rodar(argv, digitado, tmp_path):
    relogio = RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA))
    api = api_de_demonstracao(relogio)  # a mesma API falsa da demo, com os clientes fictícios
    saida = io.StringIO()
    codigo = main(argv, io.StringIO(digitado), saida, relogio=relogio, api=api, pasta_dados=tmp_path, ambiente={})
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
        montar(tmp_path / "dados", pasta_config=config, ambiente={})


# --- C2d: IA na montagem ----------------------------------------------------------------


def rodar_com_ambiente(argv, digitado, tmp_path, ambiente):
    relogio = RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA))
    saida = io.StringIO()
    codigo = main(
        argv, io.StringIO(digitado), saida,
        relogio=relogio, api=api_de_demonstracao(relogio), pasta_dados=tmp_path, ambiente=ambiente,
    )
    return codigo, saida.getvalue()


def test_sem_chave_a_ia_fica_desligada_e_o_chat_funciona(tmp_path):
    codigo, saida = rodar_com_ambiente(["--telefone", MARIA], "sim\n1\n2\nsim\n1\n1\n", tmp_path, {})

    assert codigo == 0
    assert "IA desligada" in saida
    assert "Chatbot: Pedido registrado!" in saida


def test_com_chave_o_chat_avisa_que_a_ia_esta_ligada_so_para_clientes_ficticios(tmp_path):
    codigo, saida = rodar_com_ambiente(["--telefone", MARIA], "sim\n", tmp_path, {"GEMINI_API_KEY": "ficticia"})

    assert codigo == 0
    assert "IA ligada" in saida
    assert "ficticia" not in saida  # a chave nunca aparece


class _ApiDeVerdade:
    """Qualquer API que não seja a falsa conta como de verdade."""


def test_ia_da_cota_gratis_com_api_de_verdade_nao_inicia(tmp_path):
    with pytest.raises(ValueError, match="cota grátis"):
        montar(tmp_path, api=_ApiDeVerdade(), ambiente={"GEMINI_API_KEY": "ficticia"})


def test_api_de_verdade_sem_chave_da_ia_inicia_normalmente(tmp_path):
    montar(tmp_path, api=_ApiDeVerdade(), ambiente={})

"""Testes do pedido mais rápido (pedido-agilizar-2026-11-18.md): estado automático (A1),
modelo de mensagem (A2) e leitura do modelo por vírgulas sem IA (A3).

Seam: Conversa.iniciar()/responder(texto) com a API de teste e o catálogo real
(um produto só). Dados fictícios.
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.pedido import ProdutoCatalogo
from revgas_chatbot.vendas.registro import RegistradorDeVendas

MARIA = "5500900000001"
NOVO = "5500900000002"
CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO", "VALEGAS"}


@pytest.fixture
def api():
    api = ApiRevGasFalsa(relogio=RelogioFalso(datetime(2026, 11, 18, 15, 0, tzinfo=FUSO_TERESINA)))
    api.clientes.append({"id": 77, "name": "Maria Exemplo", "phones": [MARIA], "location": "Rua Exemplo",
                         "number": "100", "neighborhood": "Centro", "city": "Teresina",
                         "state_federation": "Piauí"})
    return api


@pytest.fixture
def dependencias(api):
    cadenciada = ApiCadenciada(api, api.relogio)
    return Dependencias(
        api=cadenciada,
        registrador=RegistradorDeVendas(api=cadenciada, relogio=api.relogio, catalogo=CATALOGO,
                                        pagamentos_permitidos=PAGAMENTOS, establishment_id=2580),
        catalogo=CATALOGO, pagamentos=PAGAMENTOS,
        seguranca="ORIENTAÇÕES",
    )


def falar(conversa, *mensagens):
    respostas = [conversa.iniciar()] + [conversa.responder(m) for m in mensagens]
    return respostas


def texto(resposta):
    return "\n".join(resposta)


def test_perguntas_individuais_para_quem_tem_cadastro(dependencias):
    *_, resposta = falar(Conversa(MARIA, dependencias), "sim")

    assert resposta == ["Olá, Maria!", "Botijão de gás (R$ 110,00). Quantos?"]


def test_perguntas_individuais_para_cliente_novo(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano")

    assert resposta == ["Olá, Fulano!", "Botijão de gás (R$ 110,00). Quantos?"]


# --- A1: estado automático -------------------------------------------------------------------


@pytest.mark.parametrize("cidade, no_resumo", [("Teresina", "Teresina, Piauí"), ("Timon", "Timon, Maranhão"),
                                                ("timon", "timon, Maranhão")])
def test_estado_nunca_e_perguntado(dependencias, cidade, no_resumo):
    conversa = Conversa(NOVO, dependencias)
    respostas = falar(conversa, "sim", "Fulano", "1", "Rua Nova", "10", "pular", "Centro", cidade)

    assert respostas[-1] == ["Complemento?\n0) Não tem / Pular etapa"]
    assert not any("estado" in m.lower() for r in respostas for m in r)
    *_, resumo = [conversa.responder(m) for m in ("pular", "pular", "1", "2")]
    assert no_resumo in texto(resumo)


# --- A3: leitura do modelo por vírgulas, sem IA --------------------------------------------


def test_modelo_completo_vai_direto_ao_resumo(dependencias, api):
    conversa = Conversa(NOVO, dependencias)
    *_, pix = falar(conversa, "sim", "Fulano", "2, Rua das Flores 123, Centro, Teresina, Pix")
    assert "O pagamento no PIX será:" in texto(pix)
    resumo = conversa.responder("2")

    assert "Confira seu pedido:" in texto(resumo)
    assert "2x botijão de gás" in texto(resumo)
    assert "Rua das Flores, 123 — Centro, Teresina, Piauí" in texto(resumo)
    assert "Pagamento: PIX (pagar na entrega)" in texto(resumo)
    conversa.responder("1")
    assert api.vendas_criadas()[0]["products"][0]["quantity"] == 2


def test_modelo_com_troco(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resumo = falar(conversa, "sim", "Fulano",
                       "1, Rua Nova 45, bairro Jóquei, Teresina, Dinheiro, troco para R$ 200,00")

    assert "Rua Nova, 45 — Jóquei, Teresina, Piauí" in texto(resumo)
    assert "Dinheiro, troco para R$ 200,00" in texto(resumo)


def test_modelo_com_troco_menor_que_o_total_pergunta_o_troco(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", "2, Rua Nova 45, Centro, Teresina, Dinheiro, troco 100")

    assert "Vai precisar de troco?" in texto(resposta)


def test_modelo_incompleto_pergunta_so_o_que_falta(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", "3, Rua Sete, Centro")

    assert resposta[-1] == "Qual é o número?\n0) Não tem / Pular etapa"  # sem número na rua
    assert conversa.responder("10") == ["Qual é a cidade?"]
    resposta = conversa.responder("Teresina")
    assert resposta[-1].startswith("Complemento?") is False and "Como vai pagar?" in texto(resposta)
    conversa.responder("1")
    resumo = conversa.responder("2")
    assert "3x botijão de gás" in texto(resumo) and "Rua Sete, 10 — Centro, Teresina, Piauí" in texto(resumo)


def test_modelo_com_s_n(dependencias):
    conversa = Conversa(NOVO, dependencias)
    falar(conversa, "sim", "Fulano", "1, Rua Nova S/N, Centro, Teresina, Pix")
    resumo = conversa.responder("2")

    assert "Rua Nova, S/N — Centro, Teresina, Piauí" in texto(resumo)


def test_cartao_sem_dizer_qual_pergunta_credito_ou_debito(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", "1, Rua Nova 45, Centro, Teresina, cartão")

    assert "Cartão de crédito ou de débito?" in texto(resposta)
    assert "Pagamento:" in texto(conversa.responder("4"))  # cartão de crédito → resumo


@pytest.mark.parametrize("forma, rotulo", [("credito", "Cartão de crédito"), ("débito", "Cartão de débito"),
                                           ("dinheiro", "Dinheiro"), ("PIX", "PIX")])
def test_formas_de_pagamento_do_modelo(dependencias, forma, rotulo):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", f"1, Rua Nova 45, Centro, Teresina, {forma}")

    if forma == "dinheiro":
        assert "Vai precisar de troco?" in texto(resposta)
    elif forma == "PIX":
        assert "O pagamento no PIX será:" in texto(resposta)
    else:
        assert f"Pagamento: {rotulo}" in texto(resposta)


def test_cadastro_com_modelo_sem_endereco_pergunta_o_do_cadastro(dependencias):
    conversa = Conversa(MARIA, dependencias)
    *_, resposta = falar(conversa, "sim", "2, , , , Pix")

    assert "Entregar em Rua Exemplo, 100" in texto(resposta)


def test_cadastro_com_modelo_com_endereco_vale_o_dito(dependencias):
    conversa = Conversa(MARIA, dependencias)
    falar(conversa, "sim", "2, Rua das Flores 123, Centro, Teresina, Pix")
    resumo = conversa.responder("2")

    assert "Rua das Flores, 123 — Centro, Teresina, Piauí" in texto(resumo)


def test_mensagem_sem_virgula_segue_como_hoje(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", "2")

    assert resposta == ["Agora o endereço de entrega.", "Qual é a rua?"]


def test_quantidade_invalida_no_modelo_e_perguntada(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", "50, Rua Nova 45, Centro, Teresina, Pix")

    assert resposta[-1] == "Quantos?"
    conversa.responder("2")
    resumo = conversa.responder("2")
    assert "Rua Nova, 45" in texto(resumo) and "2x botijão de gás" in texto(resumo)


# --- Revisão do "agilizar": I1 (frase comum com vírgula) e I2 (número do apartamento) -------


@pytest.mark.parametrize("frase", ["quero 2, por favor", "2, pode ser", "2, obrigado"])
def test_i1_frase_comum_com_virgula_usa_so_a_quantidade(dependencias, frase):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", frase)

    assert resposta == ["Agora o endereço de entrega.", "Qual é a rua?"]


def test_i1_exemplo_do_modelo_continua_indo_direto_ao_resumo(dependencias):
    conversa = Conversa(NOVO, dependencias)
    falar(conversa, "sim", "Fulano", "2, Rua das Flores 123, Centro, Teresina, Pix")
    resumo = conversa.responder("2")

    assert "Confira seu pedido:" in texto(resumo)


def test_i2_numero_do_apartamento_vai_para_o_complemento(dependencias):
    conversa = Conversa(NOVO, dependencias)
    *_, resumo = falar(conversa, "sim", "Fulano", "2, Av. Frei Serafim 2000 apto 301, Centro, Teresina, debito")

    assert "Av. Frei Serafim, 2000 — Centro, Teresina, Piauí" in texto(resumo)
    assert "Complemento: apto 301" in texto(resumo)


@pytest.mark.parametrize("rua, no_resumo", [
    ("Rua 7 de Setembro 100", "Rua 7 de Setembro, 100"),
    ("Quadra 12 Casa 5", None),  # sem nome de rua seguro: o número não é inventado
    ("Rua Nova 45 casa 2", "Rua Nova, 45"),
    ("Rua Nova S/N fundos", "Rua Nova, S/N"),
])
def test_i2_numero_que_faz_parte_do_nome_nao_e_confundido(dependencias, rua, no_resumo):
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = falar(conversa, "sim", "Fulano", f"1, {rua}, Centro, Teresina, Pix")

    if no_resumo:
        assert no_resumo in texto(conversa.responder("2"))

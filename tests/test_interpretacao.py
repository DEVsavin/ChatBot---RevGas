"""Testes do C2a: validar_interpretacao confere o que a IA devolveu.

Seam combinado: a função pura validar_interpretacao. A IA "responde" aqui
com objetos Interpretacao montados à mão (bons e ruins).
"""

from decimal import Decimal

import pytest

from revgas_chatbot.interpretacao import Interpretacao, validar_interpretacao
from revgas_chatbot.pedido import Endereco, ProdutoCatalogo

CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
FORMAS = {"PIX": "PIX", "MONEY": "Dinheiro", "CARTAO_DE_DEBITO": "Cartão de débito"}
ENDERECO = {
    "rua": "Rua Nova",
    "numero": "10",
    "bairro": "Centro",
    "cidade": "Teresina",
    "estado": "PI",
    "complemento": "Casa 2",
    "referencia": None,
}


def validar(**campos):
    return validar_interpretacao(Interpretacao(**campos), CATALOGO, FORMAS)


def test_interpretacao_toda_valida():
    resultado = validar(
        intencao="PEDIR", produto="Botijão de gás", quantidade=2, endereco=ENDERECO,
        pagamento="Dinheiro", troco="250",
    )

    assert resultado.intencao == "PEDIR"
    assert resultado.product_id == 26717
    assert resultado.quantidade == 2
    assert resultado.endereco == Endereco(
        logradouro="Rua Nova", numero="10", bairro="Centro", cidade="Teresina",
        estado="Piauí", complemento="Casa 2", referencia=None,
    )
    assert resultado.pagamento == "MONEY"
    assert resultado.troco == Decimal("250")


# --- cada campo inválido é descartado sozinho ----------------------------------------


@pytest.mark.parametrize("produto", ["pizza", "", None, 26717, "botijão"])
def test_produto_fora_do_catalogo_e_descartado(produto):
    resultado = validar(intencao="PEDIR", produto=produto, quantidade=2)

    assert resultado.product_id is None
    assert resultado.quantidade == 2  # o resto continua


@pytest.mark.parametrize("quantidade", [0, 11, -1, 2.5, 2.0, "2.5", True, None, "dois"])
def test_quantidade_invalida_e_descartada(quantidade):
    resultado = validar(intencao="PEDIR", produto="botijão de gás", quantidade=quantidade)

    assert resultado.quantidade is None
    assert resultado.product_id == 26717


@pytest.mark.parametrize("quantidade, esperado", [(1, 1), (10, 10), ("3", 3)])
def test_quantidade_valida(quantidade, esperado):
    assert validar(intencao="PEDIR", quantidade=quantidade).quantidade == esperado


def test_estado_inexistente_deixa_o_endereco_incompleto_mas_guarda_os_outros_campos():
    resultado = validar(intencao="PEDIR", endereco={**ENDERECO, "estado": "XX"})

    assert resultado.endereco is None
    assert resultado.campos_do_endereco["logradouro"] == "Rua Nova"
    assert "estado" not in resultado.campos_do_endereco


@pytest.mark.parametrize("rua", ["", "   ", "a" * 101, 10])
def test_texto_vazio_longo_demais_ou_de_outro_tipo_e_descartado(rua):
    resultado = validar(intencao="PEDIR", endereco={**ENDERECO, "rua": rua})

    assert "logradouro" not in resultado.campos_do_endereco
    assert resultado.endereco is None


@pytest.mark.parametrize("endereco", ["Rua Nova, 10", ["Rua Nova"], 10])
def test_endereco_que_nao_e_dicionario_e_descartado(endereco):
    resultado = validar(intencao="PEDIR", endereco=endereco)

    assert resultado.endereco is None and resultado.campos_do_endereco == {}


@pytest.mark.parametrize("pagamento", ["boleto", "Cartão de crédito", "", None, 1])
def test_pagamento_fora_da_lista_e_descartado(pagamento):
    assert validar(intencao="PEDIR", pagamento=pagamento).pagamento is None


@pytest.mark.parametrize("intencao", ["COMPRAR", "", None, 3])
def test_intencao_desconhecida_vira_outra(intencao):
    assert validar(intencao=intencao).intencao == "OUTRA"


@pytest.mark.parametrize("intencao", ["vazamento", " Vazamento ", "VAZAMENTO"])
def test_intencao_vale_sem_diferenciar_maiusculas(intencao):
    # Segurança: um 'vazamento' em minúsculas não pode se perder.
    assert validar(intencao=intencao).intencao == "VAZAMENTO"


# --- troco (S2): só com Dinheiro e >= total do catálogo ------------------------------

PEDIDO_EM_DINHEIRO = {"intencao": "PEDIR", "produto": "botijão de gás", "quantidade": 2, "pagamento": "Dinheiro"}


@pytest.mark.parametrize("troco, esperado", [("250", "250"), (250, "250"), ("220", "220"), ("R$ 300,50", "300.50")])
def test_troco_valido(troco, esperado):
    assert validar(**PEDIDO_EM_DINHEIRO, troco=troco).troco == Decimal(esperado)


def test_troco_sem_pagamento_em_dinheiro_e_descartado():
    resultado = validar(**{**PEDIDO_EM_DINHEIRO, "pagamento": "PIX"}, troco="250")

    assert resultado.troco is None
    assert resultado.pagamento == "PIX"


def test_troco_menor_que_o_total_do_catalogo_e_descartado():
    # 2 x R$ 110,00 = R$ 220,00; troco para 200 não serve.
    assert validar(**PEDIDO_EM_DINHEIRO, troco="200").troco is None


@pytest.mark.parametrize("sem", ["quantidade", "produto"])
def test_troco_sem_quantidade_ou_produto_validos_e_descartado(sem):
    assert validar(**{**PEDIDO_EM_DINHEIRO, sem: None}, troco="500").troco is None


@pytest.mark.parametrize("troco", ["muito", "-10", "0", True, None, "250,505"])
def test_troco_que_nao_e_valor_e_descartado(troco):
    assert validar(**PEDIDO_EM_DINHEIRO, troco=troco).troco is None


# --- I5: campo do endereço só vale se o cliente o disse -------------------------------


def validar_com_texto(texto, **campos):
    return validar_interpretacao(Interpretacao(**campos), CATALOGO, FORMAS, texto=texto)


def test_cidade_e_estado_inventados_pela_ia_sao_descartados():
    resultado = validar_com_texto(
        "Rua Nova, 10, Centro", intencao="PEDIR",
        endereco={"rua": "Rua Nova", "numero": "10", "bairro": "Centro", "cidade": "Teresina", "estado": "PI"},
    )

    assert resultado.campos_do_endereco == {"logradouro": "Rua Nova", "numero": "10", "bairro": "Centro"}
    assert resultado.endereco is None


@pytest.mark.parametrize(
    "texto",
    ["rua nova 10 centro teresina pi", "Rua Nova, 10 - Centro, Teresina-PI", "RUA NOVA 10, CENTRO, TERESINA, PIAUÍ"],
)
def test_campos_ditos_valem_sem_diferenciar_maiusculas_acentos_e_pontuacao(texto):
    resultado = validar_com_texto(texto, intencao="PEDIR", endereco=ENDERECO)

    assert resultado.endereco is not None
    assert resultado.endereco.estado == "Piauí"


def test_numero_so_vale_como_numero_inteiro_no_texto():
    # "10" não pode casar com "100".
    resultado = validar_com_texto("Rua Nova 100, Centro", intencao="PEDIR", endereco={"rua": "Rua Nova", "numero": "10"})

    assert "numero" not in resultado.campos_do_endereco


def test_sem_texto_a_validacao_continua_como_antes():
    assert validar(intencao="PEDIR", endereco=ENDERECO).endereco is not None


# --- Ajuste do endereço: PERGUNTA e número (A1, A2) -----------------------------------


@pytest.mark.parametrize("intencao", ["PERGUNTA", "pergunta"])
def test_intencao_pergunta(intencao):
    assert validar(intencao=intencao).intencao == "PERGUNTA"


@pytest.mark.parametrize("numero, esperado", [("ap409", None), ("bloco b", None), ("10a", "10A"), ("s/n", "S/N")])
def test_numero_do_endereco_dito_pela_ia_segue_a_regra_a2(numero, esperado):
    resultado = validar(intencao="PEDIR", endereco={"rua": "Rua Nova", "numero": numero})

    assert resultado.campos_do_endereco.get("numero") == esperado

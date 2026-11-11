"""Testes do C1a: regras de entrada da conversa (funções puras de entrada.py)."""

from decimal import Decimal

import pytest

from revgas_chatbot.entrada import (
    comando,
    opcao,
    sim_ou_nao,
    telefone,
    telefone_legivel,
    valor_em_reais,
)


@pytest.mark.parametrize(
    "digitado, esperado",
    [
        ("(86) 99999-0000", "5586999990000"),
        ("86999990000", "5586999990000"),
        ("+55 86 99999-0000", "5586999990000"),
        ("5586999990000", "5586999990000"),
        ("(86) 3333-0000", "558633330000"),  # fixo, 10 dígitos
        ("558633330000", "558633330000"),
    ],
)
def test_telefone_aceita_formatos_comuns_e_devolve_55_ddd_numero(digitado, esperado):
    assert telefone(digitado) == esperado


@pytest.mark.parametrize(
    "digitado",
    ["", "abc", "99999-0000", "123", "44 86 99999-0000", "558699999000011", "(86) 99999-00000"],
)
def test_telefone_invalido_devolve_none(digitado):
    assert telefone(digitado) is None


@pytest.mark.parametrize(
    "numero, legivel",
    [("5586999990000", "(86) 99999-0000"), ("558633330000", "(86) 3333-0000")],
)
def test_telefone_legivel(numero, legivel):
    assert telefone_legivel(numero) == legivel


# --- sim/não, opção, valor ---------------------------------------------------------


@pytest.mark.parametrize("texto", ["sim", "SIM", "s", "isso", "pode", "confirmo", " Sim! ", "sim."])
def test_sim(texto):
    assert sim_ou_nao(texto) is True


@pytest.mark.parametrize("texto", ["não", "nao", "NÃO", "n", "não."])
def test_nao(texto):
    assert sim_ou_nao(texto) is False


@pytest.mark.parametrize("texto", ["", "talvez", "simone", "sim e não", "nunca"])
def test_nem_sim_nem_nao(texto):
    assert sim_ou_nao(texto) is None


OPCOES = ["PIX", "Dinheiro", "Cartão de débito", "Cartão de crédito"]


@pytest.mark.parametrize(
    "texto, indice",
    [("1", 0), (" 2 ", 1), ("2)", 1), ("pix", 0), ("dinheiro", 1), ("cartao de debito", 2), ("Cartão de Crédito", 3)],
)
def test_opcao_pelo_numero_ou_pelo_texto(texto, indice):
    assert opcao(texto, OPCOES) == indice


@pytest.mark.parametrize("texto", ["0", "5", "-1", "cartão", "", "1 ou 2"])
def test_opcao_que_nao_esta_na_lista(texto):
    assert opcao(texto, OPCOES) is None


@pytest.mark.parametrize(
    "texto, valor",
    [("200", "200"), ("200,00", "200.00"), ("R$ 200", "200"), ("r$200,50", "200.50"), ("200.50", "200.50"), ("1.200,00", "1200.00")],
)
def test_valor_em_reais(texto, valor):
    assert valor_em_reais(texto) == Decimal(valor)


@pytest.mark.parametrize("texto", ["", "duzentos", "0", "-10", "200,505", "R$", "1,2,3"])
def test_valor_invalido(texto):
    assert valor_em_reais(texto) is None


# --- comandos globais (V3) ----------------------------------------------------------


@pytest.mark.parametrize(
    "texto",
    ["estou sentindo cheiro de gás", "VAZAMENTO", "o botijão está vazando!", "tá cheirando gás aqui"],
)
@pytest.mark.parametrize("livre", [False, True])
def test_vazamento_vale_sempre_ate_em_texto_livre(texto, livre):
    assert comando(texto, livre=livre) == "VAZAMENTO"


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("quero cancelar", "CANCELAR_OU_DEVOLVER"),
        ("cancela isso", "CANCELAR_OU_DEVOLVER"),
        ("preciso devolver o botijão", "CANCELAR_OU_DEVOLVER"),
        ("quero o estorno", "CANCELAR_OU_DEVOLVER"),
        ("quero falar com um atendente", "ATENDENTE"),
        ("quero falar com uma pessoa", "ATENDENTE"),
        ("sair", "SAIR"),
        ("tchau!", "SAIR"),
    ],
)
def test_comandos_por_palavra_inteira_nos_passos_de_opcao(texto, esperado):
    assert comando(texto, livre=False) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "vou pagar pessoalmente",  # 'pessoa' só como palavra inteira
        "quero trocar o endereço",  # 'trocar' não é comando
        "troca de vasilhame",
        "cancelamento",  # palavra diferente de 'cancelar'
        "2",
        "",
    ],
)
def test_nao_e_comando(texto):
    assert comando(texto, livre=False) is None


@pytest.mark.parametrize(
    "texto",
    ["Rua do Atendente, 10", "Maria Pessoa", "Travessa Cancela", "Sair da Silva"],
)
def test_em_texto_livre_comando_so_vale_como_mensagem_inteira(texto):
    assert comando(texto, livre=True) is None


@pytest.mark.parametrize(
    "texto, esperado",
    [("atendente", "ATENDENTE"), ("Cancelar", "CANCELAR_OU_DEVOLVER"), ("sair.", "SAIR")],
)
def test_em_texto_livre_a_mensagem_inteira_dispara(texto, esperado):
    assert comando(texto, livre=True) == esperado


# --- só dígitos ASCII (I1) -----------------------------------------------------------


@pytest.mark.parametrize("digitado", ["１１９９９９９００００", "86９９９９９0000", "(٨٦) 99999-0000"])
def test_telefone_com_digitos_que_nao_sao_ascii_e_invalido(digitado):
    assert telefone(digitado) is None


@pytest.mark.parametrize("digitado", ["２００", "200,０0"])
def test_valor_com_digitos_que_nao_sao_ascii_e_invalido(digitado):
    assert valor_em_reais(digitado) is None

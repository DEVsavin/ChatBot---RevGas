"""Testes do M2: montagem do corpo do POST /sales (função pura, sem HTTP).

Dados fictícios. O product_id 1 é de um catálogo de teste.
"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from revgas_chatbot.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.venda import corpo_para_json, marca, montar_corpo_venda

CATALOGO = {1: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
CLIENTE = 123
ESTABELECIMENTO = 2580


def pedido(**mudancas) -> Pedido:
    base = Pedido(
        pedido_id="abc-123",
        telefone="5586999990000",
        nome="Cliente Teste",
        itens=(Item(product_id=1, quantidade=1),),
        endereco=Endereco(
            logradouro="Rua Exemplo",
            numero="100",
            bairro="Centro",
            cidade="Teresina",
            estado="Piauí",
        ),
        pagamentos=(Pagamento(tipo="PIX", valor=Decimal("110.00")),),
    )
    return replace(base, **mudancas)


def montar(p: Pedido) -> dict:
    return montar_corpo_venda(p, customer_id=CLIENTE, catalogo=CATALOGO, establishment_id=ESTABELECIMENTO)


# --- campos básicos -----------------------------------------------------------


def test_corpo_tem_estabelecimento_cliente_e_canal_whatsapp():
    corpo = montar(pedido())
    assert corpo["establishment_id"] == 2580
    assert corpo["customer_id"] == 123
    assert corpo["channel"] == "WHATSAPP"


def test_endereco_vai_nos_campos_da_api():
    corpo = montar(pedido())
    assert corpo["location"] == "Rua Exemplo"
    assert corpo["number"] == "100"
    assert corpo["neighborhood"] == "Centro"
    assert corpo["city"] == "Teresina"
    assert corpo["state_federation"] == "Piauí"


def test_complemento_e_referencia_vao_quando_informados():
    endereco = replace(pedido().endereco, complemento="Apto 2", referencia="Perto da praça")
    corpo = montar(pedido(endereco=endereco))
    assert corpo["complement"] == "Apto 2"
    assert corpo["landmark"] == "Perto da praça"


@pytest.mark.parametrize("vazio", [None, "", "  "])
def test_complemento_e_referencia_vazios_nao_vao(vazio):
    endereco = replace(pedido().endereco, complemento=vazio, referencia=vazio)
    corpo = montar(pedido(endereco=endereco))
    assert "complement" not in corpo
    assert "landmark" not in corpo


def test_campos_pendentes_nao_sao_enviados():
    # origin/option/broadcast: valores desconhecidos (D4/D5).
    # lat/lon: D6. deliverer_id: a central atribui.
    corpo = montar(pedido())
    for campo in ("origin", "option", "broadcast", "lat", "lon", "deliverer_id"):
        assert campo not in corpo


# --- itens: uma linha por unidade ---------------------------------------------


def test_n_unidades_viram_n_linhas_com_quantity_1():
    p = pedido(
        itens=(Item(product_id=1, quantidade=3),),
        pagamentos=(Pagamento(tipo="PIX", valor=Decimal("330.00")),),
    )
    linhas = montar(p)["products"]
    assert linhas == [{"product_id": 1, "quantity": 1, "value": Decimal("110.00")}] * 3


def test_preco_vem_do_catalogo():
    catalogo = {1: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("99.90"))}
    p = pedido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal("99.90")),))
    corpo = montar_corpo_venda(p, customer_id=CLIENTE, catalogo=catalogo, establishment_id=ESTABELECIMENTO)
    assert corpo["products"][0]["value"] == Decimal("99.90")


def test_produto_fora_do_catalogo_e_erro_de_uso():
    # A validação (M1) já barra isso; aqui é só uma proteção extra.
    with pytest.raises(ValueError):
        montar(pedido(itens=(Item(product_id=999, quantidade=1),)))


# --- pagamentos ---------------------------------------------------------------


def test_pagamentos_vao_com_tipo_e_valor():
    p = pedido(
        pagamentos=(
            Pagamento(tipo="PIX", valor=Decimal("100.00")),
            Pagamento(tipo="MONEY", valor=Decimal("10.00")),
        )
    )
    assert montar(p)["payments"] == [
        {"payment_type": "PIX", "value": Decimal("100.00")},
        {"payment_type": "MONEY", "value": Decimal("10.00")},
    ]


@pytest.mark.parametrize("tipo", ["CARTAO_DE_CREDITO", "CARTAO_DE_DEBITO"])
def test_cartao_nao_envia_bandeira_nem_credenciadora(tipo):
    p = pedido(pagamentos=(Pagamento(tipo=tipo, valor=Decimal("110.00")),))
    pagamento = montar(p)["payments"][0]
    assert "card_brand" not in pagamento
    assert "creditor_establishment_id" not in pagamento


# --- observation: troco + marca de reconciliação -------------------------------


def test_marca_tem_o_pedido_id():
    assert marca("abc-123") == "[chatbot:abc-123]"


def test_observation_termina_com_a_marca():
    assert montar(pedido())["observation"] == "[chatbot:abc-123]"


def test_observacao_do_cliente_vem_antes_da_marca():
    corpo = montar(pedido(observacao="Interfone quebrado, chamar no portão."))
    assert corpo["observation"] == "Interfone quebrado, chamar no portão. [chatbot:abc-123]"


def test_troco_vai_na_observation_antes_da_marca():
    p = pedido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("200.00"),
    )
    assert montar(p)["observation"] == "Troco para R$ 200,00. [chatbot:abc-123]"


def test_troco_com_milhar_usa_formato_brasileiro():
    p = pedido(
        itens=(Item(product_id=1, quantidade=10),),
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("1100.00")),),
        troco_para=Decimal("1200"),
    )
    assert montar(p)["observation"].startswith("Troco para R$ 1.200,00.")


def test_dinheiro_sem_troco_nao_menciona_troco():
    p = pedido(pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),))
    assert "Troco" not in montar(p)["observation"]


def test_observacao_troco_e_marca_juntos_nessa_ordem():
    p = pedido(
        observacao="Casa azul.",
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("150.00"),
    )
    assert montar(p)["observation"] == "Casa azul. Troco para R$ 150,00. [chatbot:abc-123]"


# --- JSON --------------------------------------------------------------------


def test_json_envia_dinheiro_como_numero_e_nao_como_texto():
    p = pedido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal("110")),))
    texto = corpo_para_json(montar(p))
    dados = json.loads(texto)
    assert dados["products"][0]["value"] == 110.0
    assert dados["payments"][0]["value"] == 110.0
    assert '"value": "' not in texto  # nunca string


def test_json_preserva_centavos():
    catalogo = {1: ProdutoCatalogo(nome="regulador", preco=Decimal("12.31"))}
    p = pedido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal("12.31")),))
    corpo = montar_corpo_venda(p, customer_id=CLIENTE, catalogo=catalogo, establishment_id=ESTABELECIMENTO)
    assert json.loads(corpo_para_json(corpo), parse_float=Decimal)["products"][0]["value"] == Decimal("12.31")


def test_json_mantem_acentos_legiveis():
    assert "Piauí" in corpo_para_json(montar(pedido()))


# --- ajustes da revisão do M2 --------------------------------------------------


@pytest.mark.parametrize("estado", ["PI", "pi", "piaui", "Piauí"])
def test_estado_vai_sempre_por_extenso(estado):
    endereco = replace(pedido().endereco, estado=estado)
    assert montar(pedido(endereco=endereco))["state_federation"] == "Piauí"


def test_troco_igual_ao_valor_em_dinheiro_vira_sem_troco():
    p = pedido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("110.00"),
    )
    assert montar(p)["observation"] == "[chatbot:abc-123]"


def test_cada_linha_e_um_objeto_independente():
    p = pedido(
        itens=(Item(product_id=1, quantidade=2),),
        pagamentos=(Pagamento(tipo="PIX", valor=Decimal("220.00")),),
    )
    linhas = montar(p)["products"]
    linhas[0]["quantity"] = 99
    assert linhas[1]["quantity"] == 1

"""Testes do M1: modelo do Pedido + validação (spec seção 3.1).

Nenhum teste aqui faz chamada HTTP: a validação é 100% local.
Os dados são fictícios (telefone e endereço inventados só para teste).
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from revgas_chatbot.pedido import (
    Endereco,
    ForaDoCatalogo,
    Invalido,
    Item,
    Pagamento,
    Pedido,
    ProdutoCatalogo,
    Valido,
    validar,
)

# Catálogo de TESTE: o product_id real ainda não foi escolhido (D7).
CATALOGO = {1: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
PERMITIDOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"}


def pedido_valido(**mudancas) -> Pedido:
    base = Pedido(
        pedido_id="pedido-teste-1",
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


def checar(pedido: Pedido):
    return validar(pedido, CATALOGO, PERMITIDOS)


# --- caminho feliz -----------------------------------------------------------


def test_pedido_completo_e_valido_e_traz_o_total():
    resultado = checar(pedido_valido())
    assert resultado == Valido(total=Decimal("110.00"))


def test_total_multiplica_preco_pela_quantidade():
    pedido = pedido_valido(
        itens=(Item(product_id=1, quantidade=2),),
        pagamentos=(Pagamento(tipo="PIX", valor=Decimal("220.00")),),
    )
    assert checar(pedido) == Valido(total=Decimal("220.00"))


def test_pagamento_dividido_que_soma_o_total_e_valido():
    pedido = pedido_valido(
        pagamentos=(
            Pagamento(tipo="PIX", valor=Decimal("100.00")),
            Pagamento(tipo="MONEY", valor=Decimal("10.00")),
        )
    )
    assert isinstance(checar(pedido), Valido)


def test_somas_com_decimal_sao_exatas():
    # Com float, 0.1 + 0.2 = 0.30000000000000004 e a soma "não bateria".
    catalogo = {1: ProdutoCatalogo(nome="item barato", preco=Decimal("0.30"))}
    pedido = pedido_valido(
        pagamentos=(
            Pagamento(tipo="PIX", valor=Decimal("0.10")),
            Pagamento(tipo="PIX", valor=Decimal("0.20")),
        )
    )
    assert validar(pedido, catalogo, PERMITIDOS) == Valido(total=Decimal("0.30"))


# --- dados faltando ou errados → Invalido (o cliente corrige) -----------------


@pytest.mark.parametrize("campo", ["logradouro", "numero", "bairro", "cidade", "estado"])
def test_endereco_sem_campo_obrigatorio_e_invalido(campo):
    endereco = replace(pedido_valido().endereco, **{campo: "  "})
    resultado = checar(pedido_valido(endereco=endereco))
    assert isinstance(resultado, Invalido)
    assert f"endereco.{campo}" in resultado.campos()


def test_complemento_e_referencia_sao_opcionais():
    endereco = replace(pedido_valido().endereco, complemento=None, referencia=None)
    assert isinstance(checar(pedido_valido(endereco=endereco)), Valido)


def test_pedido_sem_endereco_e_invalido():
    resultado = checar(pedido_valido(endereco=None))
    assert isinstance(resultado, Invalido)
    assert "endereco" in resultado.campos()


def test_nome_vazio_e_invalido():
    resultado = checar(pedido_valido(nome=" "))
    assert isinstance(resultado, Invalido)
    assert "nome" in resultado.campos()


@pytest.mark.parametrize(
    "telefone",
    [
        "86999990000",  # sem o 55
        "558699999",  # curto demais
        "55869999900001",  # 14 dígitos
        "+55 86 99999-0000",  # com formatação: a conversa deve normalizar antes
        "",
    ],
)
def test_telefone_fora_do_formato_e_invalido(telefone):
    resultado = checar(pedido_valido(telefone=telefone))
    assert isinstance(resultado, Invalido)
    assert "telefone" in resultado.campos()


@pytest.mark.parametrize("telefone", ["5586999990000", "558633330000"])
def test_telefone_com_12_ou_13_digitos_e_valido(telefone):
    assert isinstance(checar(pedido_valido(telefone=telefone)), Valido)


def test_pedido_sem_itens_e_invalido():
    resultado = checar(pedido_valido(itens=()))
    assert isinstance(resultado, Invalido)
    assert "itens" in resultado.campos()


@pytest.mark.parametrize("quantidade", [0, -1])
def test_quantidade_menor_que_1_e_invalida(quantidade):
    resultado = checar(pedido_valido(itens=(Item(product_id=1, quantidade=quantidade),)))
    assert isinstance(resultado, Invalido)
    assert "itens" in resultado.campos()


def test_pedido_sem_pagamento_e_invalido():
    resultado = checar(pedido_valido(pagamentos=()))
    assert isinstance(resultado, Invalido)
    assert "pagamentos" in resultado.campos()


def test_soma_dos_pagamentos_diferente_do_total_e_invalida():
    pedido = pedido_valido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal("100.00")),))
    resultado = checar(pedido)
    assert isinstance(resultado, Invalido)
    assert "pagamentos" in resultado.campos()


def test_forma_de_pagamento_fora_da_lista_e_invalida():
    pedido = pedido_valido(pagamentos=(Pagamento(tipo="BANKDRAFT", valor=Decimal("110.00")),))
    resultado = checar(pedido)
    assert isinstance(resultado, Invalido)
    assert "pagamentos" in resultado.campos()


def test_pagamento_com_valor_zero_ou_negativo_e_invalido():
    pedido = pedido_valido(
        pagamentos=(
            Pagamento(tipo="PIX", valor=Decimal("110.00")),
            Pagamento(tipo="MONEY", valor=Decimal("0.00")),
        )
    )
    assert isinstance(checar(pedido), Invalido)


@pytest.mark.parametrize("valor", ["NaN", "sNaN", "Infinity", "-Infinity", "110.005"])
def test_pagamento_com_valor_nao_finito_ou_mais_de_2_casas_e_invalido(valor):
    pedido = pedido_valido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal(valor)),))
    resultado = checar(pedido)  # não pode lançar exceção
    assert isinstance(resultado, Invalido)
    assert "pagamentos" in resultado.campos()


def test_pagamento_sem_casas_decimais_e_valido():
    pedido = pedido_valido(pagamentos=(Pagamento(tipo="PIX", valor=Decimal("110")),))
    assert isinstance(checar(pedido), Valido)


@pytest.mark.parametrize("pedido_id", ["", "   "])
def test_pedido_sem_identificador_e_invalido(pedido_id):
    # Sem pedido_id a marca de reconciliação viraria "[chatbot:]".
    resultado = checar(pedido_valido(pedido_id=pedido_id))
    assert isinstance(resultado, Invalido)
    assert "pedido_id" in resultado.campos()


def test_varios_problemas_sao_listados_juntos():
    resultado = checar(pedido_valido(nome="", telefone="123", endereco=None))
    assert isinstance(resultado, Invalido)
    assert {"nome", "telefone", "endereco"} <= set(resultado.campos())


# --- troco (pagamento em dinheiro) -------------------------------------------


def test_dinheiro_sem_troco_e_valido():
    pedido = pedido_valido(pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),))
    assert isinstance(checar(pedido), Valido)


def test_dinheiro_com_troco_maior_ou_igual_ao_valor_e_valido():
    pedido = pedido_valido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("200.00"),
    )
    assert isinstance(checar(pedido), Valido)


def test_troco_para_exatamente_o_valor_em_dinheiro_e_valido():
    # Fronteira: troco "para 110" num pagamento de 110 = troco zero, aceito.
    pedido = pedido_valido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("110.00"),
    )
    assert isinstance(checar(pedido), Valido)


@pytest.mark.parametrize("troco", ["NaN", "Infinity", "200.001", "0", "-10"])
def test_troco_com_valor_nao_finito_zero_ou_mais_de_2_casas_e_invalido(troco):
    pedido = pedido_valido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal(troco),
    )
    resultado = checar(pedido)
    assert isinstance(resultado, Invalido)
    assert "troco_para" in resultado.campos()


def test_troco_para_valor_menor_que_o_pago_em_dinheiro_e_invalido():
    pedido = pedido_valido(
        pagamentos=(Pagamento(tipo="MONEY", valor=Decimal("110.00")),),
        troco_para=Decimal("100.00"),
    )
    resultado = checar(pedido)
    assert isinstance(resultado, Invalido)
    assert "troco_para" in resultado.campos()


def test_troco_compara_so_com_a_parte_em_dinheiro():
    # 100 no PIX + 10 em dinheiro: troco para 20 é suficiente.
    pedido = pedido_valido(
        pagamentos=(
            Pagamento(tipo="PIX", valor=Decimal("100.00")),
            Pagamento(tipo="MONEY", valor=Decimal("10.00")),
        ),
        troco_para=Decimal("20.00"),
    )
    assert isinstance(checar(pedido), Valido)


def test_troco_sem_pagamento_em_dinheiro_e_invalido():
    pedido = pedido_valido(troco_para=Decimal("200.00"))  # pagamento é PIX
    resultado = checar(pedido)
    assert isinstance(resultado, Invalido)
    assert "troco_para" in resultado.campos()


# --- produto fora do catálogo → erro de configuração, não do cliente ---------


def test_produto_fora_do_catalogo_vira_fora_do_catalogo_e_nao_invalido():
    pedido = pedido_valido(itens=(Item(product_id=999, quantidade=1),))
    assert checar(pedido) == ForaDoCatalogo(product_ids=(999,))


def test_fora_do_catalogo_tem_prioridade_sobre_outros_problemas():
    # Sem preço não dá nem para conferir a soma dos pagamentos.
    pedido = pedido_valido(nome="", itens=(Item(product_id=999, quantidade=1),))
    assert isinstance(checar(pedido), ForaDoCatalogo)


# --- estado e observação ------------------------------------------------------


@pytest.mark.parametrize("estado", ["Piauí", "PI", "pi", "piaui", " PIAUÍ "])
def test_estado_aceita_sigla_ou_nome(estado):
    endereco = replace(pedido_valido().endereco, estado=estado)
    assert isinstance(checar(pedido_valido(endereco=endereco)), Valido)


@pytest.mark.parametrize("estado", ["XX", "Piauii", "Teresina"])
def test_estado_desconhecido_e_invalido(estado):
    endereco = replace(pedido_valido().endereco, estado=estado)
    resultado = checar(pedido_valido(endereco=endereco))
    assert isinstance(resultado, Invalido)
    assert "endereco.estado" in resultado.campos()


def test_observacao_com_ate_200_caracteres_e_valida():
    assert isinstance(checar(pedido_valido(observacao="a" * 200)), Valido)


def test_observacao_com_mais_de_200_caracteres_e_invalida():
    resultado = checar(pedido_valido(observacao="a" * 201))
    assert isinstance(resultado, Invalido)
    assert "observacao" in resultado.campos()

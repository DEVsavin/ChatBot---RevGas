"""Testes de Devolucao.solicitar(...): registra na `observation` da venda via PUT /sales/{id}."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.devolucao.devolucao import (
    Devolucao,
    PedidoDoCliente,
    PedidosEncontrados,
    SolicitacaoAberta,
    SolicitacaoInvalida,
)
from revgas_chatbot.fuso import FUSO_TERESINA

TELEFONE = "5586999990000"
AGORA = datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)

PEDIDO = PedidoDoCliente(
    id=1001,
    data=date(2026, 11, 10),
    itens=(("GLP 13KG", 1),),
    total=Decimal("110.00"),
    status="ACTIVE",
    criada_em=AGORA - timedelta(minutes=30),
)
PEDIDO_CANCELADO = PedidoDoCliente(
    id=1002,
    data=date(2026, 11, 10),
    itens=(("GLP 13KG", 1),),
    total=Decimal("110.00"),
    status="CANCEL",
)

ENCONTRADOS = PedidosEncontrados(customer_id=77, nome="Cliente Teste", pedidos=(PEDIDO, PEDIDO_CANCELADO))
DO_PEDIDO = {"encontrados": ENCONTRADOS, "escolha": 1}
DO_CANCELADO = {"encontrados": ENCONTRADOS, "escolha": 2}
SEM_PEDIDO = {"encontrados": ENCONTRADOS, "escolha": None}


@pytest.fixture
def api():
    falsa = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    falsa.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "products": [{"product_id": 26717, "quantity": 1, "value": Decimal("110.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("110.00")}],
            "observation": "[chatbot:original]",
        },
        criada_em=AGORA - timedelta(minutes=30),
    )
    falsa.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "status": "CANCEL",
            "products": [{"product_id": 26717, "quantity": 1, "value": Decimal("110.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("110.00")}],
        },
        criada_em=AGORA - timedelta(days=1),
    )
    return falsa


@pytest.fixture
def devolucao(api):
    return Devolucao(api=ApiCadenciada(api, api.relogio), relogio=api.relogio)


def test_cancelamento_anota_na_venda_via_api(devolucao, api):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoAberta)
    assert resultado.protocolo == "ERP-1001"
    assert resultado.entregador_pode_ter_saido is True
    venda = next(v for v in api.vendas if v["id"] == 1001)
    assert "[chatbot:original]" in venda["observation"]
    assert "[CHATBOT][CANCELAMENTO]" in venda["observation"]
    assert "Motivo: desisti." in venda["observation"]


@pytest.mark.parametrize(
    "tipo, motivo, preferencia, texto",
    [
        ("CANCELAMENTO", "DESISTI", None, None),
        ("CANCELAMENTO", "PEDI_ERRADO", None, None),
        ("ESTORNO", "PEDIDO_CANCELADO_JA_PAGO", "DINHEIRO_DE_VOLTA", None),
        ("ESTORNO", "COBRANCA_ERRADA", "CREDITO_PROXIMA_COMPRA", None),
        ("PROBLEMA_PRODUTO", "OUTRO", None, "lacre violado"),
    ],
)
def test_tipos_e_motivos_validos_anotam_na_venda(devolucao, api, tipo, motivo, preferencia, texto):
    alvo = DO_CANCELADO if motivo == "PEDIDO_CANCELADO_JA_PAGO" else DO_PEDIDO
    resultado = devolucao.solicitar(tipo, TELEFONE, **alvo, motivo=motivo, preferencia=preferencia, texto=texto)

    assert isinstance(resultado, SolicitacaoAberta)
    assert resultado.protocolo.startswith("ERP-")


def test_sem_pedido_nao_toca_na_api_nem_cria_arquivo(devolucao, api):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **SEM_PEDIDO, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoAberta)
    assert resultado.protocolo == "ATENDIMENTO"
    assert api.operacoes == []


def test_texto_com_cpf_e_mascarado_antes_de_ir_para_observation(devolucao, api):
    devolucao.solicitar(
        "PROBLEMA_PRODUTO", TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="meu cpf 123.456.789-01"
    )
    venda = next(v for v in api.vendas if v["id"] == 1001)
    assert "123.456.789-01" not in venda["observation"]
    assert "[número omitido]" in venda["observation"]


def test_validacoes_rejeitam_dados_invalidos(devolucao):
    assert isinstance(devolucao.solicitar("CANCELAMENTO", "", **DO_PEDIDO, motivo="DESISTI"), SolicitacaoInvalida)
    assert isinstance(devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_CANCELADO, motivo="DESISTI"), SolicitacaoInvalida)
    assert isinstance(devolucao.solicitar("ESTORNO", TELEFONE, **DO_PEDIDO, motivo="COBRANCA_ERRADA"), SolicitacaoInvalida)
    assert isinstance(devolucao.solicitar("PROBLEMA_PRODUTO", TELEFONE, **DO_PEDIDO, motivo="OUTRO"), SolicitacaoInvalida)

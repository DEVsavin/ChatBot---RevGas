"""Testes da devolução na conversa: usa a API RevGás (`GET /sales` + `PUT /sales/{id}`), sem fila local."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api import Resposta
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.devolucao.devolucao import Devolucao
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.pedido import ProdutoCatalogo
from revgas_chatbot.vendas.registro import RegistradorDeVendas

AGORA = datetime(2026, 11, 18, 15, 0, tzinfo=FUSO_TERESINA)
TELEFONE = "5500900000001"
CATALOGO = {
    26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00")),
    26255: ProdutoCatalogo(nome="água 20 L", preco=Decimal("15.00")),
}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO", "VALEGAS"}
MARIA = {
    "id": 77,
    "name": "Maria Exemplo",
    "phones": [TELEFONE],
    "location": "Rua Exemplo",
    "number": "100",
    "neighborhood": "Centro",
    "city": "Teresina",
    "state_federation": "Piauí",
}
ATE_O_PEDIDO = ("sim", "1", "2", "sim", "1", "2", "1")


@pytest.fixture
def api():
    falsa = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    falsa.clientes.append(dict(MARIA))
    return falsa


def semear(
    api,
    *,
    dias_atras=1,
    quantidade=2,
    status=None,
    cliente=77,
    pagamento="PIX",
    horas_atras=None,
    observacao=None,
    entregue_em=None,
):
    venda = api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": cliente,
            "channel": "WHATSAPP",
            "products": [
                {
                    "product_id": 26717,
                    "quantity": quantidade,
                    "value": Decimal("110.00"),
                    "product_description": "botijão de gás",
                }
            ],
            **({"payments": [{"payment_type": pagamento, "value": Decimal("110.00") * quantidade}]} if pagamento else {}),
            **({"observation": observacao} if observacao else {}),
        },
        criada_em=AGORA - (timedelta(hours=horas_atras) if horas_atras is not None else timedelta(days=dias_atras)),
    )
    guardada = next(v for v in api.vendas if v["id"] == venda["id"])
    if status:
        guardada["status"] = status
    if entregue_em:
        guardada["delivered_at"] = entregue_em
    return venda["id"]


@pytest.fixture
def dependencias(api):
    cadenciada = ApiCadenciada(api, api.relogio)
    return Dependencias(
        api=cadenciada,
        registrador=RegistradorDeVendas(
            api=cadenciada,
            relogio=api.relogio,
            catalogo=CATALOGO,
            pagamentos_permitidos=PAGAMENTOS,
            establishment_id=2580,
        ),
        catalogo=CATALOGO,
        pagamentos=PAGAMENTOS,
        seguranca="ORIENTAÇÕES DE SEGURANÇA 193",
        devolucao=Devolucao(api=cadenciada, relogio=api.relogio, catalogo=CATALOGO),
    )


@pytest.fixture
def conversa(dependencias):
    return Conversa(TELEFONE, dependencias)


def falar(conversa, *mensagens) -> str:
    conversa.iniciar()
    resposta = []
    for m in mensagens:
        resposta = conversa.responder(m)
    return "\n".join(resposta)


def obs(api, venda_id: int) -> str:
    venda = next(v for v in api.vendas if v["id"] == venda_id)
    return str(venda.get("observation") or "")


@pytest.mark.parametrize("frase", ["quero devolver", "devolução"])
def test_devolucao_generica_mostra_o_menu(conversa, frase):
    resposta = falar(conversa, frase)
    assert "1) Cancelar um pedido" in resposta and "4) Falar com um atendente" in resposta


def test_vazamento_vence_a_devolucao(conversa):
    resposta = falar(conversa, "veio vazando, quero devolver")
    assert "ORIENTAÇÕES DE SEGURANÇA 193" in resposta


def test_no_meio_do_pedido_cancelar_pergunta_o_que_fazer(conversa, api):
    resposta = falar(conversa, "sim", "1", "quero cancelar")
    assert "1) Continuar este pedido" in resposta and "2) Desistir deste pedido" in resposta
    assert "3) Falar de um pedido já feito" in resposta
    assert "Pedido descartado. Nada foi enviado." in "\n".join(conversa.responder("2"))
    assert api.vendas_criadas() == []


def test_continuar_este_pedido_repete_a_pergunta_e_conclui(conversa, api):
    falar(conversa, "sim", "1", "veio com lacre violado")
    assert conversa.responder("1") == ["Quantos?"]
    conversa.responder("2")
    conversa.responder("sim")
    conversa.responder("1")
    conversa.responder("2")
    assert conversa.responder("1")[0].startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_cancelamento_completo_anota_na_venda_via_api(conversa, api):
    venda = semear(api, observacao="[chatbot:orig]")
    falar(conversa, "quero cancelar", "1")
    assert "Você já pagou" in "\n".join(conversa.responder("1"))
    resumo = "\n".join(conversa.responder("não"))
    assert "Vou pedir à equipe: cancelar" in resumo and f"Pedido {venda}" in resumo
    final = "\n".join(conversa.responder("1"))

    assert f"ERP-{venda}" in final
    assert "[chatbot:orig]" in obs(api, venda)
    assert "[CHATBOT][CANCELAMENTO]" in obs(api, venda)


def test_problema_no_produto_anota_na_venda_via_api(conversa, api):
    venda = semear(api)
    resumo = falar(conversa, "veio com lacre violado", "1")
    assert "verificar um problema no produto" in resumo
    final = "\n".join(conversa.responder("1"))

    assert f"ERP-{venda}" in final
    assert "[CHATBOT][PROBLEMA_PRODUTO]" in obs(api, venda)
    assert "lacre violado" in obs(api, venda)


def test_cobranca_errada_anota_estorno_na_venda_via_api(conversa, api):
    venda = semear(api)
    falar(conversa, "paguei duas vezes", "1")
    resumo = "\n".join(conversa.responder("2"))
    assert "Preferência: dinheiro de volta" in resumo
    final = "\n".join(conversa.responder("1"))

    assert f"ERP-{venda}" in final
    assert "[CHATBOT][ESTORNO]" in obs(api, venda)
    assert "dinheiro de volta" in obs(api, venda)


def test_nao_na_confirmacao_nao_atualiza_venda(conversa, api):
    venda = semear(api)
    falar(conversa, "quero cancelar", "1", "1", "não")
    assert "nada foi registrado" in "\n".join(conversa.responder("2")).lower()
    assert "atualizar_venda" not in api.operacoes
    assert obs(api, venda) == ""


@pytest.mark.parametrize("pagamento", ["MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"])
def test_pago_na_entrega_nao_pergunta_se_pagou(conversa, api, pagamento):
    venda = semear(api, pagamento=pagamento)
    resumo = falar(conversa, "quero cancelar", "1", "1")
    assert "Você já pagou" not in resumo
    assert "Confirma? 1) sim 2) não" in resumo
    assert f"ERP-{venda}" in "\n".join(conversa.responder("1"))


def test_pedido_entregue_oferece_opcoes(conversa, api):
    semear(api, status="CONFIRM")
    resposta = falar(conversa, "quero cancelar", "1")
    assert "Esse pedido já foi entregue." in resposta
    assert "1) Problema com o produto" in resposta and "2) Devolução do valor" in resposta
    assert "3) Falar com um atendente" in resposta


def test_depois_do_pedido_pergunta_direto_pelo_pedido_feito(conversa, api):
    falar(conversa, *ATE_O_PEDIDO)
    [venda] = api.vendas_criadas()
    resp = "\n".join(conversa.responder("cancelar"))
    assert f"Quer cancelar o pedido {venda['id']}, que você acabou de fazer?" in resp


def test_aviso_entregador_quando_pedido_recente(conversa, api):
    semear(api, horas_atras=1)
    final = falar(conversa, "quero cancelar", "1", "1", "não", "1")
    assert "O entregador pode já estar a caminho." in final


def test_falha_no_put_sales_nao_inventa_protocolo(conversa, api):
    semear(api)
    api.roteirizar("atualizar_venda", Resposta(500, {"message": "erro"}))
    final = falar(conversa, "quero cancelar", "1", "1", "não", "1")
    assert "ERP-" not in final
    assert "Não consegui registrar" in final


def test_modo_real_so_pedidos_do_chatbot(dependencias, api):
    do_chatbot = semear(api, cliente=5700355, observacao="DEMONSTRAÇÃO DO CHATBOT - NÃO ENTREGAR. [chatbot:abc]")
    semear(api, cliente=5700355, observacao="venda feita no painel", dias_atras=2)
    real = replace(
        dependencias,
        devolucao=Devolucao(api=dependencias.api, relogio=api.relogio, cliente_fixo=5700355, catalogo=CATALOGO),
    )
    resposta = falar(Conversa("5586999990000", real, numero_confiavel=False), "quero cancelar")
    assert f"Pedido {do_chatbot}" in resposta and "É esse?" in resposta

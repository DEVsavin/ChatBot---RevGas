"""Testes do WA2: o envio de respostas pela API da Meta (Graph API), sem internet.

Seam combinado: EnviadorWhatsApp(...).enviar(para, texto) com um transporte
falso do httpx no lugar da Meta. Token fictício; telefones fictícios (DDD 00).
"""

import json

import httpx
import pytest

from revgas_chatbot.canais.enviador_whatsapp import VERSAO, EnviadorWhatsApp

TOKEN = "token-ficticio-de-teste-789"
PHONE_NUMBER_ID = "100000000000001"
COM_9 = "5500900000001"  # 13 dígitos
SEM_9 = "550000000001"  # o mesmo celular, 12 dígitos


def com_meta(responder):
    recebidas: list[httpx.Request] = []

    def tratar(request):
        recebidas.append(request)
        return responder(request)

    return EnviadorWhatsApp(TOKEN, PHONE_NUMBER_ID, transporte=httpx.MockTransport(tratar)), recebidas


def ok(request):
    return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.R"}]})


def erro(status, codigo=None):
    return lambda r: httpx.Response(status, json={"error": {"message": "erro", "code": codigo or status}})


def para_quem(request):
    return json.loads(request.content)["to"]


def test_envia_texto_pela_graph_api():
    meta, recebidas = com_meta(ok)

    assert meta.enviar(COM_9, "Olá!") is True
    [r] = recebidas
    assert r.method == "POST"
    assert str(r.url) == f"https://graph.facebook.com/{VERSAO}/{PHONE_NUMBER_ID}/messages"
    assert r.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(r.content) == {
        "messaging_product": "whatsapp", "to": COM_9, "type": "text", "text": {"body": "Olá!"},
    }


def test_a_versao_e_fixa():
    assert VERSAO == "v26.0"


def test_o_token_nunca_vai_na_url():
    meta, recebidas = com_meta(ok)
    meta.enviar(COM_9, "oi")

    assert TOKEN not in str(recebidas[0].url)


def test_espera_no_maximo_10_segundos():
    meta, recebidas = com_meta(ok)
    meta.enviar(COM_9, "oi")

    assert recebidas[0].extensions["timeout"]["read"] == 10


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_erro_vira_false_sem_nova_tentativa(status):
    meta, recebidas = com_meta(erro(status))

    assert meta.enviar(COM_9, "oi") is False
    assert len(recebidas) == 1  # nunca reenvia: evita mensagem duplicada


@pytest.mark.parametrize("falha", [httpx.ReadTimeout("demorou"), httpx.ConnectError("sem rede")])
def test_falha_de_rede_vira_false(falha):
    def levanta(request):
        raise falha

    meta, recebidas = com_meta(levanta)

    assert meta.enviar(COM_9, "oi") is False
    assert len(recebidas) == 1


def test_resposta_200_que_nao_e_json_conta_como_enviada():
    meta, _ = com_meta(lambda r: httpx.Response(200, text="ok"))

    assert meta.enviar(COM_9, "oi") is True


# --- I2: 131030 (destinatário fora da lista) → uma tentativa com a outra forma ------------


def recusa_forma(recusada):
    def tratar(request):
        if para_quem(request) == recusada:
            return httpx.Response(400, json={"error": {"message": "Recipient phone number not in allowed list",
                                                       "code": 131030}})
        return ok(request)
    return tratar


@pytest.mark.parametrize("veio, outra", [(SEM_9, COM_9), (COM_9, SEM_9)])
def test_131030_tenta_uma_vez_a_outra_forma_do_numero(veio, outra):
    meta, recebidas = com_meta(recusa_forma(veio))

    assert meta.enviar(veio, "oi") is True
    assert [para_quem(r) for r in recebidas] == [veio, outra]


def test_depois_usa_direto_a_forma_que_funcionou():
    meta, recebidas = com_meta(recusa_forma(SEM_9))
    meta.enviar(SEM_9, "um")
    meta.enviar(SEM_9, "dois")

    assert [para_quem(r) for r in recebidas] == [SEM_9, COM_9, COM_9]


def test_131030_nas_duas_formas_vira_false_com_duas_tentativas_so():
    meta, recebidas = com_meta(erro(400, 131030))

    assert meta.enviar(SEM_9, "oi") is False
    assert len(recebidas) == 2


def test_131030_de_numero_sem_outra_forma_nao_tenta_de_novo():
    fixo = "550033330000"  # 12 dígitos de telefone fixo: não existe forma "com o 9"
    meta, recebidas = com_meta(erro(400, 131030))

    assert meta.enviar(fixo, "oi") is False
    assert len(recebidas) == 1


# --- log ----------------------------------------------------------------------------------


def test_log_so_com_o_status_sem_token_numero_ou_texto(caplog):
    meta, _ = com_meta(erro(500))
    with caplog.at_level("DEBUG"):
        meta.enviar(COM_9, "Rua Secreta 7")

    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert "500" in registros
    for segredo in (TOKEN, COM_9, SEM_9, "Rua Secreta"):
        assert segredo not in registros


def test_token_recusado_diz_o_que_fazer(caplog):
    meta, _ = com_meta(erro(401))
    with caplog.at_level("WARNING"):
        meta.enviar(COM_9, "oi")

    assert "token do WhatsApp recusado (401)" in caplog.text


def test_token_e_id_sao_obrigatorios():
    with pytest.raises(ValueError):
        EnviadorWhatsApp(" ", PHONE_NUMBER_ID)
    with pytest.raises(ValueError):
        EnviadorWhatsApp(TOKEN, "")

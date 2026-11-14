"""Testes do WH1: o cliente que fala com o WAHA (envio e consulta de @lid), sem internet.

Seam combinado (pedido de 14/11): ClienteWaha(...) com um transporte falso do
httpx no lugar do WAHA. Chave e números fictícios (DDD 00).
"""

import json

import httpx
import pytest

from revgas_chatbot.canais.cliente_waha import ClienteWaha

URL = "http://127.0.0.1:3000"
CHAVE = "chave-ficticia-do-waha"
MARIA = "5500900000001"


def com_waha(responder, url=URL):
    recebidas: list[httpx.Request] = []

    def tratar(request):
        recebidas.append(request)
        return responder(request)

    return ClienteWaha(url, CHAVE, "default", transporte=httpx.MockTransport(tratar)), recebidas


def ok(status=201, corpo=None):
    return lambda r: httpx.Response(status, json=corpo if corpo is not None else {"id": "msg-1"})


def test_envia_texto_pelo_sendtext():
    waha, recebidas = com_waha(ok())

    assert waha.enviar(MARIA, "Olá!") is True
    [r] = recebidas
    assert (r.method, str(r.url)) == ("POST", f"{URL}/api/sendText")
    assert r.headers["x-api-key"] == CHAVE
    assert json.loads(r.content) == {"session": "default", "chatId": f"{MARIA}@c.us", "text": "Olá!"}


def test_responde_no_chat_id_que_chegou():
    waha, recebidas = com_waha(ok())
    waha.lembrar(MARIA, "123456789@lid")
    waha.enviar(MARIA, "oi")

    assert json.loads(recebidas[0].content)["chatId"] == "123456789@lid"


@pytest.mark.parametrize("status", [200, 201])
def test_200_ou_201_e_enviado(status):
    waha, _ = com_waha(ok(status))

    assert waha.enviar(MARIA, "oi") is True


@pytest.mark.parametrize("status", [400, 401, 404, 422, 500])
def test_erro_vira_false_sem_reenvio(status):
    waha, recebidas = com_waha(ok(status, {"message": "erro"}))

    assert waha.enviar(MARIA, "oi") is False
    assert len(recebidas) == 1


@pytest.mark.parametrize("falha", [httpx.ReadTimeout("demorou"), httpx.ConnectError("sem rede")])
def test_falha_de_rede_vira_false(falha):
    def levanta(r):
        raise falha

    waha, recebidas = com_waha(levanta)

    assert waha.enviar(MARIA, "oi") is False
    assert len(recebidas) == 1


def test_espera_no_maximo_10_segundos():
    waha, recebidas = com_waha(ok())
    waha.enviar(MARIA, "oi")

    assert recebidas[0].extensions["timeout"]["read"] == 10


def test_nada_vai_para_o_log(caplog):
    waha, _ = com_waha(ok(500))
    with caplog.at_level("DEBUG"):
        waha.enviar(MARIA, "Rua Secreta 7")

    assert "500" in caplog.text
    for segredo in (CHAVE, MARIA, "Rua Secreta"):
        assert segredo not in caplog.text


# --- @lid: identificador oculto → telefone ------------------------------------------------


def test_numero_do_lid():
    waha, recebidas = com_waha(ok(200, {"lid": "123456789@lid", "pn": f"{MARIA}@c.us"}))

    assert waha.numero_do_lid("123456789@lid") == MARIA
    [r] = recebidas
    assert r.method == "GET"
    assert r.url.raw_path.decode() == "/api/default/lids/123456789%40lid"
    assert r.headers["x-api-key"] == CHAVE


@pytest.mark.parametrize("responder", [
    ok(200, {"lid": "123456789@lid", "pn": None}),
    ok(200, {}),
    ok(404, {"message": "not found"}),
    lambda r: httpx.Response(200, text="não é json"),
])
def test_lid_sem_numero_vira_none(responder):
    waha, _ = com_waha(responder)

    assert waha.numero_do_lid("123456789@lid") is None


# --- endereço do WAHA: só local ------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://waha.exemplo.com", "http://192.168.0.10:3000", "http://0.0.0.0:3000", ""])
def test_waha_que_nao_e_local_e_recusado(url):
    with pytest.raises(ValueError):
        ClienteWaha(url, CHAVE, "default")


@pytest.mark.parametrize("url", ["http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:3000/"])
def test_waha_local_e_aceito(url):
    ClienteWaha(url, CHAVE, "default")


def test_chave_obrigatoria():
    with pytest.raises(ValueError):
        ClienteWaha(URL, " ", "default")

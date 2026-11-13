"""Testes do WA1: o webhook do WhatsApp, com a Meta simulada.

Seam combinado: canal_whatsapp.criar_app_whatsapp(...) com o TestClient do
FastAPI (sem abrir porta, sem internet). Os corpos são assinados com um
segredo de teste; as mensagens aceitas vão para um coletor falso.
Telefones fictícios (DDD 00).
"""

import hashlib
import hmac
import json
import threading

import pytest
from fastapi.testclient import TestClient

from revgas_chatbot.canais.canal_whatsapp import MensagemRecebida, criar_app_whatsapp

SEGREDO = "segredo-de-teste-do-app"
VERIFY_TOKEN = "token-de-verificacao-de-teste-com-mais-de-32-caracteres"
PHONE_NUMBER_ID = "100000000000001"
DOMINIO = "exemplo-fixo.ngrok-free.app"
BASE = f"https://{DOMINIO}"
MARIA = "5500900000001"


@pytest.fixture
def recebidas():
    return []


@pytest.fixture
def cliente(recebidas):
    app = criar_app_whatsapp(
        receber=recebidas.append,
        app_secret=SEGREDO,
        verify_token=VERIFY_TOKEN,
        phone_number_id=PHONE_NUMBER_ID,
        dominio=DOMINIO,
        porta=8001,
    )
    return TestClient(app, base_url=BASE)


def assinar(corpo: bytes, segredo: str = SEGREDO) -> str:
    return "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()


def mensagem(texto="oi", *, id="wamid.1", de=MARIA, tipo="text", **extra):
    m = {"from": de, "id": id, "timestamp": "1762970000", "type": tipo}
    if tipo == "text":
        m["text"] = {"body": texto}
    m.update(extra)
    return m


def evento(*mensagens, phone_number_id=PHONE_NUMBER_ID, statuses=None):
    valor = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_number_id},
    }
    if mensagens:
        valor["contacts"] = [{"profile": {"name": "Teste"}, "wa_id": mensagens[0]["from"]}]
        valor["messages"] = list(mensagens)
    if statuses:
        valor["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "200000000000002", "changes": [{"field": "messages", "value": valor}]}]}


def postar(cliente, dados, *, assinatura=None, cru: bytes | None = None, **cabecalhos):
    corpo = cru if cru is not None else json.dumps(dados).encode()
    headers = {"Content-Type": "application/json", **cabecalhos}
    if assinatura is not False:
        headers["X-Hub-Signature-256"] = assinatura or assinar(corpo)
    return cliente.post("/webhook", content=corpo, headers=headers)


# --- verificação (GET) ------------------------------------------------------------------


def test_verificacao_com_o_token_certo_devolve_o_challenge(cliente):
    r = cliente.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                        "hub.challenge": "1158201444"})

    assert r.status_code == 200
    assert r.text == "1158201444"
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"},
    {},
])
def test_verificacao_sem_o_token_certo_e_recusada(cliente, params):
    assert cliente.get("/webhook", params=params).status_code == 403


# --- assinatura (POST) ------------------------------------------------------------------


def test_mensagem_de_texto_assinada_chega_ao_coletor(cliente, recebidas):
    r = postar(cliente, evento(mensagem("quero gás")))

    assert r.status_code == 200
    assert recebidas == [MensagemRecebida(telefone=MARIA, id="wamid.1", texto="quero gás")]


@pytest.mark.parametrize("assinatura", [False, "sha256=", "sha256=abc", "md5=" + "0" * 64, "0" * 64])
def test_sem_assinatura_valida_e_recusado_e_nada_e_processado(cliente, recebidas, assinatura):
    r = postar(cliente, evento(mensagem()), assinatura=assinatura)

    assert r.status_code == 403
    assert recebidas == []


def test_assinatura_de_outro_segredo_e_recusada(cliente, recebidas):
    corpo = json.dumps(evento(mensagem())).encode()
    r = postar(cliente, None, cru=corpo, assinatura=assinar(corpo, "outro-segredo"))

    assert r.status_code == 403
    assert recebidas == []


def test_assinatura_vale_para_o_corpo_cru_e_nao_para_o_json(cliente, recebidas):
    original = json.dumps(evento(mensagem())).encode()
    com_espacos = json.dumps(evento(mensagem()), indent=2).encode()  # mesmo JSON, outros bytes
    r = postar(cliente, None, cru=com_espacos, assinatura=assinar(original))

    assert r.status_code == 403
    assert recebidas == []


# --- Host, tamanho e rotas --------------------------------------------------------------


@pytest.mark.parametrize("rota, metodo", [("/webhook", "get"), ("/webhook", "post"), ("/fila", "get")])
@pytest.mark.parametrize("host", ["site-malicioso.com", "outro.ngrok-free.app", "127.0.0.1:9999"])
def test_host_que_nao_e_o_dominio_nem_local_e_recusado(cliente, recebidas, rota, metodo, host):
    corpo = json.dumps(evento(mensagem())).encode()
    r = getattr(cliente, metodo)(rota, headers={"Host": host, "X-Hub-Signature-256": assinar(corpo)},
                                 **({"content": corpo} if metodo == "post" else {}))

    assert r.status_code == 400
    assert recebidas == []


@pytest.mark.parametrize("host", ["127.0.0.1:8001", "localhost:8001", DOMINIO])
def test_dominio_e_local_sao_aceitos(cliente, recebidas, host):
    r = postar(cliente, evento(mensagem()), Host=host)

    assert r.status_code == 200
    assert len(recebidas) == 1


def test_corpo_acima_de_256_kb_e_recusado(cliente, recebidas):
    corpo = json.dumps(evento(mensagem("a" * (256 * 1024)))).encode()
    r = postar(cliente, None, cru=corpo)

    assert r.status_code == 413
    assert recebidas == []


@pytest.mark.parametrize("rota, metodo", [("/", "get"), ("/fila", "get"), ("/conversas", "post"),
                                          ("/docs", "get"), ("/openapi.json", "get")])
def test_so_existe_a_rota_do_webhook(cliente, rota, metodo):
    # W5: a página do chat e a fila (dados pessoais) nunca passam pelo túnel.
    assert getattr(cliente, metodo)(rota).status_code == 404


# --- o que é processado -----------------------------------------------------------------


def test_a_mesma_mensagem_entregue_duas_vezes_chega_uma_vez(cliente, recebidas):
    postar(cliente, evento(mensagem("sim", id="wamid.X")))
    r = postar(cliente, evento(mensagem("sim", id="wamid.X")))

    assert r.status_code == 200
    assert len(recebidas) == 1


def test_a_mesma_mensagem_ao_mesmo_tempo_chega_uma_vez(cliente, recebidas):
    corpo = json.dumps(evento(mensagem("sim", id="wamid.Y"))).encode()
    threads = [threading.Thread(target=postar, args=(cliente, None), kwargs={"cru": corpo}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(recebidas) == 1


def test_varias_mensagens_no_mesmo_evento_chegam_na_ordem(cliente, recebidas):
    postar(cliente, evento(mensagem("um", id="wamid.1"), mensagem("dois", id="wamid.2")))

    assert [m.texto for m in recebidas] == ["um", "dois"]


def test_lembra_so_os_ultimos_ids(recebidas):
    app = criar_app_whatsapp(receber=recebidas.append, app_secret=SEGREDO, verify_token=VERIFY_TOKEN,
                             phone_number_id=PHONE_NUMBER_ID, dominio=DOMINIO, ids_lembrados=2)
    cliente = TestClient(app, base_url=BASE)
    for i in (1, 2, 3, 1):  # o 1 já saiu da memória quando volta
        postar(cliente, evento(mensagem(id=f"wamid.{i}")))

    assert [m.id for m in recebidas] == ["wamid.1", "wamid.2", "wamid.3", "wamid.1"]


@pytest.mark.parametrize("dados", [
    evento(statuses=[{"id": "wamid.1", "status": "delivered", "recipient_id": MARIA}]),
    evento(mensagem(), phone_number_id="999"),
    evento(mensagem(de="123")),
    evento(mensagem(de="15551234567")),  # número de fora do Brasil
    evento(mensagem(tipo="reaction", reaction={"message_id": "wamid.0", "emoji": "👍"})),
    {"object": "outra_coisa"},
    {"entry": "não é lista"},
    [1, 2, 3],
])
def test_o_que_nao_e_para_nos_responde_200_e_nao_processa(cliente, recebidas, dados):
    r = postar(cliente, dados)

    assert r.status_code == 200
    assert recebidas == []


def test_json_invalido_assinado_responde_200_e_nao_processa(cliente, recebidas):
    r = postar(cliente, None, cru=b"{isto nao e json")

    assert r.status_code == 200
    assert recebidas == []


@pytest.mark.parametrize("tipo, extra", [
    ("audio", {"audio": {"id": "1"}}),
    ("image", {"image": {"id": "1"}}),
    ("location", {"location": {"latitude": -5.0, "longitude": -42.0}}),
    ("sticker", {"sticker": {"id": "1"}}),
])
def test_mensagem_que_nao_e_texto_chega_sem_texto(cliente, recebidas, tipo, extra):
    postar(cliente, evento(mensagem(tipo=tipo, **extra)))

    assert recebidas == [MensagemRecebida(telefone=MARIA, id="wamid.1", texto=None)]


def test_numero_sem_o_9_e_aceito_como_veio(cliente, recebidas):
    postar(cliente, evento(mensagem(de="550000000001")))  # 12 dígitos

    assert recebidas[0].telefone == "550000000001"


def test_nada_vai_para_o_log(cliente, caplog):
    with caplog.at_level("DEBUG"):
        postar(cliente, evento(mensagem("Rua Secreta 7")))
        postar(cliente, evento(mensagem()), assinatura="sha256=" + "0" * 64)
        cliente.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"})

    registros = "\n".join(r.getMessage() for r in caplog.records)
    for segredo in (SEGREDO, VERIFY_TOKEN, MARIA, "Rua Secreta"):
        assert segredo not in registros

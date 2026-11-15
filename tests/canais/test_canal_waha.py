"""Testes do WH1: o receptor de webhooks do WAHA, com o WAHA simulado.

Seam combinado (pedido de 14/11): criar_app_waha(...) com o TestClient do
FastAPI e webhooks assinados com uma chave de teste (HMAC sha512). Nada é
enviado ao WhatsApp. Telefones fictícios (DDD 00) ou inventados.
"""

import hashlib
import hmac
import itertools
import json
import threading
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.canal_waha import ConfiguracaoInvalida, configuracao_waha, criar_app_waha, main
from revgas_chatbot.canais.atendimento_whatsapp import NAO_TEXTO, AtendimentoWhatsApp, MensagemRecebida
from revgas_chatbot.canais.cliente_waha import ClienteWaha
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.montagem import api_de_demonstracao, montar

CHAVE_HMAC = "chave-hmac-de-teste-com-mais-de-32-caracteres"
BASE = "http://127.0.0.1:8002"
AGORA = datetime(2026, 11, 15, 10, 0, tzinfo=FUSO_TERESINA)
MARIA = "5500900000001"  # fictício
EQUIPE = "5586999990000"  # inventado
EQUIPE_SEM_9 = "558699990000"
OUTRO = "5586988880000"  # inventado, fora da equipe


class ClienteFalso:
    """No lugar do ClienteWaha: guarda os chatIds e responde a consulta de @lid."""

    def __init__(self, lids=None):
        self.chats = {}
        self.lids = lids or {}
        self.consultas = []

    def lembrar(self, telefone, chat_id):
        self.chats[telefone] = chat_id

    def numero_do_lid(self, lid):
        self.consultas.append(lid)
        return self.lids.get(lid)


def assinar(corpo: bytes, chave: str = CHAVE_HMAC) -> str:
    return hmac.new(chave.encode(), corpo, hashlib.sha512).hexdigest()


_ids = itertools.count(1)


def evento(texto="oi", *, de=f"{MARIA}@c.us", id=None, from_me=False, has_media=False, event="message",
           session="default", timestamp=None):
    return {
        "id": "evt_01", "timestamp": int(AGORA.timestamp() * 1000), "event": event, "session": session,
        "engine": "WEBJS", "me": {"id": "5586900000000@c.us", "pushName": "Chip"},
        "payload": {
            "id": id or f"false_{de}_{next(_ids)}", "timestamp": timestamp or int(AGORA.timestamp()),
            "from": de, "fromMe": from_me, "to": "5586900000000@c.us", "body": texto, "hasMedia": has_media,
        },
    }


def montar_app(recebidas, *, cliente=None, atende=None, relogio=None, **extra):
    return criar_app_waha(
        receber=recebidas.append,
        cliente=cliente or ClienteFalso(),
        hmac_key=CHAVE_HMAC,
        sessao="default",
        atende=atende or (lambda numero: numero in (MARIA, EQUIPE, EQUIPE_SEM_9)),
        relogio=relogio or RelogioFalso(AGORA),
        **extra,
    )


def postar(cliente_http, dados, *, assinatura=None, cru=None, **cabecalhos):
    corpo = cru if cru is not None else json.dumps(dados).encode()
    headers = {"Content-Type": "application/json", **cabecalhos}
    if assinatura is not False:
        headers["X-Webhook-Hmac"] = assinatura if assinatura is not None else assinar(corpo)
        headers["X-Webhook-Hmac-Algorithm"] = "sha512"
    return cliente_http.post("/webhook", content=corpo, headers=headers)


@pytest.fixture
def recebidas():
    return []


@pytest.fixture
def falso():
    return ClienteFalso({"123456789@lid": EQUIPE})


@pytest.fixture
def http(recebidas, falso):
    return TestClient(montar_app(recebidas, cliente=falso), base_url=BASE)


# --- assinatura, Host, tamanho e rotas ---------------------------------------------------


def test_mensagem_assinada_chega_ao_atendimento(http, recebidas, falso):
    r = postar(http, evento("quero gás", id="msg-1"))

    assert r.status_code == 200
    assert recebidas == [MensagemRecebida(telefone=MARIA, id="msg-1", texto="quero gás")]
    assert falso.chats == {MARIA: f"{MARIA}@c.us"}


@pytest.mark.parametrize("assinatura", [False, "", "abc", "0" * 128])
def test_sem_assinatura_valida_e_recusado(http, recebidas, assinatura):
    assert postar(http, evento(), assinatura=assinatura).status_code == 403
    assert recebidas == []


def test_assinatura_de_outra_chave_ou_de_outro_corpo_e_recusada(http, recebidas):
    original = json.dumps(evento()).encode()
    assert postar(http, None, cru=original, assinatura=assinar(original, "outra-chave")).status_code == 403
    com_espacos = json.dumps(json.loads(original), indent=2).encode()
    assert postar(http, None, cru=com_espacos, assinatura=assinar(original)).status_code == 403
    assert recebidas == []


@pytest.mark.parametrize("host", ["site-malicioso.com", "127.0.0.1:9999", "192.168.0.10:8002"])
def test_host_fora_da_lista_e_recusado(http, recebidas, host):
    assert postar(http, evento(), Host=host).status_code == 400
    assert http.get("/qualquer", headers={"Host": host}).status_code == 400
    assert recebidas == []


@pytest.mark.parametrize("host", ["host.docker.internal:8002", "127.0.0.1:8002", "localhost:8002"])
def test_hosts_aceitos(http, recebidas, host):
    assert postar(http, evento(), Host=host).status_code == 200
    assert len(recebidas) == 1


def test_corpo_acima_de_256_kb_e_recusado(http, recebidas):
    assert postar(http, evento("a" * (256 * 1024))).status_code == 413
    assert recebidas == []


@pytest.mark.parametrize("rota", ["/", "/fila", "/dashboard", "/docs", "/openapi.json"])
def test_so_existe_o_webhook(http, rota):
    assert http.get(rota).status_code == 404


# --- filtros: a quem o bot responde -------------------------------------------------------


@pytest.mark.parametrize("dados", [
    evento(from_me=True),  # o próprio chip: senão o bot responde a si mesmo
    evento(de="120363000000000000@g.us"),  # grupo
    evento(de="status@broadcast"),
    evento(de="120363000000000001@newsletter"),
    evento(event="message.any"),
    evento(event="session.status"),
    evento(session="outra"),
    evento(de="15551234567@c.us"),  # fora do Brasil
    evento(de=f"{OUTRO}@c.us"),  # fora da equipe
    evento(de="999999999@lid"),  # @lid que o WAHA não conhece
    evento(timestamp=int(AGORA.timestamp()) - 11 * 60),  # mensagem velha
    {"event": "message"},
    [1, 2, 3],
])
def test_ignorados_respondem_200_e_nao_chegam(http, recebidas, dados):
    assert postar(http, dados).status_code == 200
    assert recebidas == []


def test_json_invalido_assinado_responde_200(http, recebidas):
    assert postar(http, None, cru=b"{isto nao e json").status_code == 200
    assert recebidas == []


def test_mensagem_de_9_minutos_ainda_chega(http, recebidas):
    postar(http, evento(timestamp=int(AGORA.timestamp()) - 9 * 60))

    assert len(recebidas) == 1


def test_numero_da_equipe_sem_o_9_chega(http, recebidas):
    postar(http, evento(de=f"{EQUIPE_SEM_9}@c.us"))

    assert recebidas[0].telefone == EQUIPE_SEM_9


def test_formato_s_whatsapp_net_tambem_vale(http, recebidas):
    postar(http, evento(de=f"{MARIA}@s.whatsapp.net"))

    assert recebidas[0].telefone == MARIA


def test_atender_todos_aceita_qualquer_numero_brasileiro(recebidas):
    http = TestClient(montar_app(recebidas, atende=lambda numero: True), base_url=BASE)
    postar(http, evento(de=f"{OUTRO}@c.us"))

    assert recebidas[0].telefone == OUTRO


def test_lid_conhecido_vira_o_telefone_e_a_resposta_vai_para_o_lid(http, recebidas, falso):
    postar(http, evento(de="123456789@lid"))

    assert recebidas[0].telefone == EQUIPE
    assert falso.chats[EQUIPE] == "123456789@lid"


@pytest.mark.parametrize("dados", [evento("", has_media=True), evento("legenda da foto", has_media=True), evento("")])
def test_midia_ou_corpo_vazio_chega_sem_texto(http, recebidas, dados):
    postar(http, dados)

    assert recebidas[0].texto is None


def test_repetida_chega_uma_vez_mesmo_ao_mesmo_tempo(http, recebidas):
    corpo = json.dumps(evento("sim", id="msg-X")).encode()
    threads = [threading.Thread(target=postar, args=(http, None), kwargs={"cru": corpo}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(recebidas) == 1


def test_nada_vai_para_o_log(http, caplog):
    with caplog.at_level("DEBUG"):
        postar(http, evento("Rua Secreta 7"))
        postar(http, evento(de="999999999@lid"))
        postar(http, evento(), assinatura="0" * 128)

    for segredo in (CHAVE_HMAC, MARIA, "Rua Secreta", "999999999"):
        assert segredo not in caplog.text


# --- ponta a ponta: atendimento de verdade, WAHA simulado ---------------------------------


class WahaSimulado:
    def __init__(self, tmp_path):
        self.api = api_de_demonstracao(RelogioFalso(AGORA))
        programa = montar(tmp_path, relogio=self.api.relogio, api=self.api, ambiente={})
        self.enviadas: list[dict] = []

        def tratar(request):
            self.enviadas.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "ok"})

        self.cliente = ClienteWaha("http://127.0.0.1:3000", "chave", "default", transporte=httpx.MockTransport(tratar))
        self.atendimento = AtendimentoWhatsApp(lambda tel: programa(tel, numero_confiavel=True), self.cliente)
        app = criar_app_waha(receber=self.atendimento.receber, cliente=self.cliente, hmac_key=CHAVE_HMAC,
                             sessao="default", atende=lambda n: True, relogio=self.api.relogio)
        self.http = TestClient(app, base_url=BASE)
        self.atendimento.ligar()

    def conversa(self, *textos, de=f"{MARIA}@c.us"):
        for texto in textos:
            assert postar(self.http, evento(texto, de=de)).status_code == 200
            self.atendimento.esperar()
        return [e["text"] for e in self.enviadas if e["chatId"] == de]


def test_oi_recebe_a_saudacao_no_mesmo_chat(tmp_path):
    waha = WahaSimulado(tmp_path)
    respostas = waha.conversa("oi")
    waha.atendimento.desligar()

    assert respostas[0].startswith("Olá! Sou o atendente virtual")
    assert {e["chatId"] for e in waha.enviadas} == {f"{MARIA}@c.us"}


def test_pedido_completo_pelo_waha(tmp_path):
    waha = WahaSimulado(tmp_path)
    respostas = waha.conversa("oi", "sim", "1", "2", "sim", "1", "1")
    waha.atendimento.desligar()

    assert respostas[-1].startswith("Pedido registrado!")
    assert len(waha.api.vendas_criadas()) == 1


def test_foto_recebe_so_entendo_texto(tmp_path):
    waha = WahaSimulado(tmp_path)
    postar(waha.http, evento("", has_media=True))
    waha.atendimento.esperar()
    waha.atendimento.desligar()

    assert [e["text"] for e in waha.enviadas] == [NAO_TEXTO]


# --- configuração e comando ----------------------------------------------------------------


AMBIENTE = {
    "WAHA_URL": "http://127.0.0.1:3000",
    "WAHA_SESSAO": "default",
    "WAHA_API_KEY": "chave-ficticia-do-waha",
    "WAHA_HMAC_KEY": CHAVE_HMAC,
    "WAHA_ATENDER": "equipe",
    "WHATSAPP_NUMEROS_EQUIPE": "5586999990000",
}


def test_configuracao_completa():
    c = configuracao_waha(AMBIENTE)

    assert (c.url, c.sessao, c.atender) == ("http://127.0.0.1:3000", "default", "equipe")
    assert c.atende(EQUIPE) and c.atende(EQUIPE_SEM_9) and not c.atende(OUTRO)


def test_atender_todos():
    c = configuracao_waha({**AMBIENTE, "WAHA_ATENDER": "todos", "WHATSAPP_NUMEROS_EQUIPE": ""})

    assert c.atende(OUTRO)


@pytest.mark.parametrize("mudanca, nome", [
    ({"WAHA_API_KEY": ""}, "WAHA_API_KEY"),
    ({"WAHA_HMAC_KEY": ""}, "WAHA_HMAC_KEY"),
    ({"WAHA_HMAC_KEY": "curta"}, "WAHA_HMAC_KEY"),
    ({"WAHA_URL": "http://192.168.0.10:3000"}, "WAHA_URL"),
    ({"WAHA_ATENDER": "alguns"}, "WAHA_ATENDER"),
    ({"WHATSAPP_NUMEROS_EQUIPE": ""}, "WHATSAPP_NUMEROS_EQUIPE"),
])
def test_configuracao_invalida_diz_o_nome_e_nunca_o_valor(mudanca, nome):
    with pytest.raises(ConfiguracaoInvalida) as erro:
        configuracao_waha({**AMBIENTE, **mudanca})

    assert nome in str(erro.value)
    for valor in ("chave-ficticia-do-waha", CHAVE_HMAC):
        assert valor not in str(erro.value)


def test_sem_url_e_sessao_usa_o_padrao():
    sem = {k: v for k, v in AMBIENTE.items() if k not in ("WAHA_URL", "WAHA_SESSAO", "WAHA_ATENDER")}
    c = configuracao_waha(sem)

    assert (c.url, c.sessao, c.atender) == ("http://127.0.0.1:3000", "default", "equipe")


def test_comando_liga_so_em_127_0_0_1_na_porta_8002(tmp_path, capsys):
    chamadas = []
    main([], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path, ambiente=AMBIENTE)

    [opcoes] = chamadas
    assert (opcoes["host"], opcoes["port"], opcoes["access_log"]) == ("127.0.0.1", 8002, False)
    saida = capsys.readouterr().out
    assert "http://127.0.0.1:3000/dashboard" in saida
    for segredo in ("chave-ficticia-do-waha", CHAVE_HMAC, "5586999990000"):
        assert segredo not in saida


def test_comando_sem_configuracao_nao_liga(tmp_path):
    chamadas = []
    with pytest.raises(SystemExit) as erro:
        main([], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path, ambiente={})

    assert chamadas == []
    assert "WAHA_API_KEY" in str(erro.value)


def test_comando_com_venda_real_avisa(tmp_path, capsys):
    ambiente = {**AMBIENTE, "REVGAS_API_KEY": "k", "REVGAS_API_URL": "https://api.exemplo.test/v1",
                "REVGAS_CLIENTE_DEMO": "5700355"}
    main(["--venda-real"], rodar=lambda app, **o: None, pasta_dados=tmp_path, ambiente=ambiente)

    assert "VENDA REAL LIGADA" in capsys.readouterr().out


def test_comando_nao_aceita_outro_endereco(tmp_path):
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"], rodar=lambda app, **o: None, pasta_dados=tmp_path, ambiente=AMBIENTE)


# --- revisão da spec: I2 (venda real só com "equipe") e I3 (IA só equipe) -----------------


def test_venda_real_com_atender_todos_nao_inicia(tmp_path):
    ambiente = {**AMBIENTE, "WAHA_ATENDER": "todos", "REVGAS_API_KEY": "k",
                "REVGAS_API_URL": "https://api.exemplo.test/v1", "REVGAS_CLIENTE_DEMO": "5700355"}
    chamadas = []
    with pytest.raises(SystemExit) as erro:
        main(["--venda-real"], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path, ambiente=ambiente)

    assert chamadas == []
    assert "WAHA_ATENDER=equipe" in str(erro.value)


def test_com_atender_todos_a_ia_continua_so_para_a_equipe(tmp_path):
    from revgas_chatbot.ia.interpretacao import InterpretadorFalso
    from revgas_chatbot.montagem import regra_da_ia

    api = api_de_demonstracao(RelogioFalso(AGORA))
    ia = InterpretadorFalso()
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={}, interpretador=ia,
                      ia_permitida=regra_da_ia([EQUIPE]))
    enviadas = []
    cliente = ClienteWaha("http://127.0.0.1:3000", "chave", "default",
                          transporte=httpx.MockTransport(lambda r: (enviadas.append(r), httpx.Response(201))[1]))
    atendimento = AtendimentoWhatsApp(lambda tel: programa(tel, numero_confiavel=True), cliente)
    app = criar_app_waha(receber=atendimento.receber, cliente=cliente, hmac_key=CHAVE_HMAC, sessao="default",
                         atende=lambda n: True, relogio=api.relogio)  # "todos"
    http = TestClient(app, base_url=BASE)
    atendimento.ligar()
    for texto in ("oi", "sim", "Fulano", "quero 2 botijões, por favor"):
        postar(http, evento(texto, de=f"{OUTRO}@c.us"))
        atendimento.esperar()
    atendimento.desligar()

    assert len(enviadas) > 0  # foi atendido (só pelo roteiro)
    assert ia.recebidos == []

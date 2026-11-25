"""Testes do W2: o servidor web do chat, pelas rotas HTTP.

Seam combinado: canal_web.criar_app(...) com o TestClient do FastAPI (sem
abrir porta, sem internet), usando a montagem real, e a API de teste. Dados fictícios (DDD 00) e um número "real" inventado só para o teste.
"""

import threading
from dataclasses import replace
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.canal_web import criar_app, main
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.montagem import api_de_demonstracao, montar

BASE = "http://127.0.0.1:8000"
MARIA = "(00) 90000-0001"
REAL = "(86) 99999-0000"  # um número que não é fictício (nunca é chamado)


@pytest.fixture
def api():
    return api_de_demonstracao(RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)))


@pytest.fixture
def cliente(tmp_path, api):
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={})
    return TestClient(criar_app(programa, porta=8000, pasta_dados=tmp_path), base_url=BASE)


def iniciar(cliente, telefone=MARIA):
    resposta = cliente.post("/conversas", json={"telefone": telefone})
    assert resposta.status_code == 200, resposta.text
    return resposta.json()


def enviar(cliente, sessao, texto):
    resposta = cliente.post(f"/conversas/{sessao}/mensagens", json={"texto": texto})
    assert resposta.status_code == 200, resposta.text
    return "\n".join(resposta.json()["mensagens"])


def test_numero_ficticio_comeca_a_conversa_e_reconhece_a_maria(cliente):
    inicio = iniciar(cliente)

    assert "Este número é seu: (00) 90000-0001?" in "\n".join(inicio["mensagens"])
    assert "Olá, Maria!" in enviar(cliente, inicio["sessao"], "sim")


def test_pedido_completo_pela_web(cliente, api):
    sessao = iniciar(cliente)["sessao"]
    for texto in ("sim", "2", "sim", "1", "2"):
        enviar(cliente, sessao, texto)

    assert enviar(cliente, sessao, "1").startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_numero_real_e_tratado_como_digitado(cliente, api):
    # V1: número que não é fictício não revela o cadastro.
    inicio = iniciar(cliente, REAL)
    assert "Qual é o seu nome?" in enviar(cliente, inicio["sessao"], "sim")
    assert "buscar_clientes" not in api.operacoes  # nenhuma busca de cadastro


@pytest.mark.parametrize("telefone", ["", "abc", "123", "１１９９９９９００００"])
def test_numero_invalido_nao_cria_conversa(cliente, telefone):
    resposta = cliente.post("/conversas", json={"telefone": telefone})

    assert resposta.status_code == 400
    assert "Número inválido" in resposta.json()["erro"]


def test_sessao_desconhecida_e_404(cliente):
    resposta = cliente.post("/conversas/inventada/mensagens", json={"texto": "oi"})

    assert resposta.status_code == 404


def test_sessao_e_aleatoria_e_longa(cliente):
    sessoes = {iniciar(cliente)["sessao"] for _ in range(3)}

    assert len(sessoes) == 3
    assert all(len(s) >= 22 for s in sessoes)  # token_urlsafe: >= 128 bits


def test_mensagem_com_mais_de_500_caracteres_e_recusada(cliente, api):
    sessao = iniciar(cliente)["sessao"]
    antes = len(api.operacoes)
    resposta = cliente.post(f"/conversas/{sessao}/mensagens", json={"texto": "a" * 501})

    assert resposta.status_code == 400
    assert len(api.operacoes) == antes  # nada chegou à conversa
    assert "Olá, Maria!" in enviar(cliente, sessao, "sim")  # a conversa continua no mesmo passo


def test_mensagem_com_500_caracteres_e_aceita(cliente):
    sessao = iniciar(cliente)["sessao"]
    resposta = cliente.post(f"/conversas/{sessao}/mensagens", json={"texto": "a" * 500})

    assert resposta.status_code == 200


def test_post_sem_json_e_recusado(cliente):
    resposta = cliente.post("/conversas", content="telefone=00900000001",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    assert resposta.status_code == 415


@pytest.mark.parametrize("rota, metodo", [("/", "get"), ("/fila", "get"), ("/conversas", "post")])
@pytest.mark.parametrize("host", ["site-malicioso.com", "192.168.0.10:8000", "127.0.0.1:9999"])
def test_host_que_nao_e_local_e_recusado_em_todas_as_rotas(cliente, rota, metodo, host):
    # H1: protege contra "DNS rebinding" (um site ler a fila ou mandar mensagens).
    resposta = getattr(cliente, metodo)(rota, headers={"Host": host}, **({"json": {"telefone": MARIA}} if metodo == "post" else {}))

    assert resposta.status_code == 400


def test_localhost_tambem_e_aceito(cliente):
    resposta = cliente.post("/conversas", json={"telefone": MARIA}, headers={"Host": "localhost:8000"})

    assert resposta.status_code == 200


def test_mensagens_ao_mesmo_tempo_sao_atendidas_uma_de_cada_vez(tmp_path, api):
    trava = threading.Lock()
    agora = {"n": 0, "max": 0}
    original = api.buscar_clientes

    def buscar_lento(tel):
        with trava:
            agora["n"] += 1
            agora["max"] = max(agora["max"], agora["n"])
        time.sleep(0.05)
        with trava:
            agora["n"] -= 1
        return original(tel)

    api.buscar_clientes = buscar_lento
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={})
    cliente = TestClient(criar_app(programa, porta=8000, pasta_dados=tmp_path), base_url=BASE)
    sessoes = [iniciar(cliente)["sessao"] for _ in range(2)]

    threads = [threading.Thread(target=enviar, args=(cliente, s, "sim")) for s in sessoes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert agora["max"] == 1


# --- W3: página do chat, página da fila e o comando que liga o servidor -----------------


def test_pagina_do_chat(cliente):
    resposta = cliente.get("/")

    assert resposta.status_code == 200
    assert "text/html" in resposta.headers["content-type"]
    html = resposta.text
    assert "textContent" in html  # as mensagens entram como texto
    assert "innerHTML" not in html  # nunca como HTML
    for de_fora in ("<script src", "<link", "http://", "https://", "@import"):
        assert de_fora not in html  # nada carregado de fora: funciona sem internet
    assert "(00) 90000-0001" in html  # dica dos números de demonstração
    assert "/fila" not in html


def test_comando_liga_o_servidor_so_em_127_0_0_1_na_porta_8000(tmp_path):
    chamadas = []
    main([], rodar=lambda app, **opcoes: chamadas.append(opcoes), pasta_dados=tmp_path, ambiente={})

    [opcoes] = chamadas
    assert opcoes["host"] == "127.0.0.1"
    assert opcoes["port"] == 8000
    assert opcoes["access_log"] is False  # o caminho tem o identificador da sessão


def test_comando_aceita_outra_porta_mas_nunca_outro_endereco(tmp_path):
    chamadas = []
    main(["--porta", "8080"], rodar=lambda app, **opcoes: chamadas.append(opcoes), pasta_dados=tmp_path, ambiente={})
    assert chamadas[0]["host"] == "127.0.0.1" and chamadas[0]["port"] == 8080

    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"], rodar=lambda app, **opcoes: None, pasta_dados=tmp_path, ambiente={})


# --- M5 (D16/D17): venda real ligada só com --venda-real --------------------------------

from revgas_chatbot.montagem import ConfigVendaReal  # noqa: E402

EQUIPE = "(86) 99999-1111"  # inventado
CONFIG_REAL = ConfigVendaReal(url="https://api.exemplo.test/v1", chave="chave-ficticia", cliente_demo=5700355)
AMBIENTE_REAL = {
    "REVGAS_API_KEY": "chave-ficticia",
    "REVGAS_API_URL": "https://api.exemplo.test/v1",
    "REVGAS_CLIENTE_DEMO": "5700355",
}


@pytest.fixture
def real(tmp_path, api):
    # Modo real com a API falsa no lugar da RevGás (nenhuma chamada sai daqui).
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={}, venda_real=CONFIG_REAL)
    return TestClient(criar_app(programa, porta=8000, pasta_dados=tmp_path), base_url=BASE)


def test_modo_real_avisa_na_resposta(real):
    inicio = iniciar(real)

    assert inicio["venda_real"] is True
    assert "VENDA REAL LIGADA" in inicio["aviso"]


def test_sem_modo_real_nao_avisa(cliente):
    inicio = iniciar(cliente)

    assert inicio["venda_real"] is False
    assert not inicio["aviso"]


def test_modo_real_nao_busca_cadastro_e_pede_o_nome(real, api):
    sessao = iniciar(real)["sessao"]

    assert "Qual é o seu nome?" in enviar(real, sessao, "sim")
    assert "buscar_clientes" not in api.operacoes


def test_modo_real_pedido_completo_usa_o_cliente_fixo(real, api):
    sessao = iniciar(real)["sessao"]
    for texto in ("sim", "Fulano", "2", "Rua Inventada", "10", "pular", "Centro", "Teresina", "pular", "pular", "1", "2"):
        enviar(real, sessao, texto)
    resposta = enviar(real, sessao, "1")

    assert resposta.startswith("Pedido registrado!"), resposta
    assert "buscar_clientes" not in api.operacoes and "cadastrar_cliente" not in api.operacoes
    assert "criar_venda" in api.operacoes
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 5700355
    assert venda["broadcast"] == "NONE"
    assert venda["observation"].startswith("DEMONSTRAÇÃO DO CHATBOT - NÃO ENTREGAR.")
    assert "Fulano" not in venda["observation"]


def test_modo_real_uma_so_api_cadenciada(tmp_path, api):
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={}, venda_real=CONFIG_REAL)

    assert programa.dependencias.api is programa.dependencias.registrador._api


def test_comando_com_venda_real_liga_e_avisa(tmp_path, capsys):
    chamadas = []
    main(["--venda-real"], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path, ambiente=AMBIENTE_REAL)

    assert len(chamadas) == 1
    saida = capsys.readouterr().out
    assert "VENDA REAL LIGADA" in saida
    assert "chave-ficticia" not in saida


def test_comando_sem_venda_real_nao_liga_mesmo_com_o_env(tmp_path, capsys):
    main([], rodar=lambda app, **o: None, pasta_dados=tmp_path, ambiente=AMBIENTE_REAL)

    assert "VENDA REAL" not in capsys.readouterr().out


@pytest.mark.parametrize("mudanca, nome", [
    ({"REVGAS_API_KEY": ""}, "REVGAS_API_KEY"),
    ({"REVGAS_API_URL": "http://api.exemplo.test/v1"}, "REVGAS_API_URL"),
    ({"REVGAS_CLIENTE_DEMO": ""}, "REVGAS_CLIENTE_DEMO"),
    ({"REVGAS_CLIENTE_DEMO": "0"}, "REVGAS_CLIENTE_DEMO"),
    ({"REVGAS_CLIENTE_DEMO": "abc"}, "REVGAS_CLIENTE_DEMO"),
])
def test_comando_com_venda_real_e_configuracao_errada_nao_liga(tmp_path, mudanca, nome):
    chamadas = []
    with pytest.raises(SystemExit) as erro:
        main(["--venda-real"], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path,
             ambiente={**AMBIENTE_REAL, **mudanca})

    assert chamadas == []
    assert nome in str(erro.value)
    assert "chave-ficticia" not in str(erro.value)


def test_06_cancelamento_no_chat_web_mostra_o_nome_do_produto(cliente):
    sessao = iniciar(cliente)["sessao"]
    for texto in ("sim", "3", "sim", "1", "2", "1"):
        enviar(cliente, sessao, texto)

    enviar(cliente, sessao, "quero cancelar")  # M2: pergunta pelo pedido desta conversa
    assert "3x botijão de gás" in enviar(cliente, sessao, "2")  # "é outro pedido" → o encontrado


# --- Avisar os entregadores (pedido de 18/11): só com --venda-real e digitando AVISAR ------


def test_venda_real_com_aviso_manda_new_sale(tmp_path, api):
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={},
                      venda_real=replace(CONFIG_REAL, avisar_entregadores=True))
    cliente = TestClient(criar_app(programa, porta=8000, pasta_dados=tmp_path), base_url=BASE)
    inicio = iniciar(cliente)
    assert "AVISA os entregadores" in inicio["aviso"]
    for texto in ("sim", "Fulano", "2", "Rua Inventada", "10", "pular", "Centro", "Teresina", "pular", "pular", "1", "2"):
        enviar(cliente, inicio["sessao"], texto)
    enviar(cliente, inicio["sessao"], "1")

    [venda] = api.vendas_criadas()
    assert venda["broadcast"] == "NEW_SALE"
    assert "deliverer_id" not in venda


def test_venda_real_sem_a_opcao_continua_none(real, api):
    sessao = iniciar(real)["sessao"]
    for texto in ("sim", "Fulano", "2", "Rua Inventada", "10", "pular", "Centro", "Teresina", "pular", "pular", "1", "2", "1"):
        enviar(real, sessao, texto)

    assert api.vendas_criadas()[0]["broadcast"] == "NONE"


def test_avisar_sem_venda_real_nao_inicia(tmp_path):
    chamadas = []
    with pytest.raises(SystemExit) as erro:
        main(["--avisar-entregadores"], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path,
             ambiente=AMBIENTE_REAL, ler=lambda t: "AVISAR")

    assert chamadas == [] and "--venda-real" in str(erro.value)


@pytest.mark.parametrize("digitado", ["", "não", "avisar", "sim"])
def test_avisar_sem_digitar_avisar_nao_inicia(tmp_path, digitado):
    chamadas = []
    with pytest.raises(SystemExit) as erro:
        main(["--venda-real", "--avisar-entregadores"], rodar=lambda app, **o: chamadas.append(o),
             pasta_dados=tmp_path, ambiente=AMBIENTE_REAL, ler=lambda t: digitado)

    assert chamadas == [] and "AVISAR" in str(erro.value)


def test_avisar_digitando_avisar_inicia_com_o_aviso(tmp_path, capsys):
    perguntas, chamadas = [], []
    main(["--venda-real", "--avisar-entregadores"], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path,
         ambiente=AMBIENTE_REAL, ler=lambda t: (perguntas.append(t), " AVISAR ")[1])

    assert len(chamadas) == 1
    assert "AVISA os entregadores" in capsys.readouterr().out
    assert "AVISAR" in perguntas[0]

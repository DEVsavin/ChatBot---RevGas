"""Testes do W2: o servidor web do chat, pelas rotas HTTP.

Seam combinado: canal_web.criar_app(...) com o TestClient do FastAPI (sem
abrir porta, sem internet), usando a montagem real, a API de teste e uma IA
falsa. Dados fictícios (DDD 00) e um número "real" inventado só para o teste.
"""

import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.canal_web import criar_app, main
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.ia.interpretacao import Interpretacao, InterpretadorFalso
from revgas_chatbot.montagem import api_de_demonstracao, montar

BASE = "http://127.0.0.1:8000"
MARIA = "(00) 90000-0001"
REAL = "(86) 99999-0000"  # um número que não é fictício (nunca é chamado)


@pytest.fixture
def ia():
    return InterpretadorFalso()


@pytest.fixture
def api():
    return api_de_demonstracao(RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)))


@pytest.fixture
def cliente(tmp_path, api, ia):
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={}, interpretador=ia)
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
    assert inicio["ia_ligada"] is True
    assert "Olá, Maria!" in enviar(cliente, inicio["sessao"], "sim")


def test_pedido_completo_pela_web(cliente, api):
    sessao = iniciar(cliente)["sessao"]
    for texto in ("sim", "1", "2", "sim", "1"):
        enviar(cliente, sessao, texto)

    assert enviar(cliente, sessao, "1").startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_numero_real_e_tratado_como_digitado_e_sem_ia(cliente, api, ia):
    # V1 + H2: número que não é fictício não revela o cadastro e não usa a IA grátis.
    inicio = iniciar(cliente, REAL)
    assert inicio["ia_ligada"] is False
    assert "IA desligada para este número" in inicio["aviso"]

    assert "Qual é o seu nome?" in enviar(cliente, inicio["sessao"], "sim")
    enviar(cliente, inicio["sessao"], "Fulano")
    enviar(cliente, inicio["sessao"], "quero 2 botijões, por favor")  # o roteiro não entende

    assert ia.recebidos == []  # nenhum texto foi para a IA
    assert api.operacoes == []  # nenhuma busca de cadastro


def test_com_numero_ficticio_a_ia_e_usada(cliente, ia):
    ia.respostas["quero 2 botijões"] = Interpretacao(intencao="PEDIR", produto="botijão de gás", quantidade=2)
    sessao = iniciar(cliente)["sessao"]
    enviar(cliente, sessao, "sim")

    assert "Entregar em" in enviar(cliente, sessao, "quero 2 botijões")
    assert [t for t, _ in ia.recebidos] == ["quero 2 botijões"]


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
    resposta = cliente.post(f"/conversas/{sessao}/mensagens", json={"texto": "a" * 501})

    assert resposta.status_code == 400
    assert api.operacoes == []  # nada chegou à conversa
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


class _IaLenta(InterpretadorFalso):
    """Conta quantas mensagens estão sendo atendidas ao mesmo tempo."""

    def __init__(self):
        super().__init__()
        self._trava = threading.Lock()
        self.agora = 0
        self.maximo = 0

    def interpretar(self, texto, contexto):
        with self._trava:
            self.agora += 1
            self.maximo = max(self.maximo, self.agora)
        time.sleep(0.1)
        with self._trava:
            self.agora -= 1
        return super().interpretar(texto, contexto)  # anota o texto recebido


def test_mensagens_ao_mesmo_tempo_sao_atendidas_uma_de_cada_vez(tmp_path, api):
    ia = _IaLenta()
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={}, interpretador=ia)
    cliente = TestClient(criar_app(programa, porta=8000, pasta_dados=tmp_path), base_url=BASE)
    sessoes = [iniciar(cliente)["sessao"] for _ in range(2)]
    for sessao in sessoes:
        enviar(cliente, sessao, "sim")

    threads = [threading.Thread(target=enviar, args=(cliente, s, "algo que o roteiro não entende")) for s in sessoes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ia.recebidos) == 2
    assert ia.maximo == 1


def test_trocar_para_numero_real_no_meio_da_conversa_desliga_a_ia(cliente, ia):
    # H3: começa com número fictício (IA ligada), o cliente diz "não" e digita um número real.
    sessao = iniciar(cliente)["sessao"]
    for texto in ("não", REAL, "sim", "Fulano"):
        enviar(cliente, sessao, texto)
    enviar(cliente, sessao, "quero 2 botijões, por favor")

    assert ia.recebidos == []


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


def test_pagina_da_fila_sem_solicitacoes(cliente):
    resposta = cliente.get("/fila")

    assert resposta.status_code == 200
    assert "Nenhuma solicitação aberta" in resposta.text


def test_pagina_da_fila_mostra_a_transferencia_feita_pela_web(cliente):
    sessao = iniciar(cliente)["sessao"]
    enviar(cliente, sessao, "sim")
    enviar(cliente, sessao, "quero falar com um atendente")

    assert "ATD-" in cliente.get("/fila").text


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

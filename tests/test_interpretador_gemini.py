"""Testes do C2d: o adaptador do Gemini, sem internet.

Seam combinado: InterpretadorGemini(...).interpretar(texto, contexto), com um
transporte falso do httpx no lugar do Google. A chave é fictícia.
"""

import json

import httpx
import pytest

from revgas_chatbot.interpretacao import Contexto, Interpretacao
from revgas_chatbot.interpretador_gemini import InterpretadorGemini

CHAVE = "chave-ficticia-de-teste-123"
CONTEXTO = Contexto(passo="PRODUTO", produtos=("botijão de gás",), pagamentos=("PIX", "Dinheiro"))


def resposta_do_google(conteudo: dict | str, **extra) -> dict:
    texto = conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False)
    candidato = {"content": {"parts": [{"text": texto}]}, "finishReason": "STOP"}
    candidato.update(extra)
    return {"candidates": [candidato]}


def com_google(responder):
    """Monta o interpretador com um 'Google' falso; devolve também as requisições recebidas."""
    recebidas: list[httpx.Request] = []

    def tratar(request: httpx.Request) -> httpx.Response:
        recebidas.append(request)
        return responder(request)

    interpretador = InterpretadorGemini(CHAVE, transporte=httpx.MockTransport(tratar))
    return interpretador, recebidas


def test_le_a_resposta_estruturada():
    resposta = {"intencao": "PEDIR", "produto": "botijão de gás", "quantidade": 2,
                "endereco": {"rua": "Rua Nova", "numero": "10"}, "pagamento": "PIX"}
    ia, _ = com_google(lambda r: httpx.Response(200, json=resposta_do_google(resposta)))

    assert ia.interpretar("quero 2 botijões", CONTEXTO) == Interpretacao(
        intencao="PEDIR", produto="botijão de gás", quantidade=2,
        endereco={"rua": "Rua Nova", "numero": "10"}, pagamento="PIX", troco=None,
    )


def test_envia_ao_modelo_com_a_chave_no_cabecalho_e_so_o_texto_e_as_listas():
    ia, recebidas = com_google(lambda r: httpx.Response(200, json=resposta_do_google({"intencao": "OUTRA"})))
    ia.interpretar("quero gás", CONTEXTO)

    [requisicao] = recebidas
    assert requisicao.method == "POST"
    assert str(requisicao.url).startswith("https://generativelanguage.googleapis.com/v1beta/models/")
    assert str(requisicao.url).endswith(":generateContent")
    assert CHAVE not in str(requisicao.url)  # a chave vai no cabeçalho, nunca na URL
    assert requisicao.headers["x-goog-api-key"] == CHAVE
    corpo = requisicao.content.decode()
    assert "quero gás" in corpo and "botijão de gás" in corpo and "Dinheiro" in corpo
    assert json.loads(corpo)["generationConfig"]["responseMimeType"] == "application/json"


def test_espera_no_maximo_8_segundos():
    ia, recebidas = com_google(lambda r: httpx.Response(200, json=resposta_do_google({"intencao": "OUTRA"})))
    ia.interpretar("oi", CONTEXTO)

    assert recebidas[0].extensions["timeout"]["read"] == 8


def _levanta(erro):
    def tratar(request):
        raise erro
    return tratar


@pytest.mark.parametrize(
    "responder",
    [
        lambda r: httpx.Response(400, json={"error": {"message": f"API key {CHAVE} not valid"}}),
        lambda r: httpx.Response(403, json={"error": {}}),
        lambda r: httpx.Response(429, json={"error": {}}),
        lambda r: httpx.Response(500, text="erro"),
        _levanta(httpx.ReadTimeout("demorou")),
        _levanta(httpx.ConnectError("sem rede")),
        lambda r: httpx.Response(200, text="não é json"),
        lambda r: httpx.Response(200, json={"candidates": []}),
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
        lambda r: httpx.Response(200, json=resposta_do_google({"intencao": "PEDIR"}, finishReason="SAFETY")),
        lambda r: httpx.Response(200, json=resposta_do_google("isto não é json")),
        lambda r: httpx.Response(200, json=resposta_do_google("[1, 2]")),
        lambda r: httpx.Response(200, json=resposta_do_google({"sem_intencao": True})),
    ],
)
def test_qualquer_falha_vira_none_sem_excecao(responder):
    ia, _ = com_google(responder)

    assert ia.interpretar("quero gás", CONTEXTO) is None


def test_a_chave_e_o_texto_nunca_vao_para_o_log(caplog):
    ia, _ = com_google(lambda r: httpx.Response(400, json={"error": {"message": f"API key {CHAVE} not valid"}}))
    with caplog.at_level("DEBUG"):
        ia.interpretar("meu endereço é Rua Secreta 7", CONTEXTO)

    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert CHAVE not in registros
    assert "Rua Secreta" not in registros

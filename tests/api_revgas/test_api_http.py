"""Testes do M4: o adaptador HTTP da API da RevGás, sem internet.

Seam combinado: ApiRevGasHttp(...).<operação>(...), com um transporte falso
do httpx no lugar da RevGás. A chave é fictícia; os dados também.
"""

import json
from datetime import date

import httpx
import pytest

from revgas_chatbot.api_revgas.api import Resposta, TempoEsgotado
from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp

URL = "https://api.exemplo.test/v1"
CHAVE = "chave-ficticia-de-teste-456"


def com_revgas(responder):
    """Monta o adaptador com uma 'RevGás' falsa; devolve também as requisições recebidas."""
    recebidas: list[httpx.Request] = []

    def tratar(request: httpx.Request) -> httpx.Response:
        recebidas.append(request)
        return responder(request)

    return ApiRevGasHttp(URL, CHAVE, transporte=httpx.MockTransport(tratar)), recebidas


def ok(status=200, corpo=None):
    return lambda r: httpx.Response(status, json=corpo if corpo is not None else {})


# --- o que vai para a RevGás ------------------------------------------------------------


def test_buscar_clientes_pelo_telefone():
    lista = {"data": [{"id": 7}], "pagination": {"has_next": False}}
    api, recebidas = com_revgas(ok(200, lista))

    assert api.buscar_clientes("5500900000001") == Resposta(200, lista)
    [r] = recebidas
    assert r.method == "GET"
    assert r.url.path == "/v1/customers"
    assert r.url.params["phone"] == "5500900000001"


def test_a_chave_vai_no_cabecalho_authorization_sem_bearer_e_nunca_na_url():
    api, recebidas = com_revgas(ok(200, {"data": []}))
    api.buscar_clientes("5500900000001")

    [r] = recebidas
    assert r.headers["authorization"] == CHAVE
    assert CHAVE not in str(r.url)


def test_cadastrar_cliente_envia_o_json_e_devolve_o_cliente_criado():
    api, recebidas = com_revgas(ok(201, {"data": {"id": 9, "name": "Fulano"}}))

    assert api.cadastrar_cliente({"name": "Fulano"}) == Resposta(201, {"id": 9, "name": "Fulano"})
    [r] = recebidas
    assert (r.method, r.url.path) == ("POST", "/v1/customers")
    assert json.loads(r.content) == {"name": "Fulano"}


def test_criar_venda_envia_o_corpo_e_devolve_a_venda_criada():
    venda = {"id": 30, "total": 220.0, "status": "OPEN"}
    api, recebidas = com_revgas(ok(201, {"data": venda}))

    assert api.criar_venda({"customer_id": 9}) == Resposta(201, venda)
    [r] = recebidas
    assert (r.method, r.url.path) == ("POST", "/v1/sales")
    assert json.loads(r.content) == {"customer_id": 9}


def test_criacao_sem_envelope_data_tambem_e_lida():
    venda = {"id": 30, "total": 220.0, "status": "OPEN"}
    api, _ = com_revgas(ok(201, venda))

    assert api.criar_venda({}) == Resposta(201, venda)


def test_buscar_vendas_com_os_filtros_da_doc():
    lista = {"data": [], "pagination": {"has_next": False}}
    api, recebidas = com_revgas(ok(200, lista))

    resposta = api.buscar_vendas(customer_id=9, channel="WHATSAPP", de=date(2026, 11, 11), ate=date(2026, 11, 13), pagina=2)

    assert resposta == Resposta(200, lista)
    [r] = recebidas
    assert (r.method, r.url.path) == ("GET", "/v1/sales")
    assert dict(r.url.params) == {
        "customer_id": "9", "channel": "WHATSAPP", "from": "2026-11-11", "to": "2026-11-13", "page": "2",
    }


def test_buscar_vendas_sem_canal_nao_manda_o_filtro():
    api, recebidas = com_revgas(ok(200, {"data": []}))
    api.buscar_vendas(customer_id=9, channel=None, de=date(2026, 11, 11), ate=date(2026, 11, 13), pagina=1)

    assert "channel" not in recebidas[0].url.params


def test_cancelar_venda_sem_motivo():
    # M4(c): dúvida 8, o schema aceita motive_cancel_sale_id nulo.
    api, recebidas = com_revgas(ok(200, {"data": {"id": 30, "status": "CANCEL"}}))

    assert api.cancelar_venda(30) == Resposta(200, {"id": 30, "status": "CANCEL"})
    [r] = recebidas
    assert (r.method, r.url.path) == ("PUT", "/v1/sales/30/cancel")
    assert json.loads(r.content) == {"motive_cancel_sale_id": None}


# --- respostas de erro: devolvidas como estão, quem decide é o RegistradorDeVendas ------


def test_erro_http_vira_resposta_com_o_corpo_da_api_sem_tirar_o_envelope():
    erros = {"data": [{"field": "number", "message": "obrigatório"}]}
    api, _ = com_revgas(ok(422, erros))

    assert api.criar_venda({}) == Resposta(422, erros)


@pytest.mark.parametrize("status", [401, 402, 403, 404, 409, 429, 500, 504])
def test_qualquer_status_vira_resposta_sem_excecao(status):
    api, _ = com_revgas(ok(status, {"message": "erro"}))

    assert api.criar_venda({}).status == status


def test_corpo_que_nao_e_json_vira_dados_vazios():
    api, _ = com_revgas(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>"))

    assert api.criar_venda({}) == Resposta(502, None)


def test_redirecionamento_nao_e_seguido():
    # 3xx -> TRANSFERIR_ATENDENTE no registrador; a chave nunca vai para outro endereço.
    api, recebidas = com_revgas(lambda r: httpx.Response(302, headers={"Location": "https://outro.test/"}))

    assert api.criar_venda({}).status == 302
    assert len(recebidas) == 1


@pytest.mark.parametrize(
    "erro",
    [httpx.ReadTimeout("demorou"), httpx.ConnectTimeout("demorou"), httpx.ConnectError("sem rede"),
     httpx.RemoteProtocolError("caiu")],
)
def test_falha_de_rede_vira_tempo_esgotado(erro):
    # Resultado incerto: o registrador nunca repete o POST, vai para a reconciliação.
    def levanta(request):
        raise erro

    api, _ = com_revgas(levanta)

    with pytest.raises(TempoEsgotado):
        api.criar_venda({})


def test_espera_no_maximo_15_segundos():
    api, recebidas = com_revgas(ok(200, {"data": []}))
    api.buscar_clientes("5500900000001")

    assert recebidas[0].extensions["timeout"]["read"] == 15


def test_nada_vai_para_o_log(caplog):
    api, _ = com_revgas(ok(422, {"data": [{"field": "name", "message": "Fulano inválido"}]}))
    with caplog.at_level("DEBUG"):
        api.cadastrar_cliente({"name": "Fulano", "phones": ["5500900000001"]})
        api.buscar_clientes("5500900000001")

    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert CHAVE not in registros
    assert "5500900000001" not in registros
    assert "Fulano" not in registros


def test_url_e_chave_sao_obrigatorias():
    with pytest.raises(ValueError):
        ApiRevGasHttp("", CHAVE)
    with pytest.raises(ValueError):
        ApiRevGasHttp(URL, "  ")


def test_dinheiro_em_decimal_vai_como_numero_json():
    # O corpo montado pelo chatbot usa Decimal; a API recebe número (110.0), nunca texto.
    from decimal import Decimal

    api, recebidas = com_revgas(ok(201, {"data": {"id": 30, "total": 220.0, "status": "OPEN"}}))
    api.criar_venda({"products": [{"product_id": 1, "quantity": 1, "value": Decimal("110.00")}]})

    [r] = recebidas
    assert r.headers["content-type"] == "application/json"
    assert json.loads(r.content) == {"products": [{"product_id": 1, "quantity": 1, "value": 110.0}]}

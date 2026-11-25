"""Testes das novas funções do chatbot puro (sem LLM):
- Vale-gás (`VALEGAS`) com serial e opção "Outra forma";
- Campo opcional Quadra no endereço (pergunta individual + `complement` na API);
- Módulo único `HistoricoDeVendas` com `page_size=100` e `GET /sales/{id}`;
- Fluxos "Onde está meu pedido?", "Meus pedidos", Recompra (preço atual do catálogo) e FAQ rápido;
- Parser determinístico ampliado (sim/não, pagamentos e frases livres sem IA).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.conversa.entrada import forma_de_pagamento, sim_ou_nao
from revgas_chatbot.devolucao.devolucao import Devolucao
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.catalogo_api import carregar_catalogo_da_api
from revgas_chatbot.vendas.historico import HistoricoDeVendas, traduzir_status
from revgas_chatbot.vendas.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo, validar
from revgas_chatbot.vendas.registro import RegistradorDeVendas
from revgas_chatbot.vendas.venda import montar_corpo_venda

AGORA = datetime(2026, 11, 25, 14, 0, tzinfo=FUSO_TERESINA)
MARIA = "5500900000001"
NOVO = "5500900000002"
CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO", "VALEGAS"}


@pytest.fixture
def api():
    api = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    api.descricoes[26717] = "botijão de gás"
    api.clientes.append(
        {
            "id": 77,
            "name": "Maria Exemplo",
            "phones": [MARIA],
            "location": "Rua Exemplo",
            "number": "100",
            "neighborhood": "Centro",
            "city": "Teresina",
            "state_federation": "Piauí",
        }
    )
    return api


@pytest.fixture
def dependencias(api):
    cadenciada = ApiCadenciada(api, api.relogio)
    historico = HistoricoDeVendas(api=cadenciada, relogio=api.relogio, catalogo=CATALOGO)
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
        seguranca="ORIENTAÇÕES 193",
        devolucao=Devolucao(api=cadenciada, relogio=api.relogio, catalogo=CATALOGO),
        historico=historico,
    )


def falar(conversa: Conversa, *mensagens: str) -> list[str]:
    conversa.iniciar()
    ultima: list[str] = []
    for m in mensagens:
        ultima = conversa.responder(m)
    return ultima


# --- Vale-gás e Outra forma -----------------------------------------------------------------


def test_valegas_pede_serial_sem_troco_e_envia_no_post_sales(dependencias, api):
    conversa = Conversa(MARIA, dependencias)
    resp = falar(conversa, "sim", "1", "sim", "vale gás")

    assert resp == ["Qual é o código do vale-gás? (7 caracteres, ex.: 1234567)"]
    resumo = "\n".join(conversa.responder("1234567"))
    assert "Vale-gás (código 1234567)" in resumo
    assert "troco" not in resumo.lower()

    final = "\n".join(conversa.responder("1"))
    assert final.startswith("Pedido registrado!")
    [venda] = api.vendas_criadas()
    assert venda["payments"] == [{"payment_type": "VALEGAS", "value": Decimal("110.00"), "serial": "1234567"}]


def test_outra_forma_de_pagamento_transfere_para_atendente(dependencias):
    conversa = Conversa(MARIA, dependencias)
    resp = "\n".join(falar(conversa, "sim", "1", "sim", "outra forma"))

    assert "equipe" in resp.lower()
    assert conversa.encerrada


def test_catalogo_carregado_direto_da_api_revgas():
    api = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    api.produtos.append({"id": 26717, "description": "GLP 13KG", "establishment_id": 2580})
    api.acordos.append(
        {
            "id": 1,
            "product_id": 26717,
            "establishment_id": 2580,
            "suggested_value": "115.00",
            "minimum_value": "105.00",
            "is_default": True,
            "type": "PAYMENT_TYPE",
        }
    )
    cadenciada = ApiCadenciada(api, api.relogio)
    catalogo = carregar_catalogo_da_api(cadenciada, 2580)

    assert catalogo == {26717: ProdutoCatalogo(nome="GLP 13KG", preco=Decimal("115.00"))}
    assert api.operacoes == ["buscar_produtos", "buscar_acordos"]


def test_devolucao_anota_solicitacao_na_observation_da_venda_via_put(dependencias, api):
    venda = api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "products": [{"product_id": 26717, "quantity": 1, "value": Decimal("110.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("110.00")}],
            "observation": "PIX: pagar na entrega. [chatbot:abc]",
        },
        criada_em=AGORA - timedelta(hours=1),
    )
    conversa = Conversa(MARIA, dependencias)
    final = "\n".join(falar(conversa, "quero cancelar", "1", "1", "não", "1"))

    assert f"ERP-{venda['id']}" in final
    guardada = next(v for v in api.vendas if v["id"] == venda["id"])
    assert "PIX: pagar na entrega. [chatbot:abc]" in guardada["observation"]
    assert "[CHATBOT][CANCELAMENTO]" in guardada["observation"]


def test_validacao_rejeita_valegas_sem_serial():
    pedido = Pedido(
        pedido_id="p1",
        telefone=MARIA,
        nome="Maria",
        itens=(Item(26717, 1),),
        endereco=Endereco("Rua A", "10", "Centro", "Teresina", "PI"),
        pagamentos=(Pagamento("VALEGAS", Decimal("110.00")),),
    )
    resultado = validar(pedido, CATALOGO, PAGAMENTOS)
    assert "serial" in [p.campo for p in resultado.problemas]


# --- Quadra no endereço ---------------------------------------------------------------------


def test_quadra_perguntada_individualmente_e_enviada_no_complemento(dependencias, api):
    conversa = Conversa(NOVO, dependencias)
    conversa.iniciar()
    conversa.responder("sim")
    conversa.responder("Bruno")
    conversa.responder("1")
    conversa.responder("Rua Ininga")
    resp_quadra = conversa.responder("201")

    assert resp_quadra == ["Qual é a quadra?\n0) Não tem / Pular etapa"]
    assert conversa.responder("12") == ["Qual é o bairro?"]
    assert conversa.responder("Ininga") == ["Qual é a cidade?"]
    assert conversa.responder("Teresina") == ["Complemento?\n0) Não tem / Pular etapa"]
    assert conversa.responder("Casa B") == ["Ponto de referência?\n0) Não tem / Pular etapa"]
    resp_pag = "\n".join(conversa.responder("pular"))
    assert "5) Vale-gás" in resp_pag and "6) Outra forma" in resp_pag

    assert conversa.responder("1") == ["O pagamento no PIX será:\n1) Pagar agora\n2) Pagar na entrega"]
    resumo = "\n".join(conversa.responder("1"))
    assert "Quadra: 12" in resumo
    assert "Complemento: Casa B" in resumo
    assert "Pagamento: PIX (pagar agora)" in resumo
    assert "Responda: 1) Confirmar  2) Corrigir  3) Cancelar" in resumo

    final = "\n".join(conversa.responder("1"))
    assert "comprovante" in final.lower()
    [venda] = api.vendas_criadas()
    assert venda["complement"] == "Quadra 12, Casa B"
    assert "PIX: pagar agora." in venda["observation"]


def test_quadra_pode_ser_pulada_com_nao_tem(dependencias, api):
    conversa = Conversa(NOVO, dependencias)
    resumo = "\n".join(
        falar(
            conversa, "sim", "Bruno", "1", "Rua Nova", "Não tem / Pular etapa",
            "0", "Centro", "Teresina", "Não tem / Pular etapa", "0", "pix", "pagar na entrega",
        )
    )
    assert "Rua Nova, S/N" in resumo
    assert "Quadra:" not in resumo
    assert "Pagamento: PIX (pagar na entrega)" in resumo
    conversa.responder("1")
    [venda] = api.vendas_criadas()
    assert "complement" not in venda
    assert "PIX: pagar na entrega." in venda["observation"]


# --- API HTTP: page_size=100 e GET /sales/{id} ----------------------------------------------


def test_api_http_envia_page_size_100_e_busca_venda_por_id():
    from revgas_chatbot.api_revgas.api_http import MonitorApi

    chamadas: list[httpx.Request] = []
    monitor = MonitorApi()

    def handler(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        if req.url.path == "/v1/sales":
            return httpx.Response(200, json={"data": [], "pagination": {"has_next": False}})
        if req.url.path == "/v1/sales/42":
            return httpx.Response(200, json={"data": {"id": 42, "status": "WARNING"}})
        return httpx.Response(404)

    http = ApiRevGasHttp(
        "https://api.exemplo.test/v1", "chave", transporte=httpx.MockTransport(handler), monitor=monitor
    )
    r1 = http.buscar_vendas(customer_id=77, channel=None, de=date(2026, 11, 1), ate=date(2026, 11, 25), pagina=1)
    r2 = http.buscar_venda(42)

    assert r1.status == 200
    assert chamadas[0].url.params["page_size"] == "100"
    assert r2.status == 200 and r2.dados["id"] == 42 and r2.dados["status"] == "WARNING"
    logs = monitor.listar()
    assert len(logs) == 2
    assert logs[0]["metodo"] == "GET" and logs[0]["status"] == 200
    assert "chave" not in str(logs)


# --- Onde está meu pedido? e Meus pedidos ---------------------------------------------------


def test_onde_esta_meu_pedido_mostra_status_entregador_e_logs(dependencias, api):
    venda = api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "status": "WARNING",
            "deliverer_name": "Carlos Motoboy",
            "products": [{"product_id": 26717, "quantity": 1, "value": Decimal("110.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("110.00")}],
            "location": "Rua Exemplo",
            "number": "100",
            "neighborhood": "Centro",
            "city": "Teresina",
            "state_federation": "Piauí",
            "logs": [
                {"id": 1, "sale_id": 1001, "status": "OPEN", "created_at": (AGORA - timedelta(minutes=20)).isoformat()},
                {"id": 2, "sale_id": 1001, "status": "WARNING", "created_at": (AGORA - timedelta(minutes=5)).isoformat()},
            ],
        },
        criada_em=AGORA - timedelta(minutes=20),
    )

    conversa = Conversa(MARIA, dependencias)
    resp = "\n".join(falar(conversa, "cadê meu gás?"))

    assert f"Pedido {venda['id']}" in resp
    assert traduzir_status("WARNING") in resp
    assert "Carlos Motoboy" in resp
    assert "Histórico:" in resp
    assert api.ultimo_page_size == 100


def test_meus_pedidos_lista_vendas_com_status(dependencias, api):
    api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "status": "CONFIRM",
            "products": [{"product_id": 26717, "quantity": 2, "value": Decimal("110.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("220.00")}],
            "location": "Rua Exemplo",
            "number": "100",
            "neighborhood": "Centro",
            "city": "Teresina",
            "state_federation": "Piauí",
        },
        criada_em=AGORA - timedelta(days=2),
    )
    conversa = Conversa(MARIA, dependencias)
    resp = "\n".join(falar(conversa, "meus pedidos"))

    assert "Seus últimos pedidos:" in resp
    assert "entregue" in resp


# --- Recompra (preço atual do catálogo!) ----------------------------------------------------


def test_recompra_oferece_repetir_com_preco_atual_do_catalogo(dependencias, api):
    # Venda antiga custava R$ 95,00; catálogo atual é R$ 110,00.
    api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": 77,
            "channel": "WHATSAPP",
            "status": "CONFIRM",
            "products": [{"product_id": 26717, "quantity": 2, "value": Decimal("95.00")}],
            "payments": [{"payment_type": "PIX", "value": Decimal("190.00")}],
            "location": "Rua Exemplo",
            "number": "100",
            "neighborhood": "Centro",
            "city": "Teresina",
            "state_federation": "Piauí",
        },
        criada_em=AGORA - timedelta(days=30),
    )

    conversa = Conversa(MARIA, dependencias)
    oferta = "\n".join(falar(conversa, "sim"))
    assert "Quer repetir seu último pedido?" in oferta
    assert "R$ 220,00" in oferta  # preço atual (2 x 110), nunca o antigo (190)

    assert conversa.responder("sim") == ["O pagamento no PIX será:\n1) Pagar agora\n2) Pagar na entrega"]
    resumo = "\n".join(conversa.responder("2"))
    assert "Confira seu pedido:" in resumo
    assert "R$ 220,00" in resumo

    final = "\n".join(conversa.responder("1"))
    assert final.startswith("Pedido registrado!")
    assert api.vendas_criadas()[-1]["total"] == Decimal("220.00")


# --- FAQ rápido e parser determinístico ampliado --------------------------------------------


def test_faq_responde_preco_sem_perder_passo_atual(dependencias):
    conversa = Conversa(MARIA, dependencias)
    conversa.iniciar()
    conversa.responder("sim")
    resp = "\n".join(conversa.responder("quanto custa o gás?"))

    assert "R$ 110,00" in resp
    assert "Vale-gás" in resp
    assert "Quantos?" in resp  # retomou o passo atual


def test_parser_deterministico_ampliado():
    assert sim_ou_nao("beleza") is True
    assert sim_ou_nao("fechou") is True
    assert sim_ou_nao("negativo") is False
    assert forma_de_pagamento("vale gás") == "VALEGAS"
    assert forma_de_pagamento("espécie") == "MONEY"
    assert forma_de_pagamento("outra forma") == "OUTRA"

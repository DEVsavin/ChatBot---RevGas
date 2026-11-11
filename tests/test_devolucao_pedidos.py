"""Testes do D4: Devolucao.pedidos_do_cliente(telefone), só leitura.

Seam combinado: pedidos_do_cliente, com a API falsa dentro de uma
ApiCadenciada. Dados fictícios.
"""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal

import pytest

from revgas_chatbot.api import Resposta
from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_falsa import TIMEOUT_SEM_CRIAR, ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.atendimento import TransferirAtendente
from revgas_chatbot.devolucao import Devolucao, NenhumPedido, PedidosEncontrados
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.registro import RegistradorDeVendas

TELEFONE = "5586999990000"
AGORA = datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)


def nova_api() -> ApiRevGasFalsa:
    api = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    api.clientes.append({"id": 77, "name": "Cliente Teste", "phones": [TELEFONE]})
    return api


def semear(api, *, dias_atras=0, canal="WHATSAPP", status=None, quantidade=1, cliente=77):
    venda = api.semear_venda(
        {
            "establishment_id": 2580,
            "customer_id": cliente,
            "channel": canal,
            "products": [
                {"product_id": 26717, "quantity": quantidade, "value": 110.0,
                 "product_description": "GLP 13KG"}
            ],
            "payments": [{"payment_type": "PIX", "value": 110.0 * quantidade}],
        },
        criada_em=AGORA - timedelta(days=dias_atras),
    )
    if status:
        for guardada in api.vendas:
            if guardada["id"] == venda["id"]:
                guardada["status"] = status
    return venda


def devolucao(api, prazo_dias=7) -> Devolucao:
    fila = FilaDeAtendimento(Path(tempfile.mkdtemp()), api.relogio)
    return Devolucao(
        api=ApiCadenciada(api, api.relogio), relogio=api.relogio, fila=fila, prazo_dias=prazo_dias
    )


def test_cliente_com_uma_venda_no_prazo_recebe_o_pedido():
    api = nova_api()
    venda = semear(api, dias_atras=1, quantidade=2)

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    assert isinstance(resultado, PedidosEncontrados)
    assert resultado.customer_id == 77
    assert resultado.nome == "Cliente Teste"
    [pedido] = resultado.pedidos
    assert pedido.id == venda["id"]
    assert pedido.itens == (("GLP 13KG", 2),)
    assert pedido.total == Decimal("220.00")
    assert pedido.data == (AGORA - timedelta(days=1)).date()


def test_mostra_so_os_3_mais_recentes():
    api = nova_api()
    ids = [semear(api, dias_atras=d)["id"] for d in (5, 4, 3, 2, 1)]  # do mais antigo ao mais novo

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    assert [p.id for p in resultado.pedidos] == [ids[4], ids[3], ids[2]]


def test_venda_fora_do_prazo_nao_aparece():
    api = nova_api()
    semear(api, dias_atras=10)
    dentro = semear(api, dias_atras=6)

    resultado = devolucao(api, prazo_dias=7).pedidos_do_cliente(TELEFONE)

    assert [p.id for p in resultado.pedidos] == [dentro["id"]]


def test_venda_cancelada_dentro_do_prazo_aparece():
    api = nova_api()
    cancelada = semear(api, dias_atras=1, status="CANCEL")

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    assert [p.id for p in resultado.pedidos] == [cancelada["id"]]
    assert resultado.pedidos[0].cancelado is True


def test_vendas_de_todos_os_canais_aparecem():
    api = nova_api()
    por_telefone = semear(api, dias_atras=2, canal="DISK")
    por_whatsapp = semear(api, dias_atras=1, canal="WHATSAPP")

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    assert {p.id for p in resultado.pedidos} == {por_telefone["id"], por_whatsapp["id"]}


def test_texto_para_o_cliente_nao_traz_status():
    api = nova_api()
    venda = semear(api, dias_atras=1, quantidade=2, status="WARNING")

    [pedido] = devolucao(api).pedidos_do_cliente(TELEFONE).pedidos

    texto = pedido.texto_para_cliente()
    assert texto == f"Pedido {venda['id']} de 10/11/2026: 2x GLP 13KG, total R$ 220,00"
    assert "WARNING" not in texto


def test_texto_para_o_cliente_marca_pedido_cancelado():
    api = nova_api()
    semear(api, dias_atras=1, status="CANCEL")

    [pedido] = devolucao(api).pedidos_do_cliente(TELEFONE).pedidos

    texto = pedido.texto_para_cliente()
    assert texto.endswith("(cancelado)")
    assert "CANCEL" not in texto  # o rótulo é em português; o status da API não aparece


def test_texto_para_o_atendente_traz_o_status():
    api = nova_api()
    semear(api, dias_atras=1, status="WARNING")

    [pedido] = devolucao(api).pedidos_do_cliente(TELEFONE).pedidos

    assert "status na API: WARNING" in pedido.texto_para_atendente()


# --- nada encontrado ou falha: nenhum pedido ou atendente ------------------------


def test_cliente_nao_cadastrado_nao_tem_pedidos():
    api = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))

    assert devolucao(api).pedidos_do_cliente(TELEFONE) == NenhumPedido()


def test_cliente_sem_venda_no_prazo_nao_tem_pedidos():
    api = nova_api()
    semear(api, dias_atras=30)

    assert devolucao(api).pedidos_do_cliente(TELEFONE) == NenhumPedido()


def test_mais_de_um_cliente_com_o_telefone_transfere():
    api = nova_api()
    api.clientes.append({"id": 78, "name": "Outra Pessoa", "phones": [TELEFONE]})

    assert isinstance(devolucao(api).pedidos_do_cliente(TELEFONE), TransferirAtendente)


@pytest.mark.parametrize("operacao", ["buscar_clientes", "buscar_vendas"])
@pytest.mark.parametrize(
    "falha",
    [Resposta(500, {"message": "erro"}), Resposta(403, {"message": "x"}), TIMEOUT_SEM_CRIAR],
)
def test_erro_ou_timeout_na_consulta_transfere(operacao, falha):
    api = nova_api()
    semear(api, dias_atras=1)
    api.roteirizar(operacao, falha)

    assert isinstance(devolucao(api).pedidos_do_cliente(TELEFONE), TransferirAtendente)


@pytest.mark.parametrize(
    "operacao, dados",
    [
        ("buscar_clientes", None),
        ("buscar_clientes", {}),
        ("buscar_clientes", {"data": None}),
        ("buscar_clientes", {"data": [{"sem_id": 1}]}),
        ("buscar_vendas", None),
        ("buscar_vendas", {}),
        ("buscar_vendas", {"data": None}),
        ("buscar_vendas", {"data": ["não é objeto"]}),
    ],
)
def test_resposta_malformada_transfere_sem_quebrar(operacao, dados):
    api = nova_api()
    semear(api, dias_atras=1)
    api.roteirizar(operacao, Resposta(200, dados))

    assert isinstance(devolucao(api).pedidos_do_cliente(TELEFONE), TransferirAtendente)


# --- só leitura e ritmo compartilhado --------------------------------------------


def _encontra(api):
    semear(api, dias_atras=1)


def _nao_cadastrado(api):
    api.clientes.clear()


def _ambiguo(api):
    api.clientes.append({"id": 78, "name": "Outra", "phones": [TELEFONE]})


def _fora_do_prazo(api):
    semear(api, dias_atras=30)


def _erro(api):
    semear(api, dias_atras=1)
    api.roteirizar("buscar_vendas", Resposta(500, {"message": "x"}))


def _malformada(api):
    semear(api, dias_atras=1)
    api.roteirizar("buscar_vendas", Resposta(200, None))


@pytest.mark.parametrize(
    "cenario", [_encontra, _nao_cadastrado, _ambiguo, _fora_do_prazo, _erro, _malformada]
)
def test_nenhum_caminho_da_busca_grava_na_api(cenario):
    api = nova_api()
    cenario(api)
    devolucao(api).pedidos_do_cliente(TELEFONE)

    assert api.operacoes
    assert set(api.operacoes) <= {"buscar_clientes", "buscar_vendas"}


def test_venda_e_devolucao_na_mesma_api_cadenciada_respeitam_1_segundo():
    api = nova_api()
    semear(api, dias_atras=1)
    cadenciada = ApiCadenciada(api, api.relogio)
    registrador = RegistradorDeVendas(
        api=cadenciada,
        relogio=api.relogio,
        catalogo={26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))},
        pagamentos_permitidos={"PIX"},
        establishment_id=2580,
    )
    busca = Devolucao(
        api=cadenciada, relogio=api.relogio, fila=FilaDeAtendimento(Path(tempfile.mkdtemp()), api.relogio)
    )

    busca.pedidos_do_cliente(TELEFONE)
    registrador.registrar(
        Pedido(
            pedido_id="ped-1",
            telefone=TELEFONE,
            nome="Cliente Teste",
            itens=(Item(product_id=26717, quantidade=1),),
            endereco=Endereco("Rua Exemplo", "100", "Centro", "Teresina", "PI"),
            pagamentos=(Pagamento(tipo="PIX", valor=Decimal("110.00")),),
        )
    )
    busca.pedidos_do_cliente(TELEFONE)

    instantes = api.requisicoes
    assert len(instantes) == 6  # 2 da busca + 2 do registro + 2 da busca
    assert all((b - a).total_seconds() >= 1 for a, b in zip(instantes, instantes[1:]))


# --- não confiar nos filtros nem na ordem da API (T1) -----------------------------


def test_api_que_ignora_filtros_e_embaralha_nao_mostra_pedido_de_outro_cliente():
    api = nova_api()
    api.ignora_filtros_de_vendas = True
    api.tamanho_da_pagina = 2  # força várias páginas
    meus = [semear(api, dias_atras=d)["id"] for d in (6, 3, 1, 2)]
    semear(api, dias_atras=0, cliente=99)  # outro cliente, mais recente que todos
    semear(api, dias_atras=300)  # meu, mas fora do prazo (janeiro)

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    # Só os meus, no prazo, do mais novo ao mais antigo, no máximo 3.
    assert [p.id for p in resultado.pedidos] == [meus[2], meus[3], meus[1]]


# --- venda fora do formato (T2, T3) e proteções -----------------------------------


def _estragar(api, venda_id, **mudancas):
    for guardada in api.vendas:
        if guardada["id"] == venda_id:
            guardada.update(mudancas)


@pytest.mark.parametrize(
    "mudancas",
    [
        {"created_at": "2026-11-10T15:00:00"},  # sem fuso
        {"id": True},
        {"id": "1001"},
        {"products": []},
        {"products": [{"product_id": 26717, "quantity": 0, "value": 110.0}]},
        {"products": [{"product_id": 26717, "quantity": True, "value": 110.0}]},
        {"status": None},
        {"total": "abc"},
        {"total": "NaN"},
    ],
)
def test_venda_fora_do_formato_transfere(mudancas):
    api = nova_api()
    venda = semear(api, dias_atras=1)
    _estragar(api, venda["id"], **mudancas)

    assert isinstance(devolucao(api).pedidos_do_cliente(TELEFONE), TransferirAtendente)


def test_pedidos_demais_transfere_em_vez_de_ler_sem_fim():
    api = nova_api()
    api.tamanho_da_pagina = 1
    for _ in range(25):
        semear(api, dias_atras=1)

    assert isinstance(devolucao(api).pedidos_do_cliente(TELEFONE), TransferirAtendente)


def test_devolucao_exige_a_api_cadenciada_compartilhada():
    api = nova_api()
    fila = FilaDeAtendimento(Path(tempfile.mkdtemp()), api.relogio)
    with pytest.raises(TypeError, match="ApiCadenciada"):
        Devolucao(api=api, relogio=api.relogio, fila=fila)


def test_venda_estranha_de_outra_pessoa_ou_de_balcao_e_ignorada():
    api = nova_api()
    api.ignora_filtros_de_vendas = True
    minha = semear(api, dias_atras=1)
    balcao = semear(api, dias_atras=1, cliente=None)
    _estragar(api, balcao["id"], products=[], status=None)  # sem cliente e malformada
    outra = semear(api, dias_atras=1, cliente=99)
    _estragar(api, outra["id"], created_at="sem data")

    resultado = devolucao(api).pedidos_do_cliente(TELEFONE)

    assert [p.id for p in resultado.pedidos] == [minha["id"]]


def test_item_de_venda_sem_cliente_e_ignorado_e_nao_vaza():
    # Sem customer_id não dá para dizer que a venda é do cliente: não é mostrada.
    api = nova_api()
    api.roteirizar("buscar_vendas", Resposta(200, {"data": [{"sem_id": 1}], "pagination": {}}))

    assert devolucao(api).pedidos_do_cliente(TELEFONE) == NenhumPedido()

"""Testes do M3: RegistradorDeVendas.registrar(pedido) com a API falsa.

Seam único combinado: todos os testes passam por `registrar`. O que a API
"viu" é conferido pelo estado da API falsa (vendas, clientes, requisições).
Dados fictícios.
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api import Resposta
from revgas_chatbot.api_revgas.api_falsa import (
    FUSO_TERESINA,
    TIMEOUT_DEPOIS_DE_CRIAR,
    TIMEOUT_SEM_CRIAR,
    ApiRevGasFalsa,
    DepoisDeCriar,
    RelogioFalso,
)
from revgas_chatbot.vendas.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.vendas.venda import montar_corpo_venda
from revgas_chatbot.vendas.registro import (
    EmAndamento,
    PedidoInvalido,
    RegistradorDeVendas,
    TransferirAtendente,
    VendaCriada,
)

TELEFONE = "5586999990000"
CATALOGO = {1: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
PERMITIDOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"}


def pedido(**mudancas) -> Pedido:
    base = Pedido(
        pedido_id="ped-1",
        telefone=TELEFONE,
        nome="Cliente Teste",
        itens=(Item(product_id=1, quantidade=1),),
        endereco=Endereco(
            logradouro="Rua Exemplo", numero="100", bairro="Centro", cidade="Teresina", estado="PI"
        ),
        pagamentos=(Pagamento(tipo="PIX", valor=Decimal("110.00")),),
    )
    return replace(base, **mudancas)


def montar(api: ApiRevGasFalsa) -> RegistradorDeVendas:
    return RegistradorDeVendas(
        api=api,
        relogio=api.relogio,
        catalogo=CATALOGO,
        pagamentos_permitidos=PERMITIDOS,
        establishment_id=2580,
    )


def api_com_cliente(relogio: RelogioFalso | None = None) -> ApiRevGasFalsa:
    api = ApiRevGasFalsa(relogio=relogio or RelogioFalso())
    api.clientes.append({"id": 77, "name": "Cliente Teste", "phones": [TELEFONE]})
    return api


def test_cliente_cadastrado_gera_venda_com_o_id_da_api():
    api = api_com_cliente()
    resultado = montar(api).registrar(pedido())

    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 77
    assert resultado == VendaCriada(id=venda["id"], total=Decimal("110.00"), status="ACTIVE")


def test_pedido_invalido_nao_faz_nenhuma_requisicao():
    api = api_com_cliente()
    resultado = montar(api).registrar(pedido(endereco=None))

    assert isinstance(resultado, PedidoInvalido)
    assert "endereco" in [p.campo for p in resultado.problemas]
    assert api.requisicoes == []


def test_produto_fora_do_catalogo_transfere_sem_nenhuma_requisicao():
    api = api_com_cliente()
    resultado = montar(api).registrar(pedido(itens=(Item(product_id=999, quantidade=1),)))

    assert isinstance(resultado, TransferirAtendente)
    assert resultado.pedido_id == "ped-1"
    assert api.requisicoes == []


def test_cliente_novo_e_cadastrado_e_a_venda_usa_o_id_novo():
    api = ApiRevGasFalsa()  # nenhum cliente cadastrado
    resultado = montar(api).registrar(pedido())

    [cliente] = api.clientes
    assert cliente["phones"] == [TELEFONE]
    assert cliente["name"] == "Cliente Teste"
    assert cliente["type"] == "PHYSICAL"
    assert cliente["state_federation"] == "Piauí"
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == cliente["id"]
    assert isinstance(resultado, VendaCriada)


def test_telefone_com_mais_de_um_cliente_transfere_sem_venda():
    api = api_com_cliente()
    api.clientes.append({"id": 78, "name": "Outra Pessoa", "phones": [TELEFONE]})
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.vendas_criadas() == []


def test_confirmacao_repetida_depois_de_concluir_nao_cria_outra_venda():
    api = api_com_cliente()
    registrador = montar(api)
    primeiro = registrador.registrar(pedido())
    segundo = registrador.registrar(pedido())

    assert len(api.vendas_criadas()) == 1
    assert segundo == primeiro


def test_confirmacao_repetida_durante_o_processamento_nao_duplica_cliente_nem_venda():
    api = ApiRevGasFalsa()  # cliente novo: o risco é duplicar o cadastro também
    registrador = montar(api)
    respostas_da_segunda = []
    api.roteirizar("buscar_clientes", lambda: respostas_da_segunda.append(registrador.registrar(pedido())))

    resultado = registrador.registrar(pedido())

    assert respostas_da_segunda == [EmAndamento(pedido_id="ped-1")]
    assert len(api.clientes) == 1
    assert len(api.vendas_criadas()) == 1
    assert isinstance(resultado, VendaCriada)


def test_depois_de_pedido_invalido_o_pedido_corrigido_pode_ser_confirmado():
    api = api_com_cliente()
    registrador = montar(api)
    assert isinstance(registrador.registrar(pedido(endereco=None)), PedidoInvalido)

    assert isinstance(registrador.registrar(pedido()), VendaCriada)
    assert len(api.vendas_criadas()) == 1


# --- erros da API ---------------------------------------------------------------


def erro_422(campo: str) -> Resposta:
    return Resposta(422, {"data": [{"field": campo, "message": "texto cru da API"}], "message": "Request error"})


def test_422_em_dado_do_cliente_pede_correcao_sem_repassar_texto_da_api():
    api = api_com_cliente()
    api.roteirizar("criar_venda", erro_422("location"))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, PedidoInvalido)
    assert [p.campo for p in resultado.problemas] == ["endereco.logradouro"]
    assert "texto cru da API" not in resultado.problemas[0].mensagem


def test_422_no_cadastro_do_cliente_em_dado_do_cliente_pede_correcao():
    api = ApiRevGasFalsa()
    api.roteirizar("cadastrar_cliente", erro_422("phones"))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, PedidoInvalido)
    assert [p.campo for p in resultado.problemas] == ["telefone"]


@pytest.mark.parametrize("campo", ["product_id", "establishment_id", "payment_type", "value", "campo_desconhecido"])
def test_422_em_campo_de_configuracao_transfere(campo):
    api = api_com_cliente()
    api.roteirizar("criar_venda", erro_422(campo))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert resultado.venda_pode_existir is False


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 302])
def test_erro_4xx_ou_3xx_na_venda_transfere_sem_aviso_de_venda(status):
    api = api_com_cliente()
    api.roteirizar("criar_venda", Resposta(status, {"message": "texto cru da API"}))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert resultado.pedido_id == "ped-1"
    assert resultado.venda_pode_existir is False
    assert api.vendas_criadas() == []


@pytest.mark.parametrize("operacao", ["buscar_clientes", "cadastrar_cliente"])
def test_erro_da_api_na_etapa_do_cliente_transfere_sem_venda(operacao):
    api = ApiRevGasFalsa()
    api.roteirizar(operacao, Resposta(500, {"message": "internal server error"}))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.vendas_criadas() == []


# --- novas tentativas: 409 e 429 ------------------------------------------------

TRAVADO = Resposta(409, {"message": "cannot modify object due to lock, try again"})
LIMITE = Resposta(429, {"message": "too many requests"})


def test_409_tenta_de_novo_uma_vez_e_cria_a_venda():
    api = api_com_cliente()
    api.roteirizar("criar_venda", TRAVADO)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, VendaCriada)
    assert len(api.vendas_criadas()) == 1


def test_409_duas_vezes_transfere_sem_venda():
    api = api_com_cliente()
    api.roteirizar("criar_venda", TRAVADO, TRAVADO)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.vendas_criadas() == []


def test_429_espera_pelo_menos_1_segundo_e_tenta_de_novo():
    api = api_com_cliente()
    api.roteirizar("criar_venda", LIMITE)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, VendaCriada)
    recusada, aceita = api.requisicoes[-2:]
    assert (aceita - recusada).total_seconds() >= 1


def test_429_tres_vezes_seguidas_transfere():
    api = api_com_cliente()
    api.roteirizar("criar_venda", LIMITE, LIMITE, LIMITE)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.vendas_criadas() == []


def test_409_na_busca_do_cliente_tambem_tenta_de_novo():
    api = api_com_cliente()
    api.roteirizar("buscar_clientes", TRAVADO)
    assert isinstance(montar(api).registrar(pedido()), VendaCriada)


# --- limite de 1 requisição por segundo ----------------------------------------


def intervalos(api: ApiRevGasFalsa) -> list[float]:
    t = api.requisicoes
    return [(b - a).total_seconds() for a, b in zip(t, t[1:])]


def test_nunca_faz_mais_de_uma_requisicao_por_segundo():
    api = ApiRevGasFalsa()  # cliente novo: buscar + cadastrar + criar = 3 requisições
    montar(api).registrar(pedido())

    assert len(api.requisicoes) == 3
    assert all(i >= 1 for i in intervalos(api))


def test_limite_vale_entre_pedidos_diferentes():
    api = api_com_cliente()
    registrador = montar(api)
    registrador.registrar(pedido(pedido_id="ped-1"))
    registrador.registrar(pedido(pedido_id="ped-2"))

    assert all(i >= 1 for i in intervalos(api))


def test_nao_espera_a_toa_se_ja_passou_1_segundo():
    api = api_com_cliente()
    registrador = montar(api)
    registrador.registrar(pedido(pedido_id="ped-1"))
    api.relogio.avancar(60)
    esperas_antes = len(api.relogio.esperas)
    registrador.registrar(pedido(pedido_id="ped-2"))

    # Só a espera entre as 2 requisições do ped-2, nenhuma antes da primeira.
    assert len(api.relogio.esperas) - esperas_antes == 1


# --- timeout no POST /sales: reconciliação --------------------------------------


def test_timeout_com_venda_criada_acha_a_venda_pela_marca_sem_reenviar():
    api = api_com_cliente()
    api.roteirizar("criar_venda", TIMEOUT_DEPOIS_DE_CRIAR)
    resultado = montar(api).registrar(pedido())

    [venda] = api.vendas_criadas()  # nenhum POST extra
    assert resultado == VendaCriada(id=venda["id"], total=Decimal("110.00"), status="ACTIVE")


def test_timeout_sem_venda_tenta_achar_3_vezes_e_transfere_com_aviso():
    api = api_com_cliente()
    api.roteirizar("criar_venda", TIMEOUT_SEM_CRIAR)
    resultado = montar(api).registrar(pedido())

    assert resultado == TransferirAtendente(
        motivo=resultado.motivo, pedido_id="ped-1", venda_pode_existir=True
    )
    assert "[chatbot:ped-1]" in resultado.motivo
    assert api.vendas_criadas() == []
    # 1 busca de cliente + 1 POST que se perdeu + 3 buscas de venda
    assert len(api.requisicoes) == 5


def test_venda_que_aparece_com_atraso_e_achada_numa_nova_tentativa():
    api = api_com_cliente()
    api.atraso_de_visibilidade = 2  # só aparece na 3ª busca
    api.roteirizar("criar_venda", TIMEOUT_DEPOIS_DE_CRIAR)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, VendaCriada)
    assert len(api.vendas_criadas()) == 1


def test_venda_criada_as_23h59_e_achada_depois_da_meia_noite():
    api = api_com_cliente(RelogioFalso(datetime(2026, 11, 10, 23, 59, 58, tzinfo=FUSO_TERESINA)))
    api.roteirizar("criar_venda", TIMEOUT_DEPOIS_DE_CRIAR)
    resultado = montar(api).registrar(pedido())

    assert api.relogio.agora().day == 11  # a busca rodou no dia seguinte
    assert isinstance(resultado, VendaCriada)


def test_outra_venda_do_mesmo_cliente_nao_e_confundida_com_a_nossa():
    api = api_com_cliente()
    outro = montar(api)
    outro.registrar(pedido(pedido_id="ped-antigo"))  # venda legítima de outro pedido
    api.roteirizar("criar_venda", TIMEOUT_SEM_CRIAR)
    resultado = outro.registrar(pedido(pedido_id="ped-1"))

    assert isinstance(resultado, TransferirAtendente)
    assert resultado.venda_pode_existir is True


def test_venda_na_segunda_pagina_da_busca_e_achada():
    api = api_com_cliente()
    api.tamanho_da_pagina = 2
    registrador = montar(api)
    api.roteirizar("criar_venda", TIMEOUT_DEPOIS_DE_CRIAR)
    # Antes da busca, chegam 3 vendas mais novas do mesmo cliente (outros pedidos).
    api.roteirizar(
        "buscar_vendas",
        lambda: [
            api.semear_venda(montar_corpo_venda(pedido(pedido_id=f"novo-{n}"), customer_id=77,
                                                catalogo=CATALOGO, establishment_id=2580))
            for n in range(3)
        ],
    )
    resultado = registrador.registrar(pedido())

    assert isinstance(resultado, VendaCriada)
    assert resultado.id == api.vendas_criadas()[0]["id"]


# --- timeout no cliente ---------------------------------------------------------


def test_timeout_no_cadastro_com_cliente_criado_segue_sem_cadastrar_de_novo():
    api = ApiRevGasFalsa()
    api.roteirizar("cadastrar_cliente", TIMEOUT_DEPOIS_DE_CRIAR)
    resultado = montar(api).registrar(pedido())

    [cliente] = api.clientes  # nenhum segundo cadastro
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == cliente["id"]
    assert isinstance(resultado, VendaCriada)


def test_timeout_no_cadastro_sem_cliente_criado_transfere_sem_cadastrar_de_novo():
    api = ApiRevGasFalsa()
    api.roteirizar("cadastrar_cliente", TIMEOUT_SEM_CRIAR)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.clientes == []
    assert api.vendas_criadas() == []


def test_timeout_na_busca_do_cliente_transfere_sem_venda():
    api = api_com_cliente()
    api.roteirizar("buscar_clientes", TIMEOUT_SEM_CRIAR)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert api.vendas_criadas() == []


# --- depois do envio, a trava nunca se solta ------------------------------------


@pytest.mark.parametrize(
    "resposta_estranha",
    [
        Resposta(201, {"id": 5}),  # sem total/status
        Resposta(201, None),  # sem corpo
    ],
)
def test_resposta_malformada_depois_do_envio_transfere_e_mantem_a_trava(resposta_estranha):
    api = api_com_cliente()
    api.roteirizar("criar_venda", resposta_estranha)
    registrador = montar(api)

    primeiro = registrador.registrar(pedido())
    requisicoes_depois_do_primeiro = len(api.requisicoes)
    segundo = registrador.registrar(pedido())

    assert isinstance(primeiro, TransferirAtendente)
    assert primeiro.venda_pode_existir is True
    assert segundo == primeiro
    assert len(api.requisicoes) == requisicoes_depois_do_primeiro  # nenhum POST novo


def test_resposta_estranha_na_reconciliacao_transfere_e_mantem_a_trava():
    api = api_com_cliente()
    api.roteirizar("criar_venda", TIMEOUT_SEM_CRIAR)
    api.roteirizar("buscar_vendas", Resposta(200, None))
    registrador = montar(api)

    primeiro = registrador.registrar(pedido())
    segundo = registrador.registrar(pedido())

    assert isinstance(primeiro, TransferirAtendente)
    assert primeiro.venda_pode_existir is True
    assert segundo == primeiro


# --- 5xx no POST /sales: resultado incerto --------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_na_venda_sem_venda_criada_procura_e_transfere_com_aviso(status):
    api = api_com_cliente()
    api.roteirizar("criar_venda", Resposta(status, {"message": "erro"}))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert resultado.venda_pode_existir is True
    assert api.vendas_criadas() == []


def test_5xx_de_proxy_com_venda_criada_acha_a_venda():
    api = api_com_cliente()
    api.roteirizar("criar_venda", DepoisDeCriar(Resposta(504, {"message": "gateway timeout"})))
    resultado = montar(api).registrar(pedido())

    [venda] = api.vendas_criadas()
    assert resultado == VendaCriada(id=venda["id"], total=Decimal("110.00"), status="ACTIVE")


def test_roteiro_com_operacao_digitada_errado_e_recusado():
    with pytest.raises(ValueError):
        ApiRevGasFalsa().roteirizar("criar_vendas", Resposta(500))


def test_409_de_dado_duplicado_nao_e_repetido():
    api = api_com_cliente()
    duplicado = Resposta(409, {"message": "An object with requested data already exists", "data": []})
    api.roteirizar("criar_venda", duplicado)
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, TransferirAtendente)
    assert len(api.requisicoes) == 2  # 1 busca de cliente + 1 POST, sem nova tentativa


def test_400_em_dado_do_cliente_pede_correcao():
    api = api_com_cliente()
    api.roteirizar("criar_venda", Resposta(400, {"data": [{"field": "number", "message": "x"}], "message": "x"}))
    resultado = montar(api).registrar(pedido())

    assert isinstance(resultado, PedidoInvalido)
    assert [p.campo for p in resultado.problemas] == ["endereco.numero"]

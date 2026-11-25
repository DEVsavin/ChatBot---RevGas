"""Testes da Conversa (C1): cada teste é um diálogo.

Seam combinado: Conversa.iniciar() / responder(texto) -> [mensagens].
O que a API "viu" é conferido pela API falsa. Dados fictícios (DDD 00).
"""

from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api import Resposta
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import TIMEOUT_SEM_CRIAR, ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.conversa.entrada import normalizar
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.vendas.pedido import ProdutoCatalogo
from revgas_chatbot.vendas.registro import RegistradorDeVendas

TELEFONE = "5500900000001"  # DDD 00: fictício
SEGURANCA = "ORIENTAÇÕES DE SEGURANÇA (texto de teste)"
CATALOGO = {
    26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00")),
    26255: ProdutoCatalogo(nome="água 20 L", preco=Decimal("15.00")),  # 2 produtos: a lista aparece (ticket 04)
}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"}
MARIA = {
    "id": 77,
    "name": "Maria Exemplo",
    "phones": [TELEFONE],
    "location": "Rua Exemplo",
    "number": "100",
    "neighborhood": "Centro",
    "city": "Teresina",
    "state_federation": "Piauí",
}


@pytest.fixture
def api():
    api = ApiRevGasFalsa(relogio=RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)))
    api.clientes.append(dict(MARIA))
    return api


@pytest.fixture
def dependencias(api):
    cadenciada = ApiCadenciada(api, api.relogio)
    registrador = RegistradorDeVendas(
        api=cadenciada,
        relogio=api.relogio,
        catalogo=CATALOGO,
        pagamentos_permitidos=PAGAMENTOS,
        establishment_id=2580,
    )
    return Dependencias(
        api=cadenciada,
        registrador=registrador,
        catalogo=CATALOGO,
        pagamentos=PAGAMENTOS,
        seguranca=SEGURANCA,
    )


@pytest.fixture
def conversa(dependencias):
    return Conversa(TELEFONE, dependencias)


def dialogo(conversa, *mensagens_do_cliente) -> list[list[str]]:
    """Inicia a conversa e manda as mensagens; devolve as respostas de cada passo."""
    respostas = [conversa.iniciar()]
    for mensagem in mensagens_do_cliente:
        respostas.append(conversa.responder(mensagem))
    return respostas


def texto(respostas: list[str]) -> str:
    return "\n".join(respostas)


def test_cliente_cadastrado_faz_pedido_completo(conversa, api):
    inicio, numero, produto, quantidade, endereco, pagamento, momento_pix, confirmacao = dialogo(
        conversa, "sim", "1", "2", "sim", "1", "2", "1"
    )

    assert "Este número é seu: (00) 90000-0001?" in texto(inicio)
    assert "Olá, Maria!" in texto(numero)
    assert "1) botijão de gás — R$ 110,00" in texto(numero)
    assert "Quantos?" in texto(produto)
    assert "Entregar em Rua Exemplo, 100 — Centro, Teresina?" in texto(quantidade)
    assert "1) PIX" in texto(endereco)
    assert "O pagamento no PIX será:" in texto(pagamento)
    resumo = texto(momento_pix)
    for trecho in ("Maria Exemplo", "(00) 90000-0001", "2x botijão de gás — R$ 220,00",
                   "Rua Exemplo, 100 — Centro, Teresina, Piauí", "Pagamento: PIX (pagar na entrega)", "1) Confirmar"):
        assert trecho in resumo

    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 77
    assert [p["quantity"] for p in venda["products"]] == [2]  # 1 linha, quantity 2 (M4)
    assert venda["payments"] == [{"payment_type": "PIX", "value": Decimal("220.00")}]
    assert texto(confirmacao) == (
        f"Pedido registrado! Número {venda['id']}, total R$ 220,00. "
        "Para acompanhar, escreva status. Para cancelar, escreva cancelar. Para outra coisa, escreva atendente."
    )


# --- resposta que não serve: explica e repete a mesma pergunta ---------------------


@pytest.mark.parametrize(
    "antes, invalida, explicacao, repetida",
    [
        ((), "talvez", "Responda sim ou não.", "Este número é seu: (00) 90000-0001?"),
        (("sim",), "9", "Escolha um número da lista.", "O que você quer pedir?"),
        (("sim", "1"), "0", "Envie um número de 1 a 10.", "Quantos?"),
        (("sim", "1"), "11", "Envie um número de 1 a 10.", "Quantos?"),
        (("sim", "1"), "muitos", "Envie um número de 1 a 10.", "Quantos?"),  # "dois" agora vale 2 (ticket 03)
        (("sim", "1", "2"), "talvez", "Responda sim ou não.", "Entregar em Rua Exemplo"),
        (("sim", "1", "2", "sim"), "7", "Escolha um número da lista.", "Como vai pagar?"),
        (("sim", "1", "2", "sim", "1", "2"), "talvez", "Escolha um número da lista.", "1) Confirmar"),
    ],
)
def test_resposta_que_nao_serve_explica_e_repete_a_pergunta(conversa, api, antes, invalida, explicacao, repetida):
    *_, resposta = dialogo(conversa, *antes, invalida)

    assert resposta[0] == explicacao
    assert repetida in texto(resposta[1:])
    assert api.vendas_criadas() == []


def test_depois_de_uma_resposta_invalida_a_conversa_segue_normal(conversa, api):
    *_, confirmacao = dialogo(conversa, "talvez", "sim", "9", "1", "0", "2", "sim", "7", "1", "2", "x", "1")

    assert texto(confirmacao).startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_busca_do_cliente_e_registro_respeitam_1_segundo(conversa, api):
    dialogo(conversa, "sim", "1", "2", "sim", "1", "2", "1")

    instantes = api.requisicoes
    assert len(instantes) == 3  # busca na conversa + busca e criação no registro
    assert all((b - a).total_seconds() >= 1 for a, b in zip(instantes, instantes[1:]))


# --- C1c: cliente novo, endereço campo a campo, troco, número digitado --------------

NOVO = "5500900000002"  # não cadastrado
ENDERECO_DIGITADO = ("Rua Nova", "S/N", "pular", "Bairro Novo", "Teresina", "Casa 2", "pular")


@pytest.fixture
def conversa_nova(dependencias):
    return Conversa(NOVO, dependencias)


def test_cliente_novo_informa_nome_e_endereco_campo_a_campo(conversa_nova, api):
    respostas = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", *ENDERECO_DIGITADO, "1", "2", "1")

    perguntas = [texto(r) for r in respostas]
    assert "Qual é o seu nome?" in perguntas[1]
    assert "Olá, Joana!" in perguntas[2]
    for pergunta in ("Qual é a rua?", "Qual é o número?", "Qual é o bairro?", "Qual é a cidade?",
                     "Complemento?", "Ponto de referência?"):  # o estado não é perguntado (agilizar, A1)
        assert any(pergunta in p for p in perguntas), pergunta
    resumo = perguntas[-2]
    assert "Nome: Joana Teste" in resumo
    assert "Rua Nova, S/N — Bairro Novo, Teresina, Piauí" in resumo

    [cliente] = [c for c in api.clientes if c["phones"] == [NOVO]]
    assert cliente["name"] == "Joana Teste"
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == cliente["id"]
    assert venda["complement"] == "Casa 2"
    assert "landmark" not in venda
    assert texto(respostas[-1]).startswith("Pedido registrado!")


@pytest.mark.parametrize(
    "passo, invalida, explicacao",
    [
        (0, "  ", "Preciso do seu nome para o pedido."),
        (3, " ", "Preciso da rua para a entrega."),
        (7, " ", "Preciso da cidade para a entrega."),
    ],
)
def test_campo_livre_invalido_explica_e_repete(conversa_nova, passo, invalida, explicacao):
    # passo: quantas respostas válidas antes do campo (depois do "sim" do número)
    validas = ["Joana Teste", "1", "1", "Rua Nova", "S/N", "pular", "Bairro Novo", "Teresina"][:passo]
    *_, antes, resposta = dialogo(conversa_nova, "sim", *validas, invalida)

    assert resposta[0] == explicacao
    assert resposta[1] == antes[-1]  # a mesma pergunta de novo


def test_numero_negado_pede_outro_e_confirma(conversa_nova):
    respostas = dialogo(conversa_nova, "não", "123", "(00) 90000-0003", "não", "00900000004", "sim")

    assert "Qual é o seu número com DDD?" in texto(respostas[1])
    assert respostas[2][0] == "Não entendi o número. Envie com DDD, por exemplo (86) 99999-0000."
    assert "É este: (00) 90000-0003? (sim/não)" in texto(respostas[3])
    assert "Qual é o seu número com DDD?" in texto(respostas[4])
    assert "É este: (00) 90000-0004? (sim/não)" in texto(respostas[5])
    assert "Qual é o seu nome?" in texto(respostas[6])


def test_numero_digitado_de_cliente_cadastrado_nao_revela_o_cadastro(conversa_nova, api):
    # V1: alguém digita o número da Maria; o chatbot não pode mostrar nada dela.
    respostas = dialogo(
        conversa_nova, "não", "(00) 90000-0001", "sim", "Fulano", "1", "1", *ENDERECO_DIGITADO, "1", "2"
    )
    conversa_inteira = "\n".join(texto(r) for r in respostas)

    assert "Maria" not in conversa_inteira
    assert "Rua Exemplo" not in conversa_inteira
    assert "Qual é o seu nome?" in texto(respostas[3])
    assert api.operacoes == []  # nenhuma busca de cliente antes do registro

    confirmacao = conversa_nova.responder("1")
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 77  # o registrador acha o cadastro pelo número
    assert venda["location"] == "Rua Nova"  # com o endereço digitado
    assert "Maria" not in texto(confirmacao)


def test_endereco_do_cadastro_negado_pede_campo_a_campo(conversa, api):
    *_, confirmacao = dialogo(conversa, "sim", "1", "1", "não", *ENDERECO_DIGITADO, "1", "2", "1")

    [venda] = api.vendas_criadas()
    assert venda["location"] == "Rua Nova"
    assert venda["number"] == "S/N"
    assert texto(confirmacao).startswith("Pedido registrado!")


def test_cadastro_com_endereco_incompleto_nao_e_oferecido(conversa, api):
    del api.clientes[0]["number"]
    respostas = dialogo(conversa, "sim", "1", "1")

    assert "Entregar em" not in texto(respostas[-1])
    assert "Qual é a rua?" in texto(respostas[-1])


@pytest.mark.parametrize("troco, no_resumo, na_observacao", [
    ("R$ 250", "Pagamento: Dinheiro, troco para R$ 250,00", "Troco para R$ 250,00."),
    ("não", "Pagamento: Dinheiro, sem troco", None),
])
def test_dinheiro_pergunta_o_troco(conversa, api, troco, no_resumo, na_observacao):
    respostas = dialogo(conversa, "sim", "1", "2", "sim", "2", troco, "1")

    assert "Vai precisar de troco?" in texto(respostas[5])
    assert no_resumo in texto(respostas[6])
    [venda] = api.vendas_criadas()
    assert venda["payments"] == [{"payment_type": "MONEY", "value": Decimal("220.00")}]
    if na_observacao:
        assert na_observacao in venda["observation"]
    else:
        assert "Troco" not in venda["observation"]


@pytest.mark.parametrize("troco", ["100", "dez reais", "0"])
def test_troco_menor_que_o_total_ou_invalido_explica_e_repete(conversa, troco):
    *_, resposta = dialogo(conversa, "sim", "1", "2", "sim", "2", troco)

    assert resposta[0] == "O troco precisa ser para pelo menos R$ 220,00, ou responda 'não'."
    assert "Vai precisar de troco?" in resposta[1]


# --- cliente que não dá para identificar com segurança → atendente -------------------


def _ambiguo(api):
    api.clientes.append({"id": 78, "name": "Outra Pessoa", "phones": [TELEFONE]})


def _erro(api):
    api.roteirizar("buscar_clientes", Resposta(500, {"message": "erro"}))


def _timeout(api):
    api.roteirizar("buscar_clientes", TIMEOUT_SEM_CRIAR)


def _estranha(api):
    api.roteirizar("buscar_clientes", Resposta(200, {"data": [{"sem_id": 1}]}))


@pytest.mark.parametrize("cenario", [_ambiguo, _erro, _timeout, _estranha])
def test_cliente_nao_identificado_vai_para_o_atendente(conversa, api, cenario):
    cenario(api)
    _, resposta = dialogo(conversa, "sim")

    assert texto(resposta) == (
        "Não consegui identificar seu cadastro agora. "
        "Uma pessoa da nossa equipe vai continuar o atendimento."
    )
    assert "Maria" not in texto(resposta)
    assert api.vendas_criadas() == []


# --- C1d: resumo (corrigir, cancelar) e depois de encerrada ---------------------------

ATE_O_RESUMO = ("sim", "1", "2", "sim", "1", "2")  # Maria, 2 botijões, endereço do cadastro, PIX na entrega


def test_cancelar_no_resumo_descarta_sem_venda_nem_solicitacao(conversa, api):
    *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "3")

    assert texto(resposta) == "Pedido descartado. Nada foi enviado."
    assert api.vendas_criadas() == []


def test_corrigir_mostra_o_menu(conversa):
    *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "2")

    assert "O que quer corrigir?\n1) Produto\n2) Endereço\n3) Pagamento\n4) Nome" in texto(resposta)


def test_corrigir_produto_volta_ao_resumo_sem_refazer_o_resto(conversa, api):
    *_, menu, quantidade, resumo = dialogo(conversa, *ATE_O_RESUMO, "2", "1", "1", "3")

    assert "Quantos?" in texto(quantidade)
    assert "3x botijão de gás — R$ 330,00" in texto(resumo)
    assert "Rua Exemplo, 100" in texto(resumo)  # endereço mantido
    assert "Pagamento: PIX (pagar na entrega)" in texto(resumo)  # pagamento mantido
    conversa.responder("1")
    [venda] = api.vendas_criadas()
    assert [p["quantity"] for p in venda["products"]] == [3]  # 1 linha, quantity 3 (M4)


def test_corrigir_endereco_pede_campo_a_campo_e_volta_ao_resumo(conversa, api):
    *_, resumo = dialogo(conversa, *ATE_O_RESUMO, "2", "2", *ENDERECO_DIGITADO)

    assert "Rua Nova, S/N — Bairro Novo, Teresina, Piauí" in texto(resumo)
    assert "2x botijão de gás" in texto(resumo)


def test_corrigir_pagamento_e_nome(conversa, api):
    dialogo(conversa, *ATE_O_RESUMO, "2", "3", "3")  # cartão de débito
    *_, resumo = [conversa.responder("2"), conversa.responder("4"), conversa.responder("Maria E. Souza")]

    assert "Pagamento: Cartão de débito" in texto(resumo)
    assert "Nome: Maria E. Souza" in texto(resumo)


def test_corrigir_produto_com_troco_que_ficou_menor_que_o_total_pergunta_o_troco_de_novo(conversa):
    # 1 botijão (R$ 110), dinheiro com troco para 150; depois corrige para 2 (R$ 220).
    dialogo(conversa, "sim", "1", "1", "sim", "2", "150")
    *_, resposta = [conversa.responder("2"), conversa.responder("1"), conversa.responder("1"), conversa.responder("2")]

    assert "Vai precisar de troco?" in texto(resposta)


def test_depois_de_encerrada_nada_e_enviado_e_oi_recomeca(conversa, api):
    *_, registrada, de_novo, oi = dialogo(conversa, *ATE_O_RESUMO, "1", "1", "oi")

    # Problema 4 (15/11): depois do pedido, um número solto explica em vez de recomeçar.
    assert "já foi registrado" in texto(de_novo) and "escreva atendente" in texto(de_novo)
    assert len(api.vendas_criadas()) == 1
    assert "Este número é seu: (00) 90000-0001?" in texto(oi)
    dialogo_continuado = [conversa.responder(m) for m in ("sim", "1", "1", "sim", "1", "2", "1")]
    assert texto(dialogo_continuado[-1]).startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 2  # rascunho novo = pedido novo


# --- C1d: resultados do registrador -----------------------------------------------


def _422(campo):
    return Resposta(422, {"data": [{"field": campo, "message": "texto cru da API"}], "message": "x"})


def test_pedido_invalido_leva_ao_campo_e_depois_registra(conversa, api):
    api.roteirizar("criar_venda", _422("location"))
    *_, invalido = dialogo(conversa, *ATE_O_RESUMO, "1")

    assert invalido[0] == "Precisamos corrigir: confira a rua do endereço."
    assert "texto cru da API" not in texto(invalido)
    assert "Qual é a rua?" in texto(invalido)
    *_, resumo = [conversa.responder(m) for m in ENDERECO_DIGITADO]
    assert "Rua Nova" in texto(resumo)
    confirmacao = conversa.responder("1")
    assert texto(confirmacao).startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_pedido_invalido_no_nome_volta_ao_nome(conversa_nova, api):
    api.roteirizar("cadastrar_cliente", _422("name"))
    *_, invalido = dialogo(conversa_nova, "sim", "Joana", "1", "1", *ENDERECO_DIGITADO, "1", "2", "1")

    assert invalido[0] == "Precisamos corrigir: confira o seu nome."
    assert "Qual é o seu nome?" in texto(invalido)
    resumo = conversa_nova.responder("Joana Teste")
    assert "Nome: Joana Teste" in texto(resumo)


def test_transferencia_do_registrador_sem_dizer_registrado(conversa, api):
    api.roteirizar("criar_venda", Resposta(403, {"message": "x"}))
    *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "1")

    assert texto(resposta) == (
        "Não consegui concluir seu pedido agora. "
        "Uma pessoa da nossa equipe vai continuar o atendimento."
    )
    assert "registrado" not in texto(resposta).lower()


def test_transferencia_com_venda_que_pode_existir_leva_o_aviso_e_a_marca_no_log(conversa, api, caplog):
    api.roteirizar("criar_venda", Resposta(500, {"message": "x"}))  # incerto: reconcilia e não acha
    with caplog.at_level("ERROR"):
        *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "1")

    assert "Pedido registrado" not in texto(resposta)  # RN8: não afirma que registrou
    assert NAO_REFACA in texto(resposta)
    registros = "\n".join(r.getMessage() for r in caplog.records)
    assert "[chatbot:" in registros
    for pessoal in (TELEFONE, "Maria", "Rua Exemplo"):
        assert pessoal not in registros


def test_confirmacao_repetida_durante_o_registro_responde_em_andamento(conversa, api):
    respostas_internas = []
    dialogo(conversa, *ATE_O_RESUMO)
    api.roteirizar("buscar_clientes", lambda: respostas_internas.append(conversa.responder("1")))

    final = conversa.responder("1")

    assert respostas_internas == [["Seu pedido já está sendo registrado. Aguarde um instante."]]
    assert texto(final).startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


NAO_REFACA = "Não faça um novo pedido: nossa equipe vai verificar se ele foi registrado."


@pytest.mark.parametrize("falha, avisa", [(Resposta(500, {"message": "x"}), True), (Resposta(403, {"message": "x"}), False)])
def test_so_avisa_para_nao_refazer_quando_a_venda_pode_existir(conversa, api, falha, avisa):
    # I2: com venda_pode_existir, o cliente não pode ser levado a pedir de novo.
    api.roteirizar("criar_venda", falha)
    *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "1")

    assert (NAO_REFACA in texto(resposta)) is avisa


# --- C1e: comandos que valem em qualquer passo ---------------------------------------


@pytest.mark.parametrize(
    "antes",
    [
        (),  # na confirmação do número
        ("sim", "1"),  # na quantidade
        ("não", "(00) 90000-0003", "sim"),  # no nome (texto livre)
        ATE_O_RESUMO,  # no resumo
    ],
)
def test_vazamento_em_qualquer_passo_orienta_primeiro(conversa, api, antes):
    *_, resposta = dialogo(conversa, *antes, "estou sentindo cheiro de gás")

    assert resposta[0] == SEGURANCA
    assert "193" in texto(resposta[1:])
    assert api.vendas_criadas() == []
    assert texto(conversa.responder("1")) == "Para um novo pedido, envie oi."  # encerrada


@pytest.mark.parametrize(
    "antes, mensagem",
    [
        (("sim",), "quero cancelar"),
        (("sim", "1"), "preciso devolver o botijão"),
        (("sim", "1", "2", "sim"), "quero falar com um atendente"),
        (("não", "(00) 90000-0003", "sim"), "atendente"),  # nome: mensagem inteira
    ],
)
def test_cancelar_devolver_ou_atendente_transfere(conversa, api, antes, mensagem):
    *_, resposta = dialogo(conversa, *antes, mensagem)

    assert texto(resposta) == "Uma pessoa da nossa equipe vai continuar o atendimento."
    assert api.vendas_criadas() == []


def test_sair_encerra_sem_enviar_nada(conversa, api):
    *_, resposta = dialogo(conversa, "sim", "1", "sair")

    assert texto(resposta) == "Tudo bem, até mais!"
    assert api.vendas_criadas() == []


def test_quero_cancelar_no_resumo_so_descarta(conversa, api):
    *_, resposta = dialogo(conversa, *ATE_O_RESUMO, "quero cancelar")

    assert texto(resposta) == "Pedido descartado. Nada foi enviado."


def test_cancelar_depois_da_venda_anota_na_venda_via_api(conversa, api):
    dialogo(conversa, *ATE_O_RESUMO, "1")
    [venda] = api.vendas_criadas()

    resp = texto(conversa.responder("quero cancelar o pedido"))

    assert f"ERP-{venda['id']}" in resp
    assert "[CHATBOT][ATENDIMENTO]" in api.vendas_criadas()[0]["observation"]


def test_palavras_de_comando_dentro_de_respostas_nao_sequestram(conversa_nova, api):
    respostas = dialogo(
        conversa_nova, "sim", "Maria Pessoa", "1", "1",
        "Rua do Atendente, 10", "S/N", "pular", "Centro", "Teresina", "pular", "pular",
        "vou pagar pessoalmente",
    )

    assert "Olá, Maria!" in texto(respostas[2])
    assert respostas[-1][0] == "Escolha um número da lista."  # 'pessoalmente' não é comando
    conversa_nova.responder("1")
    resumo = [conversa_nova.responder("2")]
    assert "Rua do Atendente, S/N" in texto(resumo[0])  # ticket 01: só o trecho antes da vírgula


@pytest.mark.parametrize("forma", ["3", "4"])  # cartão de débito e de crédito
def test_nunca_pede_dados_do_cartao_senha_ou_codigo(conversa, forma):
    respostas = dialogo(conversa, "sim", "1", "1", "sim", forma, "1")
    tudo = normalizar("\n".join(texto(r) for r in respostas))

    for proibido in ("numero do cartao", "validade", "cvv", "senha", "codigo de seguranca", "codigo"):
        assert proibido not in tudo
    assert texto(respostas[-1]).startswith("Pedido registrado!")


# --- W1: número que não é do canal (web, CN-2) ------------------------------------------


def test_numero_nao_confiavel_e_tratado_como_digitado(dependencias, api):
    # V1 na web: um número que não é fictício não pode revelar o cadastro.
    conversa = Conversa(TELEFONE, dependencias, numero_confiavel=False)
    respostas = dialogo(conversa, "sim", "Fulano", "1", "1", *ENDERECO_DIGITADO, "1", "2")
    tudo = "\n".join(texto(r) for r in respostas)

    assert "Qual é o seu nome?" in texto(respostas[1])
    assert "Maria" not in tudo and "Rua Exemplo" not in tudo
    assert api.operacoes == []  # nenhuma busca de cliente antes do registro
    conversa.responder("1")
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 77  # o registrador acha o cadastro pelo número


def test_numero_nao_confiavel_continua_assim_depois_de_oi(dependencias, api):
    conversa = Conversa(TELEFONE, dependencias, numero_confiavel=False)
    dialogo(conversa, "sair")
    conversa.responder("oi")

    assert "Qual é o seu nome?" in texto(conversa.responder("sim"))
    assert api.operacoes == []


# --- Ajuste do endereço (A1–A3), sem IA -------------------------------------------------

NAO_DESCOBRE = "Não consigo descobrir o endereço sozinho."


@pytest.mark.parametrize("resposta", ["voce consegue pegar a rua", "qual voce acha q é", "não sei", "rua?"])
@pytest.mark.parametrize("campo, antes", [("rua", ()), ("bairro", ("Rua Nova", "10", "pular"))])
def test_pergunta_ou_nao_sei_no_endereco_nao_e_guardado(conversa_nova, campo, antes, resposta):
    *_, respondido = dialogo(conversa_nova, "sim", "Joana", "1", "1", *antes, resposta)

    assert respondido[0].startswith(NAO_DESCOBRE)
    assert "atendente" in respondido[0]
    pergunta = "Qual é a rua?" if campo == "rua" else "Qual é o bairro?"
    assert respondido[-1] == pergunta  # repete a mesma pergunta


def test_pergunta_no_nome_nao_vira_nome(conversa_nova):
    *_, respondido = dialogo(conversa_nova, "sim", "não sei")

    assert respondido[-1] == "Qual é o seu nome?"
    assert "Olá" not in texto(respondido)


@pytest.mark.parametrize("rua", ["Rua do Atendente, 10", "Rua Qualquer Coisa", "Rua Onde Canta o Sabiá"])
def test_rua_com_palavra_parecida_continua_valendo(conversa_nova, rua):
    *_, respondido = dialogo(conversa_nova, "sim", "Joana", "1", "1", rua)

    assert "Qual é o número?" in texto(respondido)


@pytest.mark.parametrize("numero", ["ap409", "apto 12", "bloco b"])
def test_apartamento_no_numero_e_recusado_com_a_explicacao(conversa_nova, numero):
    *_, respondido = dialogo(conversa_nova, "sim", "Joana", "1", "1", "Rua Nova", numero)

    assert respondido == [
        "O apartamento ou bloco vai no complemento.",
        "Qual é o número do prédio ou da casa?\n0) Não tem / Pular etapa",
    ]


@pytest.mark.parametrize("numero, gravado", [("10", "10"), ("10A", "10A"), ("S/N", "S/N")])
def test_numero_valido_e_aceito(conversa_nova, api, numero, gravado):
    dialogo(conversa_nova, "sim", "Joana", "1", "1", "Rua Nova", numero, "pular", "Centro", "Teresina",
            "pular", "pular", "1", "2", "1")

    [venda] = api.vendas_criadas()
    assert venda["number"] == gravado


def test_resumo_mostra_complemento_e_referencia(conversa_nova):
    *_, resumo = dialogo(conversa_nova, "sim", "Joana", "1", "1", "Rua Nova", "10", "pular", "Centro", "Teresina",
                         "Condomínio Edifício Goya, ap 409", "perto da praça", "1", "2")

    assert "Complemento: Condomínio Edifício Goya, ap 409" in texto(resumo)
    assert "Referência: perto da praça" in texto(resumo)


def test_resumo_sem_complemento_nem_referencia_nao_mostra_as_linhas(conversa_nova):
    *_, resumo = dialogo(conversa_nova, "sim", "Joana", "1", "1", "Rua Nova", "10", "pular", "Centro", "Teresina",
                         "pular", "pular", "1", "2")

    assert "Complemento:" not in texto(resumo) and "Referência:" not in texto(resumo)


# --- Achados de 18/11 (spec-achados-1118.md) ------------------------------------------------
# Ticket 01: a rua não aceita o endereço inteiro (sem IA).

ENDERECO_INTEIRO = "na rua teste, bairro parque piaui, 8311, teresina piaui pagar no credito"


def test_01_endereco_inteiro_na_rua_aproveita_so_o_trecho_seguro(conversa_nova):
    *_, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", ENDERECO_INTEIRO)

    assert resposta == ["Anotei a rua: rua teste.", "Qual é o número?\n0) Não tem / Pular etapa"]
    *_, resumo = [conversa_nova.responder(m) for m in ("10", "pular", "Centro", "Teresina", "pular", "pular", "1", "2")]
    rua = texto(resumo).split("Entrega: ")[1].split(",")[0]
    assert rua == "rua teste"
    for proibido in ("bairro", "8311", "pagar"):
        assert proibido not in rua


@pytest.mark.parametrize("frase", ["bairro centro 8311 pagar no pix", "8311, teresina"])
def test_01_sem_trecho_seguro_pede_so_o_nome_da_rua(conversa_nova, frase):
    *_, antes, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", frase)

    assert resposta == ["Me diga só o nome da rua (ex.: Rua das Flores).", "Qual é a rua?"]


@pytest.mark.parametrize("rua", ["Rua das Flores", "Av. Frei Serafim", "Rua 7 de Setembro", "Travessa São José"])
def test_01_rua_normal_continua_aceita(conversa_nova, rua):
    *_, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", rua)

    assert resposta == ["Qual é o número?\n0) Não tem / Pular etapa"]


def test_01_rua_com_numero_depois_da_virgula_aproveita_a_rua(conversa_nova):
    *_, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua das Flores, 123")

    assert resposta == ["Anotei a rua: Rua das Flores.", "Qual é o número?\n0) Não tem / Pular etapa"]


# Ticket 02: "n" e variações valem como sem número.


@pytest.mark.parametrize("resposta", ["n", "N", "S/N", "s/n", "sn", "s n", "s.n.", "sem número", "sem numero"])
def test_02_variacoes_de_sem_numero(conversa_nova, resposta):
    *_, seguinte = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua Nova", resposta)

    assert seguinte == ["Qual é a quadra?\n0) Não tem / Pular etapa"]
    *_, resumo = [conversa_nova.responder(m) for m in ("pular", "Centro", "Teresina", "pular", "pular", "1", "2")]
    assert "Entrega: Rua Nova, S/N" in texto(resumo)


@pytest.mark.parametrize("resposta", ["ap409", "apto 12", "bloco b", "apartamento 3"])
def test_02_apartamento_explica_que_vai_no_complemento(conversa_nova, resposta):
    *_, seguinte = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua Nova", resposta)

    assert seguinte[0] == "O apartamento ou bloco vai no complemento."


@pytest.mark.parametrize("resposta", ["abc", "perto do mercado", "12 13"])
def test_02_outra_resposta_invalida_pede_o_numero_com_exemplo(conversa_nova, resposta):
    *_, seguinte = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua Nova", resposta)

    assert seguinte == [
        "Envie o número da casa ou do prédio (ex.: 123) ou escolha 0) Não tem / Pular etapa.",
        "Qual é o número?\n0) Não tem / Pular etapa",
    ]


# Ticket 03: quantidade no começo da frase (ou "N botijões" em qualquer lugar).


@pytest.mark.parametrize("frase, quantidade", [
    ("4 botijoes na rua teste 8311", 4), ("quero 2", 2), ("2 botijões", 2), ("dois", 2), ("quero três", 3),
    ("me manda 3 botijões", 3), ("10", 10),
])
def test_03_quantidade_dentro_da_frase(conversa, api, frase, quantidade):
    *_, seguinte = dialogo(conversa, "sim", "1", frase)

    assert "Entregar em Rua Exemplo, 100" in texto(seguinte)  # o resto da frase não muda o passo seguinte
    conversa.responder("sim")
    conversa.responder("1")
    resumo = conversa.responder("2")
    assert f"{quantidade}x botijão de gás" in texto(resumo)


@pytest.mark.parametrize("frase", ["11", "0", "quero 15 botijões", "nenhum", "uns botijões", "rua 8311"])
def test_03_fora_de_1_a_10_ou_sem_numero_continua_recusado(conversa, frase):
    *_, seguinte = dialogo(conversa, "sim", "1", frase)

    assert seguinte == ["Envie um número de 1 a 10.", "Quantos?"]


# Ticket 05 (Q4): na 2ª recusa seguida da mesma pergunta, exemplo + "escreva atendente".

AJUDA = "ou escreva atendente para falar com uma pessoa"


def test_05_primeira_recusa_e_a_de_hoje_e_a_segunda_traz_exemplo(conversa):
    *_, primeira = dialogo(conversa, "sim", "1", "abc")
    assert primeira == ["Envie um número de 1 a 10.", "Quantos?"]

    segunda = conversa.responder("abc")
    assert AJUDA in texto(segunda)
    assert "Exemplo: 2" in texto(segunda)
    assert segunda[-1] == "Quantos?"  # a pergunta continua por último


def test_05_resposta_aceita_zera_a_contagem(conversa):
    dialogo(conversa, "sim", "1", "abc", "abc")
    conversa.responder("2")  # aceita: vai para o endereço do cadastro
    primeira_de_novo = conversa.responder("talvez")

    assert AJUDA not in texto(primeira_de_novo)
    assert AJUDA in texto(conversa.responder("talvez"))


def test_05_nunca_transfere_sozinho(conversa):
    dialogo(conversa, "sim", "1", *["abc"] * 6)

    assert not conversa.encerrada


def test_05_no_endereco_conta_por_campo(conversa_nova):
    dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua Nova", "abc")
    segunda = conversa_nova.responder("abc")

    assert AJUDA in texto(segunda) and "123" in texto(segunda)
    conversa_nova.responder("10")  # aceito → bairro
    assert AJUDA not in texto(conversa_nova.responder(" "))


# Ticket 01, I1 da revisão: rua de verdade com "cidade"/"bairro" no nome; repetir aceita.


@pytest.mark.parametrize("rua", ["Rua Cidade Nova", "Avenida Cidade Jardim", "Travessa Bairro Novo"])
def test_01_rua_com_cidade_ou_bairro_no_nome_e_aceita(conversa_nova, rua):
    *_, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", rua)

    assert resposta == ["Qual é o número?\n0) Não tem / Pular etapa"]


def test_01_repetir_a_mesma_rua_depois_da_recusa_aceita(conversa_nova):
    *_, recusa = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "Rua do Pagamento")
    assert recusa == ["Me diga só o nome da rua (ex.: Rua das Flores).", "Qual é a rua?"]

    assert conversa_nova.responder("Rua do Pagamento") == ["Qual é o número?\n0) Não tem / Pular etapa"]


def test_01_bairro_com_numero_sem_virgula_ainda_e_endereco_inteiro(conversa_nova):
    *_, resposta = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", "rua teste bairro centro 8311")

    assert resposta[0] == "Me diga só o nome da rua (ex.: Rua das Flores)."

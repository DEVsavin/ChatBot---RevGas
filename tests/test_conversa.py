"""Testes da Conversa (C1): cada teste é um diálogo.

Seam combinado: Conversa.iniciar() / responder(texto) -> [mensagens].
O que a API "viu" é conferido pela API falsa. Dados fictícios (DDD 00).
"""

from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api import Resposta
from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_falsa import TIMEOUT_SEM_CRIAR, ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.conversa import Conversa, Dependencias
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.pedido import ProdutoCatalogo
from revgas_chatbot.registro import RegistradorDeVendas

TELEFONE = "5500900000001"  # DDD 00: fictício
CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
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
def fila(tmp_path, api):
    return FilaDeAtendimento(tmp_path, api.relogio)


@pytest.fixture
def dependencias(api, fila):
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
        fila=fila,
        catalogo=CATALOGO,
        pagamentos=PAGAMENTOS,
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
    inicio, numero, produto, quantidade, endereco, pagamento, confirmacao = dialogo(
        conversa, "sim", "1", "2", "sim", "1", "1"
    )

    assert "Este número é seu: (00) 90000-0001?" in texto(inicio)
    assert "Olá, Maria!" in texto(numero)
    assert "1) botijão de gás — R$ 110,00" in texto(numero)
    assert "Quantos?" in texto(produto)
    assert "Entregar em Rua Exemplo, 100 — Centro, Teresina?" in texto(quantidade)
    assert "1) PIX" in texto(endereco)
    resumo = texto(pagamento)
    for trecho in ("Maria Exemplo", "(00) 90000-0001", "2x botijão de gás — R$ 220,00",
                   "Rua Exemplo, 100 — Centro, Teresina, Piauí", "Pagamento: PIX", "1) confirmar"):
        assert trecho in resumo

    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 77
    assert [p["quantity"] for p in venda["products"]] == [1, 1]
    assert venda["payments"] == [{"payment_type": "PIX", "value": Decimal("220.00")}]
    assert texto(confirmacao) == f"Pedido registrado! Número {venda['id']}, total R$ 220,00."


# --- resposta que não serve: explica e repete a mesma pergunta ---------------------


@pytest.mark.parametrize(
    "antes, invalida, explicacao, repetida",
    [
        ((), "talvez", "Responda sim ou não.", "Este número é seu: (00) 90000-0001?"),
        (("sim",), "9", "Escolha um número da lista.", "O que você quer pedir?"),
        (("sim", "1"), "0", "Envie um número de 1 a 10.", "Quantos?"),
        (("sim", "1"), "11", "Envie um número de 1 a 10.", "Quantos?"),
        (("sim", "1"), "dois", "Envie um número de 1 a 10.", "Quantos?"),
        (("sim", "1", "2"), "talvez", "Responda sim ou não.", "Entregar em Rua Exemplo"),
        (("sim", "1", "2", "sim"), "7", "Escolha um número da lista.", "Como vai pagar?"),
        (("sim", "1", "2", "sim", "1"), "talvez", "Escolha um número da lista.", "1) confirmar"),
    ],
)
def test_resposta_que_nao_serve_explica_e_repete_a_pergunta(conversa, api, antes, invalida, explicacao, repetida):
    *_, resposta = dialogo(conversa, *antes, invalida)

    assert resposta[0] == explicacao
    assert repetida in texto(resposta[1:])
    assert api.vendas_criadas() == []


def test_depois_de_uma_resposta_invalida_a_conversa_segue_normal(conversa, api):
    *_, confirmacao = dialogo(conversa, "talvez", "sim", "9", "1", "0", "2", "sim", "7", "1", "x", "1")

    assert texto(confirmacao).startswith("Pedido registrado!")
    assert len(api.vendas_criadas()) == 1


def test_busca_do_cliente_e_registro_respeitam_1_segundo(conversa, api):
    dialogo(conversa, "sim", "1", "2", "sim", "1", "1")

    instantes = api.requisicoes
    assert len(instantes) == 3  # busca na conversa + busca e criação no registro
    assert all((b - a).total_seconds() >= 1 for a, b in zip(instantes, instantes[1:]))


# --- C1c: cliente novo, endereço campo a campo, troco, número digitado --------------

NOVO = "5500900000002"  # não cadastrado
ENDERECO_DIGITADO = ("Rua Nova", "S/N", "Bairro Novo", "Teresina", "pi", "Casa 2", "pular")


@pytest.fixture
def conversa_nova(dependencias):
    return Conversa(NOVO, dependencias)


def test_cliente_novo_informa_nome_e_endereco_campo_a_campo(conversa_nova, api):
    respostas = dialogo(conversa_nova, "sim", "Joana Teste", "1", "1", *ENDERECO_DIGITADO, "1", "1")

    perguntas = [texto(r) for r in respostas]
    assert "Qual é o seu nome?" in perguntas[1]
    assert "Olá, Joana!" in perguntas[2]
    for pergunta in ("Qual é a rua?", "Qual é o número?", "Qual é o bairro?", "Qual é a cidade?",
                     "Qual é o estado?", "Complemento?", "Ponto de referência?"):
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
        (7, "XX", "Não reconheci o estado. Envie a sigla (PI) ou o nome (Piauí)."),
    ],
)
def test_campo_livre_invalido_explica_e_repete(conversa_nova, passo, invalida, explicacao):
    # passo: quantas respostas válidas antes do campo (depois do "sim" do número)
    validas = ["Joana Teste", "1", "1", "Rua Nova", "S/N", "Bairro Novo", "Teresina"][:passo]
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
        conversa_nova, "não", "(00) 90000-0001", "sim", "Fulano", "1", "1", *ENDERECO_DIGITADO, "1"
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
    *_, confirmacao = dialogo(conversa, "sim", "1", "1", "não", *ENDERECO_DIGITADO, "1", "1")

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
def test_cliente_nao_identificado_vai_para_o_atendente(conversa, api, fila, cenario):
    cenario(api)
    _, resposta = dialogo(conversa, "sim")

    assert texto(resposta) == (
        "Não consegui identificar seu cadastro agora. "
        "Uma pessoa da nossa equipe vai continuar o atendimento. Protocolo ATD-20261111-1."
    )
    [aberta] = fila.listar_abertas()
    assert aberta.tipo == "ATENDIMENTO"
    assert aberta.telefone == TELEFONE
    assert "Maria" not in aberta.resumo + texto(resposta)
    assert api.vendas_criadas() == []

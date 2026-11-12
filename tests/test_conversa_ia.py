"""Testes do C2b: a Conversa com a IA (falsa) entendendo texto livre.

Seam combinado: diálogos Conversa.iniciar()/responder(), agora com um
InterpretadorFalso nas dependências. Dados fictícios (DDD 00).
"""

from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.conversa import Conversa, Dependencias
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.interpretacao import Interpretacao, InterpretadorFalso
from revgas_chatbot.pedido import ProdutoCatalogo
from revgas_chatbot.registro import RegistradorDeVendas

TELEFONE = "5500900000001"
NOVO = "5500900000002"
SEGURANCA = "ORIENTAÇÕES DE SEGURANÇA (texto de teste)"
CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"}
MARIA = {
    "id": 77, "name": "Maria Exemplo", "phones": [TELEFONE], "location": "Rua Exemplo",
    "number": "100", "neighborhood": "Centro", "city": "Teresina", "state_federation": "Piauí",
}
ENDERECO_DITO = {"rua": "Rua Nova", "numero": "10", "bairro": "Centro", "cidade": "Teresina", "estado": "PI"}


@pytest.fixture
def api():
    api = ApiRevGasFalsa(relogio=RelogioFalso(datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)))
    api.clientes.append(dict(MARIA))
    return api


@pytest.fixture
def fila(tmp_path, api):
    return FilaDeAtendimento(tmp_path, api.relogio)


@pytest.fixture
def ia():
    return InterpretadorFalso()


@pytest.fixture
def dependencias(api, fila, ia):
    cadenciada = ApiCadenciada(api, api.relogio)
    return Dependencias(
        api=cadenciada,
        registrador=RegistradorDeVendas(
            api=cadenciada, relogio=api.relogio, catalogo=CATALOGO,
            pagamentos_permitidos=PAGAMENTOS, establishment_id=2580,
        ),
        fila=fila,
        catalogo=CATALOGO,
        pagamentos=PAGAMENTOS,
        seguranca=SEGURANCA,
        interpretador=ia,
    )


@pytest.fixture
def conversa(dependencias):
    return Conversa(TELEFONE, dependencias)


def dialogo(conversa, *mensagens):
    respostas = [conversa.iniciar()]
    respostas += [conversa.responder(m) for m in mensagens]
    return respostas


def texto(respostas):
    return "\n".join(respostas)


def pedir(**campos):
    return Interpretacao(intencao="PEDIR", **campos)


# --- 3.1: produto (e o que vier junto) -------------------------------------------------


def test_quero_2_botijoes_pula_a_pergunta_da_quantidade(conversa, ia):
    ia.respostas["quero 2 botijões"] = pedir(produto="botijão de gás", quantidade=2)
    *_, resposta = dialogo(conversa, "sim", "quero 2 botijões")

    assert "Quantos?" not in texto(resposta)
    assert "Entregar em Rua Exemplo, 100" in texto(resposta)  # próximo item que falta


def test_frase_completa_vai_direto_ao_resumo_com_preco_do_catalogo(conversa, api, ia):
    frase = "manda 2 botijão pra Rua Nova 10, Centro, Teresina-PI, pago no pix"
    ia.respostas[frase] = pedir(produto="botijão de gás", quantidade=2, endereco=ENDERECO_DITO, pagamento="PIX")
    *_, resumo = dialogo(conversa, "sim", frase)

    assert "Confira seu pedido:" in texto(resumo)
    assert "2x botijão de gás — R$ 220,00" in texto(resumo)
    assert "Rua Nova, 10 — Centro, Teresina, Piauí" in texto(resumo)  # o dito substitui o do cadastro
    assert api.vendas_criadas() == []  # nada é enviado sem confirmar o resumo
    conversa.responder("1")
    [venda] = api.vendas_criadas()
    assert venda["location"] == "Rua Nova"


def test_endereco_incompleto_na_frase_e_ignorado(conversa, ia):
    frase = "2 botijões pra Rua Nova"
    ia.respostas[frase] = pedir(produto="botijão de gás", quantidade=2, endereco={"rua": "Rua Nova"})
    *_, resposta = dialogo(conversa, "sim", frase)

    assert "Entregar em Rua Exemplo, 100" in texto(resposta)


def test_dinheiro_sem_troco_na_frase_pergunta_o_troco(conversa, ia):
    frase = "2 botijões pra Rua Nova 10, Centro, Teresina-PI, pago em dinheiro"
    ia.respostas[frase] = pedir(produto="botijão de gás", quantidade=2, endereco=ENDERECO_DITO, pagamento="Dinheiro")
    *_, resposta = dialogo(conversa, "sim", frase)

    assert "Vai precisar de troco?" in texto(resposta)


def test_troco_valido_na_frase_vai_ao_resumo(conversa, ia):
    frase = "2 botijões pra Rua Nova 10, Centro, Teresina-PI, dinheiro, troco pra 250"
    ia.respostas[frase] = pedir(
        produto="botijão de gás", quantidade=2, endereco=ENDERECO_DITO, pagamento="Dinheiro", troco="250"
    )
    *_, resumo = dialogo(conversa, "sim", frase)

    assert "Pagamento: Dinheiro, troco para R$ 250,00" in texto(resumo)


def test_resposta_que_o_roteiro_entende_nao_chama_a_ia(conversa, ia):
    dialogo(conversa, "sim", "1", "2", "sim", "1")

    assert ia.recebidos == []


def test_ia_sem_resposta_se_comporta_como_o_c1(conversa, ia):
    *_, resposta = dialogo(conversa, "sim", "quero um negócio aí")  # a IA devolve None

    assert resposta[0] == "Escolha um número da lista."
    assert "O que você quer pedir?" in texto(resposta)


# --- 3.3: intenção em qualquer passo + vazamento pela IA --------------------------------


def test_vazamento_reconhecido_pela_ia_sem_palavra_chave(conversa, fila, ia):
    ia.respostas["tem um cheiro esquisito aqui"] = Interpretacao(intencao="VAZAMENTO")
    *_, resposta = dialogo(conversa, "sim", "tem um cheiro esquisito aqui")

    assert resposta[0] == SEGURANCA
    [aberta] = fila.listar_abertas()
    assert aberta.tipo == "VAZAMENTO" and aberta.prioridade == "URGENTE"


@pytest.mark.parametrize(
    "intencao, tipo",
    [("ATENDENTE", "ATENDIMENTO"), ("CANCELAR_OU_DEVOLVER", "ATENDIMENTO")],
)
def test_intencao_reconhecida_pela_ia_num_passo_de_opcao(conversa, fila, ia, intencao, tipo):
    ia.respostas["deixa pra lá, me passa pra alguém"] = Interpretacao(intencao=intencao)
    *_, resposta = dialogo(conversa, "sim", "1", "deixa pra lá, me passa pra alguém")

    [aberta] = fila.listar_abertas()
    assert aberta.tipo == tipo
    assert f"Protocolo {aberta.protocolo}" in texto(resposta)


def test_sair_reconhecido_pela_ia(conversa, fila, ia):
    ia.respostas["esquece, não quero mais nada"] = Interpretacao(intencao="SAIR")
    *_, resposta = dialogo(conversa, "sim", "esquece, não quero mais nada")

    assert texto(resposta) == "Tudo bem, até mais!"
    assert fila.listar_abertas() == []


# --- S1: passos de texto livre (só VAZAMENTO age) --------------------------------------


def test_cheiro_estranho_no_passo_do_nome_vira_vazamento_e_nao_nome(dependencias, fila, ia):
    ia.respostas["tô sentindo um cheiro estranho na cozinha"] = Interpretacao(intencao="VAZAMENTO")
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = dialogo(conversa, "sim", "tô sentindo um cheiro estranho na cozinha")

    assert resposta[0] == SEGURANCA
    assert fila.listar_abertas()[0].tipo == "VAZAMENTO"


def test_ia_dizendo_atendente_num_passo_de_texto_livre_e_ignorada(dependencias, fila, ia):
    ia.respostas["Rua do Atendente, 10"] = Interpretacao(intencao="ATENDENTE")
    ia.respostas["Maria Pessoa"] = Interpretacao(intencao="ATENDENTE")
    conversa = Conversa(NOVO, dependencias)
    respostas = dialogo(conversa, "sim", "Maria Pessoa", "1", "1", "Rua do Atendente, 10")

    assert "Olá, Maria!" in texto(respostas[2])
    assert "Qual é o número?" in texto(respostas[-1])  # 'Rua do Atendente, 10' virou a rua
    assert fila.listar_abertas() == []


# --- LGPD (C2-4, C2-D1) ------------------------------------------------------------------


def test_so_o_texto_digitado_vai_para_a_ia_e_com_numeros_longos_mascarados(conversa, ia):
    dialogo(conversa, "sim", "meu cpf é 123.456.789-01, quero gás")

    [(enviado, contexto)] = ia.recebidos
    assert "789" not in enviado
    assert "[número omitido]" in enviado
    assert contexto.produtos == ("botijão de gás",)
    tudo = enviado + repr(contexto)
    for do_cadastro in ("Maria", "Rua Exemplo", TELEFONE, "77"):
        assert do_cadastro not in tudo


def test_corrigir_produto_pela_ia_com_troco_que_ficou_menor_pergunta_o_troco_de_novo(conversa, ia):
    # I4: 1 botijão (R$ 110) em dinheiro com troco para 150; na correção, "quero 2" (R$ 220).
    ia.respostas["quero 2 botijões"] = pedir(produto="botijão de gás", quantidade=2)
    dialogo(conversa, "sim", "1", "1", "sim", "2", "150")
    conversa.responder("2")  # corrigir
    conversa.responder("1")  # produto
    resposta = conversa.responder("quero 2 botijões")

    assert "Vai precisar de troco?" in texto(resposta)
    resumo = conversa.responder("250")
    assert "troco para R$ 250,00" in texto(resumo)


# --- C2c: endereço numa frase (3.2) ---------------------------------------------------

ATE_A_RUA = ("sim", "1", "1", "não")  # Maria recusa o endereço do cadastro → "Qual é a rua?"
FRASE = "Rua Nova, 10, Centro, Teresina-PI"


def test_endereco_numa_frase_pergunta_so_o_que_falta(conversa, api, ia):
    ia.respostas[FRASE] = Interpretacao(intencao="PEDIR", endereco=ENDERECO_DITO)
    *_, rua, complemento, referencia = dialogo(conversa, *ATE_A_RUA, FRASE, "pular", "pular")

    assert "Complemento?" in texto(rua)  # rua, número, bairro, cidade e estado já vieram
    assert "Ponto de referência?" in texto(complemento)
    assert "Como vai pagar?" in texto(referencia)
    *_, confirmacao = [conversa.responder("1"), conversa.responder("1")]
    [venda] = api.vendas_criadas()
    assert (venda["location"], venda["number"], venda["state_federation"]) == ("Rua Nova", "10", "Piauí")


def test_endereco_numa_frase_com_complemento_nao_pergunta_o_complemento(conversa, ia):
    frase = FRASE + ", casa 2"
    ia.respostas[frase] = Interpretacao(intencao="PEDIR", endereco={**ENDERECO_DITO, "complemento": "Casa 2"})
    *_, rua = dialogo(conversa, *ATE_A_RUA, frase)

    assert "Ponto de referência?" in texto(rua)


def test_sem_ia_o_texto_inteiro_vira_a_rua(conversa, ia):
    # S3: a IA devolve None → como no C1.
    *_, rua = dialogo(conversa, *ATE_A_RUA, FRASE)

    assert "Qual é o número?" in texto(rua)
    *_, resumo = [conversa.responder(m) for m in ("10", "Centro", "Teresina", "PI", "pular", "pular", "1")]
    assert f"Entrega: {FRASE}, 10" in texto(resumo)


def test_frase_sem_a_rua_preenche_o_que_veio_e_pergunta_a_rua(conversa, ia):
    ia.respostas["Centro, Teresina"] = Interpretacao(
        intencao="PEDIR", endereco={"bairro": "Centro", "cidade": "Teresina"}
    )
    *_, resposta = dialogo(conversa, *ATE_A_RUA, "Centro, Teresina")

    assert "Qual é a rua?" in texto(resposta)
    *_, depois_da_rua, depois_do_numero = [conversa.responder("Rua Nova"), conversa.responder("10")]
    assert "Qual é o estado?" in texto(depois_do_numero)  # bairro e cidade não são perguntados


def test_estado_invalido_na_frase_e_perguntado_depois(conversa, ia):
    ia.respostas[FRASE] = Interpretacao(intencao="PEDIR", endereco={**ENDERECO_DITO, "estado": "XX"})
    *_, resposta = dialogo(conversa, *ATE_A_RUA, FRASE)

    assert "Qual é o estado?" in texto(resposta)


def test_uma_so_chamada_a_ia_por_mensagem_no_campo_da_rua(conversa, ia):
    ia.respostas[FRASE] = Interpretacao(intencao="PEDIR", endereco=ENDERECO_DITO)
    dialogo(conversa, *ATE_A_RUA, FRASE)

    assert [t for t, _ in ia.recebidos] == [FRASE]


def test_vazamento_dito_no_campo_da_rua(conversa, fila, ia):
    ia.respostas["acho que tem gás escapando"] = Interpretacao(intencao="VAZAMENTO")
    *_, resposta = dialogo(conversa, *ATE_A_RUA, "acho que tem gás escapando")

    assert resposta[0] == SEGURANCA
    assert fila.listar_abertas()[0].tipo == "VAZAMENTO"


# --- I5: a IA não pode inventar partes do endereço ---------------------------------------


def test_cidade_e_estado_que_o_cliente_nao_disse_sao_perguntados(conversa, ia):
    ia.respostas["Rua Nova, 10, Centro"] = Interpretacao(intencao="PEDIR", endereco=ENDERECO_DITO)
    *_, resposta = dialogo(conversa, *ATE_A_RUA, "Rua Nova, 10, Centro")

    assert "Qual é a cidade?" in texto(resposta)


def test_endereco_inventado_na_frase_do_produto_e_ignorado(conversa, ia):
    frase = "2 botijões no pix"
    ia.respostas[frase] = pedir(produto="botijão de gás", quantidade=2, endereco=ENDERECO_DITO, pagamento="PIX")
    *_, resposta = dialogo(conversa, "sim", frase)

    assert "Entregar em Rua Exemplo, 100" in texto(resposta)  # pergunta o endereço (o do cadastro)


# --- H3: a IA da cota grátis só enquanto o telefone ATUAL for fictício --------------------


def test_trocar_para_numero_real_desliga_a_ia(dependencias, ia):
    from dataclasses import replace

    ia.respostas["quero 2 botijões"] = pedir(produto="botijão de gás", quantidade=2)
    deps = replace(dependencias, ia_so_com_numero_ficticio=True)
    conversa = Conversa(TELEFONE, deps)  # começa com número fictício (DDD 00)
    dialogo(conversa, "não", "(86) 99999-0000", "sim", "Fulano")

    resposta = conversa.responder("quero 2 botijões")

    assert ia.recebidos == []  # nada foi para a IA depois da troca
    assert resposta[0] == "Escolha um número da lista."  # roteiro do C1


def test_numero_ficticio_digitado_mantem_a_ia(dependencias, ia):
    from dataclasses import replace

    ia.respostas["quero 2 botijões"] = pedir(produto="botijão de gás", quantidade=2)
    conversa = Conversa(TELEFONE, replace(dependencias, ia_so_com_numero_ficticio=True))
    dialogo(conversa, "não", "(00) 90000-0005", "sim", "Fulano")

    assert "Quantos?" not in texto(conversa.responder("quero 2 botijões"))
    # "Fulano" também foi: no passo do nome a IA é consultada só para vazamento (S1).
    assert [t for t, _ in ia.recebidos] == ["Fulano", "quero 2 botijões"]


# --- Ajuste do endereço (A1) com a IA ---------------------------------------------------


def test_pergunta_reconhecida_pela_ia_no_campo_da_rua(conversa, ia):
    ia.respostas["tem como você ver aí pra mim"] = Interpretacao(intencao="PERGUNTA")
    *_, resposta = dialogo(conversa, *ATE_A_RUA, "tem como você ver aí pra mim")

    assert resposta[0].startswith("Não consigo descobrir o endereço sozinho.")
    assert resposta[-1] == "Qual é a rua?"


def test_pergunta_reconhecida_pela_ia_no_bairro(conversa, ia):
    ia.respostas["tem como você ver aí pra mim"] = Interpretacao(intencao="PERGUNTA")
    *_, resposta = dialogo(conversa, *ATE_A_RUA, "Rua Nova", "10", "tem como você ver aí pra mim")

    assert resposta[-1] == "Qual é o bairro?"


def test_pergunta_reconhecida_pela_ia_no_nome(dependencias, ia):
    ia.respostas["tem como você ver aí pra mim"] = Interpretacao(intencao="PERGUNTA")
    conversa = Conversa(NOVO, dependencias)
    *_, resposta = dialogo(conversa, "sim", "tem como você ver aí pra mim")

    assert resposta[-1] == "Qual é o seu nome?"

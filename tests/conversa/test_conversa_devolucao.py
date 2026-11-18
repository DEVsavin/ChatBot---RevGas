"""Testes da devolução na conversa (fase C, spec-conversa.md): DC2 e DC3.

Seam: Conversa.iniciar()/responder(texto) com a API de teste, a fila numa
pasta temporária e a Devolucao real (só lê a API). Dados fictícios.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.atendimento.fila import FilaDeAtendimento
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.devolucao.devolucao import Devolucao
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.ia.interpretacao import Interpretacao, InterpretadorFalso
from revgas_chatbot.vendas.pedido import ProdutoCatalogo
from revgas_chatbot.vendas.registro import RegistradorDeVendas

AGORA = datetime(2026, 11, 18, 15, 0, tzinfo=FUSO_TERESINA)
TELEFONE = "5500900000001"
CATALOGO = {
    26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00")),
    26255: ProdutoCatalogo(nome="água 20 L", preco=Decimal("15.00")),  # 2 produtos: a lista aparece (ticket 04)
}
PAGAMENTOS = {"PIX", "MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"}
MARIA = {
    "id": 77, "name": "Maria Exemplo", "phones": [TELEFONE], "location": "Rua Exemplo",
    "number": "100", "neighborhood": "Centro", "city": "Teresina", "state_federation": "Piauí",
}
SO_LEITURA = {"buscar_clientes", "buscar_vendas"}


@pytest.fixture
def api():
    api = ApiRevGasFalsa(relogio=RelogioFalso(AGORA))
    api.clientes.append(dict(MARIA))
    return api


def semear(api, *, dias_atras=1, quantidade=2, status=None, cliente=77, pagamento="PIX", horas_atras=None,
           observacao=None, entregue_em=None):
    venda = api.semear_venda(
        {"establishment_id": 2580, "customer_id": cliente, "channel": "WHATSAPP",
         "products": [{"product_id": 26717, "quantity": quantidade, "value": 110.0,
                       "product_description": "botijão de gás"}],
         **({"payments": [{"payment_type": pagamento, "value": 110.0 * quantidade}]} if pagamento else {}),
         **({"observation": observacao} if observacao else {})},
        criada_em=AGORA - (timedelta(hours=horas_atras) if horas_atras is not None else timedelta(days=dias_atras)),
    )
    guardada = next(v for v in api.vendas if v["id"] == venda["id"])
    if status:
        guardada["status"] = status
    if entregue_em:
        guardada["delivered_at"] = entregue_em
    return venda["id"]


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
        registrador=RegistradorDeVendas(api=cadenciada, relogio=api.relogio, catalogo=CATALOGO,
                                        pagamentos_permitidos=PAGAMENTOS, establishment_id=2580),
        fila=fila, catalogo=CATALOGO, pagamentos=PAGAMENTOS, seguranca="ORIENTAÇÕES DE SEGURANÇA",
        interpretador=ia,
        devolucao=Devolucao(api=cadenciada, relogio=api.relogio, fila=fila),
    )


@pytest.fixture
def conversa(dependencias):
    return Conversa(TELEFONE, dependencias)


def falar(conversa, *mensagens):
    """Inicia e manda as mensagens; devolve a última resposta como texto."""
    conversa.iniciar()
    resposta = []
    for m in mensagens:
        resposta = conversa.responder(m)
    return "\n".join(resposta)


def abertas(fila):
    return fila.listar_abertas()


# --- DC2: como a devolução começa ---------------------------------------------------------


@pytest.mark.parametrize("frase", ["quero cancelar", "cancela meu pedido", "desisti do pedido", "não quero mais"])
def test_frases_de_cancelamento_mostram_os_pedidos(conversa, api, frase):
    venda = semear(api)
    semear(api, dias_atras=2)  # M2 (18/11): com um pedido só, a pergunta é "É esse?"; a lista é para 2 ou mais
    resposta = falar(conversa, frase)

    assert "Qual pedido?" in resposta
    assert f"1) Pedido {venda}" in resposta
    assert "0) não achei meu pedido" in resposta


@pytest.mark.parametrize("frase", ["demorou", "tá demorando muito"])
def test_demorou_nao_abre_cancelamento(conversa, api, frase):
    # DC-4 (usuário, 18/11).
    semear(api)
    resposta = falar(conversa, frase)

    assert "Qual pedido?" not in resposta


@pytest.mark.parametrize("frase", ["veio com lacre violado", "peso errado", "veio o produto errado",
                                   "botijão amassado", "veio com defeito"])
def test_frases_de_problema_no_produto(conversa, api, frase, fila):
    semear(api)
    falar(conversa, frase, "1")
    falar_mais = conversa.responder("1")  # confirma

    [s] = abertas(fila)
    assert s.tipo == "PROBLEMA_PRODUTO"
    assert s.texto_cliente == frase
    assert "DEV-" in "\n".join(falar_mais)


@pytest.mark.parametrize("frase, motivo", [("paguei duas vezes", "cobrança errada"),
                                           ("me cobraram a mais", "cobrança errada")])
def test_frases_de_cobranca_ja_trazem_o_motivo(conversa, api, fila, frase, motivo):
    semear(api)
    resposta = falar(conversa, frase, "1")

    assert "você prefere" in resposta  # pula o motivo, pergunta a preferência
    conversa.responder("2")
    conversa.responder("1")
    [s] = abertas(fila)
    assert s.tipo == "ESTORNO" and f"Motivo: {motivo}" in s.resumo


@pytest.mark.parametrize("frase", ["quero meu dinheiro de volta", "quero estorno", "reembolso"])
def test_estorno_generico_pergunta_o_motivo(conversa, api, frase):
    semear(api)
    resposta = falar(conversa, frase, "1")

    assert "1) pedido cancelado já pago" in resposta
    assert "3) cobrança errada" in resposta


@pytest.mark.parametrize("frase", ["quero devolver", "devolução"])
def test_devolucao_generica_mostra_o_menu(conversa, frase):
    resposta = falar(conversa, frase)

    assert "1) cancelar um pedido" in resposta and "4) falar com um atendente" in resposta


def test_ia_cancelar_ou_devolver_mostra_o_menu(conversa, ia):
    ia.respostas["quero resolver aquela compra"] = Interpretacao(intencao="CANCELAR_OU_DEVOLVER")
    resposta = falar(conversa, "sim", "quero resolver aquela compra")

    assert "1) cancelar um pedido" in resposta


def test_vazamento_vence_a_devolucao(conversa, fila):
    resposta = falar(conversa, "veio vazando, quero devolver")

    assert "ORIENTAÇÕES DE SEGURANÇA" in resposta
    assert [s.tipo for s in abertas(fila)] == ["VAZAMENTO"]


def test_no_meio_do_pedido_cancelar_pergunta_o_que_fazer(conversa, api, fila):
    resposta = falar(conversa, "sim", "1", "quero cancelar")  # no passo da quantidade

    assert "1) continuar este pedido" in resposta and "2) desistir deste pedido" in resposta
    assert "3) falar de um pedido já feito" in resposta
    assert "Pedido descartado. Nada foi enviado." in "\n".join(conversa.responder("2"))
    assert api.vendas_criadas() == [] and abertas(fila) == []


def test_no_meio_do_pedido_escolher_cancelar_um_pedido_feito(conversa, api):
    semear(api)
    falar(conversa, "sim", "1", "quero cancelar")

    resposta = "\n".join(conversa.responder("3"))
    assert "nada tinha sido enviado" in resposta and "Encontrei seu pedido" in resposta  # M2: um pedido só


def test_no_meio_do_pedido_problema_no_produto_pergunta_antes_de_descartar(conversa, api):
    # I2 (revisão): o rascunho só é descartado por escolha explícita.
    semear(api)
    resposta = falar(conversa, "sim", "1", "veio com lacre violado")

    assert "1) continuar este pedido" in resposta
    assert "Qual pedido?" not in resposta


def test_continuar_este_pedido_repete_a_pergunta_e_o_rascunho_fica_intacto(conversa, api):
    falar(conversa, "sim", "1", "veio com lacre violado")
    assert conversa.responder("1") == ["Quantos?"]  # volta à pergunta de antes
    conversa.responder("2")
    conversa.responder("sim")
    conversa.responder("1")  # PIX → resumo
    assert conversa.responder("1")[0].startswith("Pedido registrado!")  # o pedido seguiu até o fim
    assert len(api.vendas_criadas()) == 1


def test_no_resumo_problema_pergunta_e_continuar_mostra_o_resumo(conversa, api):
    falar(conversa, "sim", "1", "2", "sim", "1")  # no resumo
    resposta = "\n".join(conversa.responder("veio com lacre violado"))
    assert "1) continuar este pedido" in resposta
    assert "Confira seu pedido:" in "\n".join(conversa.responder("1"))
    assert api.vendas_criadas() == []


@pytest.mark.parametrize("frase", ["meu dinheiro é nota de 100", "devolve o troco certo"])
def test_frases_comuns_no_troco_nao_viram_devolucao(conversa, frase):
    falar(conversa, "sim", "1", "1", "sim", "2")  # dinheiro → troco
    resposta = "\n".join(conversa.responder(frase))

    assert "continuar este pedido" not in resposta and "Qual pedido?" not in resposta
    assert "troco" in resposta.lower()


def test_no_resumo_cancelar_continua_descartando(conversa, api, fila):
    resposta = falar(conversa, "sim", "1", "2", "sim", "1", "cancelar")

    assert "Pedido descartado. Nada foi enviado." in resposta
    assert abertas(fila) == []


def test_depois_do_pedido_cancelar_mostra_o_pedido_novo_no_topo(conversa, api):
    falar(conversa, "sim", "1", "2", "sim", "1", "1")
    [venda] = api.vendas_criadas()
    resposta = "\n".join(conversa.responder("quero cancelar"))

    assert f"Quer cancelar o pedido {venda['id']}, que você acabou de fazer?" in resposta  # M2


def test_nome_cancelar_inteiro_entra_mas_frase_no_nome_nao(dependencias, api):
    # V3: no texto livre, só a mensagem inteira vale como comando.
    conversa = Conversa("5500900000002", dependencias)
    assert "Olá, Lacre!" in falar(conversa, "sim", "Lacre")
    conversa2 = Conversa("5500900000002", dependencias)
    assert "Não encontrei pedidos" in falar(conversa2, "sim", "cancelar")  # a mensagem inteira vale


# --- DC3: o diálogo ----------------------------------------------------------------------


def test_cancelamento_completo_com_resumo_e_confirmacao(conversa, api, fila):
    venda = semear(api)
    falar(conversa, "quero cancelar", "1")
    resposta = "\n".join(conversa.responder("1"))  # desisti
    assert "Você já pagou" in resposta
    resumo = "\n".join(conversa.responder("não"))
    assert "Vou pedir à equipe: cancelar" in resumo and f"Pedido {venda}" in resumo
    assert "Motivo: desisti" in resumo and "Confirma? 1) sim 2) não" in resumo
    assert abertas(fila) == []  # nada antes do sim

    final = "\n".join(conversa.responder("1"))
    [s] = abertas(fila)
    assert s.tipo == "CANCELAMENTO" and s.dados["pedido_id"] == venda and s.dados["preferencia"] is None
    assert f"protocolo {s.protocolo}" in final
    assert "Se precisar de comprovante" in final
    assert set(api.operacoes) <= SO_LEITURA  # o chatbot só leu o ERP


def test_motivo_do_cancelamento_sem_demorou(conversa, api):
    semear(api)
    resposta = falar(conversa, "quero cancelar", "1")

    assert "1) desisti\n2) pedi errado\n3) outro" in resposta
    assert "demorou" not in resposta


def test_cancelamento_com_motivo_da_frase_e_ja_pago_pergunta_a_preferencia(conversa, api, fila):
    semear(api)
    resposta = falar(conversa, "desisti do pedido", "1")
    assert "Você já pagou" in resposta  # o motivo veio da frase
    assert "você prefere" in "\n".join(conversa.responder("sim"))
    conversa.responder("1")  # crédito
    conversa.responder("1")  # confirma

    [s] = abertas(fila)
    assert s.dados["motivo"] == "DESISTI" and s.dados["preferencia"] == "CREDITO_PROXIMA_COMPRA"


def test_motivo_outro_pede_uma_frase(conversa, api, fila):
    semear(api)
    falar(conversa, "quero cancelar", "1", "3")
    conversa.responder("achei mais barato")
    conversa.responder("não")
    conversa.responder("1")

    [s] = abertas(fila)
    assert s.dados["motivo"] == "OUTRO" and s.texto_cliente == "achei mais barato"


def test_nao_na_confirmacao_nao_registra_nada(conversa, api, fila):
    semear(api)
    falar(conversa, "quero cancelar", "1", "1", "não")
    resposta = "\n".join(conversa.responder("2"))

    assert "nada foi registrado" in resposta.lower()
    assert abertas(fila) == []


def test_escolha_invalida_repete_a_lista(conversa, api):
    semear(api)
    semear(api, dias_atras=2)  # M2 (18/11): com um pedido só, a pergunta é "É esse?"; a lista é para 2 ou mais
    resposta = falar(conversa, "quero cancelar", "7")

    assert "Escolha um número da lista." in resposta and "Qual pedido?" in resposta


def test_nao_achei_meu_pedido_segue_sem_pedido(conversa, api, fila):
    semear(api)
    semear(api, dias_atras=2)  # M2 (18/11): com um pedido só, a pergunta é "É esse?"; a lista é para 2 ou mais
    falar(conversa, "quero cancelar", "0", "1", "não", "1")

    [s] = abertas(fila)
    assert s.dados["pedido_id"] is None


def test_pedido_cancelado_vira_estorno(conversa, api, fila):
    semear(api, status="CANCEL")  # pago no PIX
    resposta = falar(conversa, "quero cancelar", "1")

    assert "já está cancelado" in resposta and "Você já pagou" in resposta  # ticket 07: PIX pergunta
    assert "você prefere" in "\n".join(conversa.responder("sim"))
    conversa.responder("2")
    conversa.responder("1")
    [s] = abertas(fila)
    assert s.tipo == "ESTORNO" and s.dados["motivo"] == "PEDIDO_CANCELADO_JA_PAGO"
    assert s.dados["preferencia"] == "DINHEIRO_DE_VOLTA"


def test_problema_generico_pede_descricao_e_nao_pergunta_preferencia(conversa, api, fila):
    semear(api)
    resposta = falar(conversa, "problema com o produto", "1")
    assert "Conte em poucas palavras" in resposta
    resumo = "\n".join(conversa.responder("o botijão veio sem gás"))
    assert "você prefere" not in resumo and "Confirma?" in resumo
    conversa.responder("1")

    [s] = abertas(fila)
    assert s.tipo == "PROBLEMA_PRODUTO" and s.prioridade == "ALTA"
    assert s.texto_cliente == "o botijão veio sem gás"


def test_sem_pedidos_no_numero_registra_mesmo_assim(conversa, fila):
    resposta = falar(conversa, "quero cancelar")

    assert "Não encontrei pedidos" in resposta
    conversa.responder("1")
    conversa.responder("não")
    conversa.responder("1")
    [s] = abertas(fila)
    assert s.dados["pedido_id"] is None and "não tem pedidos" in s.resumo


def test_erro_da_api_registra_sem_pedido_com_o_motivo_para_a_equipe(conversa, api, fila):
    from revgas_chatbot.api_revgas.api import Resposta

    api.roteirizar("buscar_clientes", Resposta(500, None))
    resposta = falar(conversa, "quero cancelar")

    assert "Não consegui ver seus pedidos agora" in resposta
    for r in ("1", "não", "1"):
        conversa.responder(r)
    [s] = abertas(fila)
    assert "Não foi possível listar os pedidos" in s.resumo


def test_numero_digitado_nao_lista_pedidos(dependencias, api, fila):
    # V1: número que não é o do canal nunca mostra pedidos.
    semear(api)
    conversa = Conversa(TELEFONE, dependencias)
    resposta = falar(conversa, "não", "(86) 99999-0000", "sim", "cancelar")

    assert "Qual pedido?" not in resposta and "consigo ver seus pedidos" in resposta
    assert "buscar_vendas" not in api.operacoes


def test_falha_na_fila_nao_inventa_protocolo(conversa, api, fila, monkeypatch):
    semear(api)
    falar(conversa, "quero cancelar", "1", "1", "não")

    def quebra(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(fila, "abrir", quebra)
    resposta = "\n".join(conversa.responder("1"))

    assert "Não consegui registrar sua solicitação agora" in resposta
    assert "DEV-" not in resposta


def test_atendente_no_meio_da_devolucao_transfere(conversa, api, fila):
    semear(api)
    resposta = falar(conversa, "quero cancelar", "quero falar com um atendente")

    assert "ATD-" in resposta


def test_depois_do_protocolo_a_conversa_volta_ao_estado_de_antes(conversa, api, fila):
    falar(conversa, "sim", "1", "2", "sim", "1", "1")  # pedido registrado
    for r in ("quero cancelar", "1", "1", "não", "1"):
        conversa.responder(r)

    assert not conversa.encerrada
    assert "já foi registrado" in "\n".join(conversa.responder("3"))  # POS_VENDA de novo


def test_nunca_pede_dado_bancario(conversa, api):
    semear(api)
    textos = [falar(conversa, "quero meu dinheiro de volta", "1")]
    textos += ["\n".join(conversa.responder(r)) for r in ("3", "2", "1")]

    tudo = "\n".join(textos).lower()
    for proibido in ("chave pix", "agência", "conta bancária", "cartão de crédito", "cpf"):
        assert proibido not in tudo


def test_modo_real_lista_o_cliente_fixo(dependencias, api, fila):
    venda = semear(api, cliente=5700355, observacao="DEMONSTRAÇÃO DO CHATBOT. [chatbot:x]")  # M2: só do chatbot
    real = replace(dependencias, devolucao=Devolucao(api=dependencias.api, relogio=api.relogio, fila=fila,
                                                      cliente_fixo=5700355))
    conversa = Conversa("5586999990000", real, numero_confiavel=False)  # como no --venda-real
    resposta = falar(conversa, "quero cancelar")

    assert f"Pedido {venda}" in resposta and "É esse?" in resposta
    assert "buscar_clientes" not in api.operacoes



# --- Ticket 07 (Q5): "Você já pagou?" só no PIX ----------------------------------------------


@pytest.mark.parametrize("pagamento", ["MONEY", "CARTAO_DE_DEBITO", "CARTAO_DE_CREDITO"])
def test_07_pago_na_entrega_nao_pergunta_e_segue_sem_preferencia(conversa, api, fila, pagamento):
    semear(api, pagamento=pagamento)
    resposta = falar(conversa, "quero cancelar", "1", "1")  # pedido, desisti

    assert "Você já pagou" not in resposta and "Confirma?" in resposta
    conversa.responder("1")
    [s] = abertas(fila)
    assert s.dados["preferencia"] is None


@pytest.mark.parametrize("pagamento", ["PIX", None])
def test_07_pix_ou_forma_desconhecida_pergunta(conversa, api, pagamento):
    semear(api, pagamento=pagamento)

    assert "Você já pagou" in falar(conversa, "quero cancelar", "1", "1")


def test_07_cancelado_pago_na_entrega_nao_tem_valor_a_devolver(conversa, api, fila):
    semear(api, status="CANCEL", pagamento="MONEY")
    resposta = falar(conversa, "quero cancelar", "1")

    assert "já está cancelado" in resposta and "não há valor a devolver" in resposta
    assert "você prefere" not in resposta
    assert abertas(fila) == []


def test_07_cancelado_no_pix_sem_pagar_nao_registra(conversa, api, fila):
    semear(api, status="CANCEL")
    falar(conversa, "quero cancelar", "1")

    assert "não há valor a devolver" in "\n".join(conversa.responder("não"))
    assert abertas(fila) == []


def test_06_lista_da_devolucao_com_o_nome_do_produto(dependencias, api):
    api.semear_venda({"establishment_id": 2580, "customer_id": 77, "channel": "WHATSAPP",
                      "products": [{"product_id": 26717, "quantity": 3, "value": 110.0}],
                      "payments": [{"payment_type": "PIX", "value": 330.0}]},
                     criada_em=AGORA - timedelta(days=1))
    conversa = Conversa(TELEFONE, replace(dependencias, devolucao=Devolucao(
        api=dependencias.api, relogio=api.relogio, fila=dependencias.fila, catalogo=CATALOGO)))

    assert "3x botijão de gás" in falar(conversa, "quero cancelar")


# --- Ticket 08 (Q1): depois do pedido, a IA pode abrir o cancelamento ------------------------

ATE_O_PEDIDO = ("sim", "1", "2", "sim", "1", "1")  # catálogo de teste com 2 produtos: "1" = botijão


def test_08_ia_abre_o_cancelamento_no_pos_venda(conversa, ia, api):
    falar(conversa, *ATE_O_PEDIDO)
    ia.respostas["quero cancelaar"] = Interpretacao(intencao="CANCELAR_OU_DEVOLVER")
    resposta = "\n".join(conversa.responder("quero cancelaar"))

    assert "Quer cancelar o pedido" in resposta  # M2: o pedido desta conversa


def test_08_ia_com_outra_frase_mostra_o_menu(conversa, ia):
    falar(conversa, *ATE_O_PEDIDO)
    ia.respostas["me arrependi daquilo"] = Interpretacao(intencao="CANCELAR_OU_DEVOLVER")

    assert "1) cancelar um pedido" in "\n".join(conversa.responder("me arrependi daquilo"))


def test_08_nao_no_resumo_da_devolucao_nao_registra(conversa, ia, fila):
    falar(conversa, *ATE_O_PEDIDO)
    ia.respostas["quero cancelaar"] = Interpretacao(intencao="CANCELAR_OU_DEVOLVER")
    for r in ("quero cancelaar", "1", "1", "não"):
        conversa.responder(r)

    assert "nada foi registrado" in "\n".join(conversa.responder("2")).lower()
    assert abertas(fila) == []


@pytest.mark.parametrize("intencao", ["ATENDENTE", "SAIR"])
def test_08_atendente_ou_sair_vindos_da_ia_continuam_ignorados(conversa, ia, fila, intencao):
    falar(conversa, *ATE_O_PEDIDO)
    ia.respostas["me ajuda aí"] = Interpretacao(intencao=intencao)
    resposta = "\n".join(conversa.responder("me ajuda aí"))

    assert "já foi registrado" in resposta
    assert abertas(fila) == [] and not conversa.encerrada


def test_08_sem_ia_a_palavra_exata_continua_valendo(dependencias):
    conversa = Conversa(TELEFONE, replace(dependencias, interpretador=None))
    falar(conversa, *ATE_O_PEDIDO)

    assert "Quer cancelar o pedido" in "\n".join(conversa.responder("cancelar"))  # M2


# --- Ticket 09 (Q2): pós-venda ensina "cancelar"; atendente leva o contexto ------------------

ENSINA = "Para cancelar, escreva cancelar. Para outra coisa, escreva atendente."


def test_09_as_duas_mensagens_do_pos_venda_ensinam_cancelar(conversa):
    registrado = falar(conversa, *ATE_O_PEDIDO)
    assert registrado.startswith("Pedido registrado!") and ENSINA in registrado
    assert ENSINA in "\n".join(conversa.responder("3"))


def test_09_atendente_depois_do_pedido_leva_o_numero_do_pedido(conversa, api, fila):
    falar(conversa, *ATE_O_PEDIDO)
    [venda] = api.vendas_criadas()
    conversa.responder("atendente")

    [s] = abertas(fila)
    assert s.tipo == "ATENDIMENTO" and f"Último pedido: {venda['id']}." in s.resumo


def test_09_atendente_depois_de_tentar_cancelar_leva_isso(conversa, api, fila):
    falar(conversa, *ATE_O_PEDIDO)
    [venda] = api.vendas_criadas()
    conversa.responder("quero cancelar")
    conversa.responder("1")
    conversa.responder("atendente")

    [s] = abertas(fila)
    assert f"Tentou cancelar o pedido {venda['id']}." in s.resumo


def test_09_atendente_depois_de_travar_leva_a_pergunta(conversa, fila):
    falar(conversa, "sim", "1", "abc", "abc", "atendente")

    [s] = abertas(fila)
    assert "Travou na pergunta: Quantos?" in s.resumo


def test_09_sem_contexto_o_resumo_e_o_de_hoje(conversa, fila):
    falar(conversa, "atendente")

    [s] = abertas(fila)
    assert s.resumo == "Cliente pediu atendente."



# --- Melhorias no cancelamento (pedido-cancelamento-melhorias-2026-11-18.md) ------------------


def test_m1_pedido_de_1h_nao_entregue_e_alta_com_aviso(conversa, api, fila):
    semear(api, horas_atras=1)
    falar(conversa, "quero cancelar", "1", "1", "não")  # é esse, desisti, não pagou
    final = "\n".join(conversa.responder("1"))

    [s] = abertas(fila)
    assert s.prioridade == "ALTA"
    assert "O entregador pode já estar a caminho" in final
    assert final.index("O entregador pode") < final.index("protocolo")
    assert "feito às 14:00" in s.resumo


def test_m1_pedido_de_3h_e_normal_sem_aviso(conversa, api, fila):
    semear(api, horas_atras=3)
    falar(conversa, "quero cancelar", "1", "1", "não")
    final = "\n".join(conversa.responder("1"))

    [s] = abertas(fila)
    assert s.prioridade == "NORMAL" and "entregador" not in final


def test_m2_depois_do_pedido_pergunta_direto_pelo_pedido_feito(conversa, api, fila):
    falar(conversa, *ATE_O_PEDIDO)
    [venda] = api.vendas_criadas()
    resposta = "\n".join(conversa.responder("cancelar"))

    assert f"Quer cancelar o pedido {venda['id']}, que você acabou de fazer?" in resposta
    assert "1) sim" in resposta and "2) é outro pedido" in resposta
    for r in ("1", "1", "não", "1"):  # sim, desisti, não pagou, confirma
        conversa.responder(r)
    [s] = abertas(fila)
    assert s.dados["pedido_id"] == venda["id"]


def test_m2_e_outro_pedido_busca_e_segue(conversa, api):
    semear(api, dias_atras=2)
    falar(conversa, *ATE_O_PEDIDO)
    conversa.responder("cancelar")

    assert "Qual pedido?" in "\n".join(conversa.responder("2"))  # 2 pedidos: a lista


def test_m2_um_pedido_so_pergunta_e_esse(conversa, api, fila):
    venda = semear(api)
    resposta = falar(conversa, "quero cancelar")

    assert "Encontrei seu pedido" in resposta and f"Pedido {venda}" in resposta
    assert "É esse? 1) sim 2) não" in resposta and "Qual pedido?" not in resposta


def test_m2_um_pedido_nao_e_esse_segue_sem_pedido(conversa, api, fila):
    semear(api)
    falar(conversa, "quero cancelar", "2", "1", "não", "1")

    [s] = abertas(fila)
    assert s.dados["pedido_id"] is None


def test_m2_dois_pedidos_mostram_a_lista(conversa, api):
    semear(api, dias_atras=1)
    semear(api, dias_atras=2)

    assert "Qual pedido?" in falar(conversa, "quero cancelar")


def test_m2_modo_real_so_pedidos_do_chatbot(dependencias, api, fila):
    do_chatbot = semear(api, cliente=5700355, observacao="DEMONSTRAÇÃO DO CHATBOT - NÃO ENTREGAR. [chatbot:abc]")
    semear(api, cliente=5700355, observacao="venda feita no painel", dias_atras=2)
    real = replace(dependencias, devolucao=Devolucao(api=dependencias.api, relogio=api.relogio, fila=fila,
                                                      cliente_fixo=5700355))
    resposta = falar(Conversa("5586999990000", real, numero_confiavel=False), "quero cancelar")

    assert f"Pedido {do_chatbot}" in resposta and "É esse?" in resposta  # o do painel não aparece


@pytest.mark.parametrize("como", [{"status": "CONFIRM"}, {"entregue_em": "2026-11-17T16:00:00-03:00"}])
def test_m3_pedido_entregue_oferece_as_opcoes(conversa, api, fila, como):
    semear(api, **como)
    resposta = falar(conversa, "quero cancelar", "1")

    assert "Esse pedido já foi entregue." in resposta
    assert "1) problema com o produto" in resposta and "2) devolução do valor" in resposta
    assert "3) falar com um atendente" in resposta
    assert abertas(fila) == []


def test_m3_entregue_problema_com_o_produto(conversa, api, fila):
    venda = semear(api, status="CONFIRM")
    falar(conversa, "quero cancelar", "1", "1")
    conversa.responder("veio sem lacre")
    conversa.responder("1")

    [s] = abertas(fila)
    assert s.tipo == "PROBLEMA_PRODUTO" and s.dados["pedido_id"] == venda


def test_m3_entregue_devolucao_do_valor(conversa, api, fila):
    semear(api, status="CONFIRM")
    resposta = falar(conversa, "quero cancelar", "1", "2")

    assert "Qual o motivo?" in resposta


def test_m3_entregue_atendente(conversa, api, fila):
    semear(api, status="CONFIRM")
    resposta = falar(conversa, "quero cancelar", "1", "3")

    [s] = abertas(fila)
    assert s.tipo == "ATENDIMENTO" and "ATD-" in resposta

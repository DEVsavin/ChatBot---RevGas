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
CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
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


def semear(api, *, dias_atras=1, quantidade=2, status=None, cliente=77):
    venda = api.semear_venda(
        {"establishment_id": 2580, "customer_id": cliente, "channel": "WHATSAPP",
         "products": [{"product_id": 26717, "quantity": quantidade, "value": 110.0,
                       "product_description": "botijão de gás"}],
         "payments": [{"payment_type": "PIX", "value": 110.0 * quantidade}]},
        criada_em=AGORA - timedelta(days=dias_atras),
    )
    if status:
        next(v for v in api.vendas if v["id"] == venda["id"])["status"] = status
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
    assert "nada tinha sido enviado" in resposta and "Qual pedido?" in resposta


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

    assert f"1) Pedido {venda['id']}" in resposta


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
    resposta = falar(conversa, "quero cancelar", "7")

    assert "Escolha um número da lista." in resposta and "Qual pedido?" in resposta


def test_nao_achei_meu_pedido_segue_sem_pedido(conversa, api, fila):
    semear(api)
    falar(conversa, "quero cancelar", "0", "1", "não", "1")

    [s] = abertas(fila)
    assert s.dados["pedido_id"] is None


def test_pedido_cancelado_vira_estorno(conversa, api, fila):
    semear(api, status="CANCEL")
    resposta = falar(conversa, "quero cancelar", "1")

    assert "já está cancelado" in resposta and "você prefere" in resposta
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
    venda = semear(api, cliente=5700355)
    real = replace(dependencias, devolucao=Devolucao(api=dependencias.api, relogio=api.relogio, fila=fila,
                                                      cliente_fixo=5700355))
    conversa = Conversa("5586999990000", real, numero_confiavel=False)  # como no --venda-real
    resposta = falar(conversa, "quero cancelar")

    assert f"1) Pedido {venda}" in resposta
    assert "buscar_clientes" not in api.operacoes

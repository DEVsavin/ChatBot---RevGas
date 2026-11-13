"""Testes do M4: o programa da venda de teste (rodado pelo usuário no terminal dele).

Seam combinado: venda_de_teste.rodar(argv, api=..., ...) com a API falsa e as
respostas do usuário simuladas. Sem rede. O telefone é fictício (DDD 00).
"""


import pytest

from revgas_chatbot.api_revgas.api import Resposta
from revgas_chatbot.api_revgas.api_falsa import TIMEOUT_DEPOIS_DE_CRIAR, ApiRevGasFalsa, DepoisDeCriar
from revgas_chatbot.vendas.venda_de_teste import rodar

TELEFONE = "(00) 90000-1234"
COM_55 = "5500900001234"
FORMAS_DO_TELEFONE = (COM_55, "00900001234", "550000001234", "0000001234", "90000-1234", "900001234")


class ApiDeTeste(ApiRevGasFalsa):
    """A API falsa + o cancelamento (só o adaptador HTTP real tem)."""

    def __init__(self):
        super().__init__()
        self.cancelamentos: list[int] = []

    def cancelar_venda(self, venda_id):
        self.requisicoes.append(self.relogio.agora())
        self.operacoes.append("cancelar_venda")
        self.cancelamentos.append(venda_id)
        return Resposta(200, {"id": venda_id, "status": "CANCEL"})


def executar(argv, respostas, *, api=None, telefone=TELEFONE):
    """Roda o programa; `respostas` são as linhas que o usuário digita, na ordem."""
    api = api or ApiDeTeste()
    fila = list(respostas)
    perguntas, saida = [], []

    def perguntar(texto):
        perguntas.append(texto)
        return fila.pop(0)

    rodar(argv, api=api, relogio=api.relogio, perguntar=perguntar,
          ler_segredo=lambda texto: telefone, escrever=saida.append)
    return api, "\n".join(saida), perguntas


def test_recusar_o_cadastro_nao_envia_nada():
    api, saida, perguntas = executar(["--broadcast", "NONE"], ["não"])

    assert "POST /customers" in perguntas[0]
    assert "cadastrar_cliente" not in api.operacoes
    assert "criar_venda" not in api.operacoes
    assert "Nada foi enviado" in saida


def test_recusar_a_venda_nao_cria_venda():
    api, saida, _ = executar(["--broadcast", "NONE"], ["sim", "não"])

    assert "cadastrar_cliente" in api.operacoes
    assert api.vendas_criadas() == []
    assert "Nada foi enviado" in saida


def test_aceitando_cria_uma_venda_no_formato_do_mvp():
    api, saida, perguntas = executar(["--broadcast", "NONE"], ["sim", "sim", "não"])

    [venda] = api.vendas_criadas()
    assert [(p["product_id"], p["quantity"]) for p in venda["products"]] == [(26717, 2)]  # 1 linha (M4)
    assert venda["payments"] == [{"payment_type": "CARTAO_DE_CREDITO", "value": 220.0}]
    assert "TESTE" in venda["observation"] and "[chatbot:" in venda["observation"]
    assert f"Venda criada: id {venda['id']}" in saida
    assert "status ACTIVE" in saida
    assert "PUT /sales/" in perguntas[-1]  # oferece o cancelamento, que também pede OK
    assert api.cancelamentos == []


def test_a_venda_de_teste_sempre_vai_com_broadcast_none():
    # D11: só NONE foi autorizado (omitido pode avisar entregador real, D5).
    api, _, _ = executar(["--broadcast", "NONE"], ["sim", "sim", "não"])

    assert api.vendas_criadas()[0]["broadcast"] == "NONE"


def test_broadcast_omitido_e_recusado_na_linha_de_comando():
    with pytest.raises(SystemExit):
        executar(["--broadcast", "omitir"], [])
    with pytest.raises(SystemExit):
        executar(["--broadcast", "NEW_SALE"], [])


def test_se_a_api_recusar_a_venda_avisa_para_nao_tentar_sem_o_broadcast():
    api = ApiDeTeste()
    api.roteirizar("criar_venda", Resposta(422, {"message": "broadcast inválido"}))
    _, saida, _ = executar(["--broadcast", "NONE"], ["sim", "sim"], api=api)

    assert "não rode de novo sem o broadcast" in saida


def test_nunca_mais_de_uma_requisicao_por_segundo_na_execucao_inteira():
    # Contagem antes, cadastro, venda, contagem depois e cancelamento: todos espaçados.
    api, _, _ = executar(["--broadcast", "NONE"], ["sim", "sim", "sim"])

    assert "cancelar_venda" in api.operacoes
    horarios = api.requisicoes
    assert len(horarios) == len(api.operacoes)
    assert all((b - a).total_seconds() >= 1 for a, b in zip(horarios, horarios[1:]))


def test_broadcast_e_obrigatorio_na_linha_de_comando():
    with pytest.raises(SystemExit):
        executar([], [])


def test_o_telefone_nunca_aparece_na_saida_nem_nas_perguntas():
    api, saida, perguntas = executar(["--broadcast", "NONE"], ["sim", "sim", "sim"])

    tudo = saida + "\n".join(perguntas)
    for forma in FORMAS_DO_TELEFONE:
        assert forma not in tudo


def test_cancelamento_aceito():
    api, saida, _ = executar(["--broadcast", "NONE"], ["sim", "sim", "sim"])

    [venda] = api.vendas_criadas()
    assert api.cancelamentos == [venda["id"]]
    assert "Cancelamento: HTTP 200" in saida


def test_cliente_ja_cadastrado_nao_e_recadastrado():
    api = ApiDeTeste()
    api.clientes.append({"id": 77, "phones": [COM_55]})
    api, saida, perguntas = executar(["--broadcast", "NONE"], ["sim", "não"], api=api)

    assert "cadastrar_cliente" not in api.operacoes
    assert api.vendas_criadas()[0]["customer_id"] == 77


def test_cliente_achado_so_em_outro_formato_para_sem_enviar():
    # D8: se o cadastro guarda o telefone sem o 55, um cadastro novo duplicaria o cliente.
    api = ApiDeTeste()
    api.clientes.append({"id": 77, "phones": ["00900001234"]})
    api, saida, perguntas = executar(["--broadcast", "NONE"], [], api=api)

    assert perguntas == []
    assert set(api.operacoes) == {"buscar_clientes"}
    assert "outro formato" in saida


def test_mostra_quantos_clientes_cada_formato_do_telefone_acha():
    api, saida, _ = executar(["--broadcast", "NONE"], ["sim", "sim", "não"])

    assert "com 55: 0" in saida and "sem 55: 0" in saida  # antes
    assert "com 55: 1" in saida  # depois do cadastro


def test_telefone_invalido_para_sem_chamar_a_api():
    api, saida, _ = executar(["--broadcast", "NONE"], [], telefone="123")

    assert api.operacoes == []
    assert "Telefone inválido" in saida


def test_erro_da_api_mostra_o_status_e_a_mensagem_sem_numeros_longos():
    api = ApiDeTeste()
    api.roteirizar("criar_venda", Resposta(422, {"message": f"card_brand obrigatório ({COM_55})"}))
    api, saida, _ = executar(["--broadcast", "NONE"], ["sim", "sim"], api=api)

    assert "POST /sales: HTTP 422" in saida
    assert "card_brand obrigatório" in saida
    assert COM_55 not in saida


def test_cancelar_uma_venda_depois():
    api, saida, perguntas = executar(["--cancelar", "30"], ["sim"])

    assert "PUT /sales/30/cancel" in perguntas[0]
    assert api.cancelamentos == [30]
    assert api.operacoes == ["cancelar_venda"]


def test_cancelar_recusado_nao_envia():
    api, _, _ = executar(["--cancelar", "30"], ["não"])

    assert api.operacoes == []


def test_a_pergunta_da_venda_mostra_o_total_e_o_broadcast():
    _, _, perguntas = executar(["--broadcast", "NONE"], ["sim", "não"])

    assert "POST /sales" in perguntas[-1]
    assert "R$ 220,00" in perguntas[-1]  # 2 x R$ 110,00 do catálogo
    assert "broadcast NONE" in perguntas[-1]


# --- D12: venda sem cliente (GET /customers dá 403 para a nossa chave) -------------------


def sem_cliente(respostas, *, api=None):
    """Roda --sem-cliente; o telefone nunca pode ser pedido."""

    def nao_pede_telefone(texto):
        raise AssertionError("o modo sem cliente não pede telefone")

    api = api or ApiDeTeste()
    fila, perguntas, saida = list(respostas), [], []

    def perguntar(texto):
        perguntas.append(texto)
        return fila.pop(0)

    rodar(["--sem-cliente"], api=api, relogio=api.relogio, perguntar=perguntar,
          ler_segredo=nao_pede_telefone, escrever=saida.append)
    return api, "\n".join(saida), perguntas


def test_sem_cliente_cria_a_venda_sem_customer_id_e_sem_buscar_cliente():
    api, saida, perguntas = sem_cliente(["sim", "não"])

    assert api.operacoes == ["criar_venda"]
    [venda] = api.vendas_criadas()
    assert "customer_id" not in venda
    assert venda["broadcast"] == "NONE"
    # 2º envio (decisão do usuário, 12/11): 1 linha com quantity 2 a 110 (Dv5: 201 = value unitário).
    assert venda["products"] == [{"product_id": 26717, "quantity": 2, "value": 110.0}]
    assert venda["payments"] == [{"payment_type": "CARTAO_DE_CREDITO", "value": 220.0}]
    assert "TESTE" in venda["observation"] and "[chatbot:" in venda["observation"]
    assert venda["landmark"] == "VENDA DE TESTE - NAO ENTREGAR"
    assert "POST /sales" in perguntas[0] and "sem cliente" in perguntas[0]
    assert "26717 x 2 a R$ 110,00" in perguntas[0] and "pagamento R$ 220,00" in perguntas[0]
    # 4º envio (decisão do usuário, 12/11): ponto genérico do centro de Teresina, como NÚMERO
    # (nas vendas reais lat/lon vêm como número; como texto, o 3º envio deu o mesmo 400).
    assert (venda["lat"], venda["lon"]) == (-5.0892, -42.8016)
    assert all(isinstance(venda[c], float) for c in ("lat", "lon"))
    assert "lat/lon -5.0892, -42.8016" in perguntas[0]
    assert f"Venda criada: id {venda['id']}, status ACTIVE" in saida
    assert "PUT /sales/" in perguntas[1]


def test_sem_cliente_recusado_nao_envia_nada():
    api, saida, _ = sem_cliente(["não"])

    assert api.operacoes == []
    assert "Nada foi enviado" in saida


def test_sem_cliente_cancelamento_aceito():
    api, saida, _ = sem_cliente(["sim", "sim"])

    assert api.cancelamentos == [api.vendas_criadas()[0]["id"]]
    assert "Cancelamento: HTTP 200" in saida


@pytest.mark.parametrize("falha", [TIMEOUT_DEPOIS_DE_CRIAR, DepoisDeCriar(Resposta(504, None)), Resposta(503, None)])
def test_sem_cliente_resultado_incerto_nao_reenvia_e_mostra_a_marca(falha):
    api = ApiDeTeste()
    api.roteirizar("criar_venda", falha)
    api, saida, perguntas = sem_cliente(["sim"], api=api)

    assert api.operacoes == ["criar_venda"]  # nunca um segundo POST
    assert "Não reenvie" in saida
    assert "[chatbot:" in saida
    assert len(perguntas) == 1  # sem oferta de cancelamento: não há id


def test_sem_cliente_erro_4xx_mostra_a_mensagem_sem_numeros_longos():
    api = ApiDeTeste()
    api.roteirizar("criar_venda", Resposta(422, {"message": f"customer_id obrigatório ({COM_55})"}))
    api, saida, perguntas = sem_cliente(["sim"], api=api)

    assert "POST /sales: HTTP 422" in saida
    assert "customer_id obrigatório" in saida
    assert COM_55 not in saida
    assert len(perguntas) == 1


def test_sem_cliente_e_um_modo_proprio():
    with pytest.raises(SystemExit):
        executar(["--sem-cliente", "--broadcast", "NONE"], [])


# --- D13: 5º envio, com o customer_id de um cliente do próprio usuário --------------------


def com_cliente(respostas, *, api=None, customer_id="5700355"):
    def nao_pede_telefone(texto):
        raise AssertionError("o modo --customer-id não pede telefone")

    api = api or ApiDeTeste()
    fila, perguntas, saida = list(respostas), [], []

    def perguntar(texto):
        perguntas.append(texto)
        return fila.pop(0)

    rodar(["--customer-id", customer_id], api=api, relogio=api.relogio, perguntar=perguntar,
          ler_segredo=nao_pede_telefone, escrever=saida.append)
    return api, "\n".join(saida), perguntas


def test_customer_id_manda_o_corpo_do_4o_envio_mais_o_cliente():
    api, saida, perguntas = com_cliente(["sim", "não"])

    assert api.operacoes == ["criar_venda"]  # nada de /customers
    [venda] = api.vendas_criadas()
    assert venda["customer_id"] == 5700355
    assert venda["products"] == [{"product_id": 26717, "quantity": 2, "value": 110.0}]
    assert venda["payments"] == [{"payment_type": "CARTAO_DE_CREDITO", "value": 220.0}]
    assert (venda["lat"], venda["lon"]) == (-5.0892, -42.8016)
    assert venda["broadcast"] == "NONE"
    assert venda["state_federation"] == "Piauí"
    assert venda["landmark"] == "VENDA DE TESTE - NAO ENTREGAR"
    assert "TESTE" in venda["observation"] and "[chatbot:" in venda["observation"]
    assert "POST /sales, cliente 5700355" in perguntas[0]
    assert f"Venda criada: id {venda['id']}, status ACTIVE" in saida
    assert "PUT /sales/" in perguntas[1]


def test_customer_id_recusado_nao_envia_nada():
    api, saida, _ = com_cliente(["não"])

    assert api.operacoes == []
    assert "Nada foi enviado" in saida


def test_customer_id_cancelamento_aceito():
    api, saida, _ = com_cliente(["sim", "sim"])

    assert api.cancelamentos == [api.vendas_criadas()[0]["id"]]
    assert "Cancelamento: HTTP 200" in saida


@pytest.mark.parametrize("falha", [TIMEOUT_DEPOIS_DE_CRIAR, DepoisDeCriar(Resposta(504, None)), Resposta(503, None)])
def test_customer_id_resultado_incerto_nao_reenvia(falha):
    api = ApiDeTeste()
    api.roteirizar("criar_venda", falha)
    api, saida, perguntas = com_cliente(["sim"], api=api)

    assert api.operacoes == ["criar_venda"]
    assert "Não reenvie" in saida and "[chatbot:" in saida
    assert len(perguntas) == 1


def test_customer_id_erro_4xx_mostra_a_mensagem():
    api = ApiDeTeste()
    api.roteirizar("criar_venda", Resposta(400, {"message": "Dados da Entrega da Venda estão incompletos!"}))
    api, saida, perguntas = com_cliente(["sim"], api=api)

    assert "POST /sales: HTTP 400" in saida and "Dados da Entrega" in saida
    assert len(perguntas) == 1


@pytest.mark.parametrize("valor", ["0", "-1", "abc"])
def test_customer_id_invalido_e_recusado(valor):
    with pytest.raises(SystemExit):
        com_cliente([], customer_id=valor)


def test_customer_id_e_um_modo_proprio():
    with pytest.raises(SystemExit):
        executar(["--customer-id", "5700355", "--sem-cliente"], [])


# --- D14: 6º envio, avisando os entregadores (só com --customer-id) ------------------------


def test_avisar_entregadores_manda_new_sale_e_avisa_na_pergunta():
    api = ApiDeTeste()
    fila, perguntas, saida = ["sim", "sim"], [], []
    rodar(["--customer-id", "5700355", "--avisar-entregadores"], api=api, relogio=api.relogio,
          perguntar=lambda t: (perguntas.append(t), fila.pop(0))[1],
          ler_segredo=lambda t: pytest.fail("não pede telefone"), escrever=saida.append)

    [venda] = api.vendas_criadas()
    assert venda["broadcast"] == "NEW_SALE"
    assert venda["customer_id"] == 5700355
    assert "deliverer_id" not in venda
    assert "broadcast NEW_SALE" in perguntas[0]
    assert "os entregadores do estabelecimento 2580 serão avisados" in perguntas[0]
    assert "PUT /sales/" in perguntas[1]  # cancelamento oferecido logo depois do 201
    assert api.cancelamentos == [venda["id"]]


def test_sem_a_flag_continua_none_e_sem_aviso():
    api, _, perguntas = com_cliente(["sim", "não"])

    assert api.vendas_criadas()[0]["broadcast"] == "NONE"
    assert "serão avisados" not in perguntas[0]
    assert "deliverer_id" not in api.vendas_criadas()[0]


@pytest.mark.parametrize("argv", [
    ["--avisar-entregadores"],
    ["--sem-cliente", "--avisar-entregadores"],
    ["--broadcast", "NONE", "--avisar-entregadores"],
    ["--cancelar", "30", "--avisar-entregadores"],
])
def test_avisar_entregadores_so_vale_junto_de_customer_id(argv):
    with pytest.raises(SystemExit):
        executar(argv, [])


# --- D15: 7º envio, com o endereço digitado na hora (nos testes, inventado) ---------------

RUA, NUMERO, COMPLEMENTO, BAIRRO = "Rua Inventada do Teste", "123", "Bloco Z", "Bairro Inventado"


def endereco_real(respostas, *, argv=("--customer-id", "5700355", "--endereco-real"), api=None):
    api = api or ApiDeTeste()
    fila, perguntas, saida = list(respostas), [], []

    def perguntar(texto):
        perguntas.append(texto)
        return fila.pop(0)

    rodar(list(argv), api=api, relogio=api.relogio, perguntar=perguntar,
          ler_segredo=lambda t: pytest.fail("não pede telefone"), escrever=saida.append)
    return api, "\n".join(saida), perguntas


def test_endereco_real_vai_no_corpo_e_o_resto_e_igual_ao_5o():
    api, saida, perguntas = endereco_real([RUA, NUMERO, COMPLEMENTO, BAIRRO, "sim", "sim"])

    [venda] = api.vendas_criadas()
    assert (venda["location"], venda["number"], venda["complement"], venda["neighborhood"]) == (
        RUA, NUMERO, COMPLEMENTO, BAIRRO)
    assert (venda["city"], venda["state_federation"]) == ("Teresina", "Piauí")
    assert venda["customer_id"] == 5700355
    assert venda["broadcast"] == "NONE"
    assert venda["products"] == [{"product_id": 26717, "quantity": 2, "value": 110.0}]
    assert "TESTE" in venda["observation"] and "[chatbot:" in venda["observation"]
    assert "deliverer_id" not in venda
    assert "endereço que você digitou" in perguntas[4]
    assert api.cancelamentos == [venda["id"]]  # cancelamento logo após o 201


def test_o_endereco_nunca_aparece_na_saida_nem_nas_perguntas():
    _, saida, perguntas = endereco_real([RUA, NUMERO, COMPLEMENTO, BAIRRO, "sim", "não"])

    tudo = saida + "\n".join(perguntas)
    for parte in (RUA, COMPLEMENTO, BAIRRO):
        assert parte not in tudo


def test_endereco_real_sem_complemento():
    api, _, _ = endereco_real([RUA, NUMERO, "", BAIRRO, "sim", "não"])

    assert "complement" not in api.vendas_criadas()[0]


@pytest.mark.parametrize("respostas", [["", NUMERO, "", BAIRRO], [RUA, " ", "", BAIRRO], [RUA, NUMERO, "", ""]])
def test_endereco_incompleto_nao_envia(respostas):
    api, saida, _ = endereco_real(respostas)

    assert api.operacoes == []
    assert "Endereço incompleto. Nada foi enviado." in saida


def test_endereco_real_recusado_nao_envia():
    api, saida, _ = endereco_real([RUA, NUMERO, "", BAIRRO, "não"])

    assert api.operacoes == []
    assert "Nada foi enviado" in saida


def test_erro_da_api_nao_mostra_o_endereco():
    api = ApiDeTeste()
    api.roteirizar("criar_venda", Resposta(400, {"message": f"Dados da Entrega incompletos: {RUA}, {BAIRRO}"}))
    _, saida, _ = endereco_real([RUA, NUMERO, "", BAIRRO, "sim"], api=api)

    assert "HTTP 400" in saida
    assert RUA not in saida and BAIRRO not in saida


@pytest.mark.parametrize("argv", [
    ["--endereco-real"],
    ["--sem-cliente", "--endereco-real"],
    ["--customer-id", "5700355", "--endereco-real", "--avisar-entregadores"],
])
def test_endereco_real_so_junto_de_customer_id_e_sem_avisar(argv):
    with pytest.raises(SystemExit):
        executar(argv, [])


# --- M5 (10.6): cancelar as vendas da demonstração ----------------------------------------

AMBIENTE_DEMO = {"REVGAS_CLIENTE_DEMO": "5700355"}


def semear(api, *, cliente=5700355, marca="[chatbot:demo-1]", status=None):
    venda = api.semear_venda({
        "customer_id": cliente, "channel": "WHATSAPP", "location": "Rua Secreta da Demo",
        "products": [{"product_id": 26717, "quantity": 1, "value": 110.0}],
        "observation": f"DEMONSTRAÇÃO DO CHATBOT - NÃO ENTREGAR. {marca}".strip(),
    })
    if status:
        next(v for v in api.vendas if v["id"] == venda["id"])["status"] = status
    return venda["id"]


def cancelar_demo(respostas, *, api, ambiente=AMBIENTE_DEMO):
    fila, perguntas, saida = list(respostas), [], []

    def perguntar(texto):
        perguntas.append(texto)
        return fila.pop(0)

    rodar(["--cancelar-demo"], api=api, relogio=api.relogio, perguntar=perguntar,
          ler_segredo=lambda t: pytest.fail("não pede telefone"), escrever=saida.append, ambiente=ambiente)
    return "\n".join(saida), perguntas


def test_cancelar_demo_so_oferece_vendas_abertas_do_nosso_cliente_com_a_marca():
    api = ApiDeTeste()
    api.ignora_filtros_de_vendas = True  # mesmo se a API ignorar os filtros, só as nossas entram
    a = semear(api, marca="[chatbot:a]")
    b = semear(api, marca="[chatbot:b]")
    semear(api, marca="[chatbot:c]", status="CANCEL")  # já cancelada
    semear(api, cliente=999, marca="[chatbot:d]")  # de outro cliente
    semear(api, marca="")  # do nosso cliente, mas sem a marca (feita no painel)
    saida, perguntas = cancelar_demo(["sim", "sim"], api=api)

    assert sorted(api.cancelamentos) == sorted([a, b])
    assert len(perguntas) == 2 and all("PUT /sales/" in p for p in perguntas)
    assert "2 venda(s) da demonstração" in saida
    assert "Rua Secreta" not in saida + "\n".join(perguntas)


def test_cancelar_demo_recusar_nao_cancela():
    api = ApiDeTeste()
    semear(api)
    _, _ = cancelar_demo(["não"], api=api)

    assert api.cancelamentos == []


def test_cancelar_demo_sem_vendas_abertas():
    api = ApiDeTeste()
    saida, perguntas = cancelar_demo([], api=api)

    assert "Nenhuma venda da demonstração aberta." in saida
    assert perguntas == []


def test_cancelar_demo_respeita_1_requisicao_por_segundo():
    api = ApiDeTeste()
    for i in range(3):
        semear(api, marca=f"[chatbot:{i}]")
    cancelar_demo(["sim", "sim", "sim"], api=api)

    horarios = api.requisicoes
    assert len(api.cancelamentos) == 3
    assert all((b - a).total_seconds() >= 1 for a, b in zip(horarios, horarios[1:]))


def test_cancelar_demo_sem_cliente_demo_nao_chama_a_api():
    api = ApiDeTeste()
    with pytest.raises(SystemExit) as erro:
        cancelar_demo([], api=api, ambiente={})

    assert "REVGAS_CLIENTE_DEMO" in str(erro.value)
    assert api.operacoes == []

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
    assert [(p["product_id"], p["quantity"]) for p in venda["products"]] == [(26717, 1), (26717, 1)]
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

"""Testes do D5: Devolucao.solicitar(...) abre a solicitação na fila.

Seam combinado: solicitar. O que foi para a fila é conferido por
listar_abertas (interface pública da fila). Dados fictícios.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa, RelogioFalso
from revgas_chatbot.devolucao.devolucao import (
    Devolucao,
    PedidoDoCliente,
    PedidosEncontrados,
    SolicitacaoAberta,
    SolicitacaoInvalida,
)
from revgas_chatbot.atendimento.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA

TELEFONE = "5586999990000"
AGORA = datetime(2026, 11, 11, 15, 0, tzinfo=FUSO_TERESINA)

PEDIDO = PedidoDoCliente(
    id=1001,
    data=date(2026, 11, 10),
    itens=(("GLP 13KG", 1),),
    total=Decimal("110.00"),
    status="ACTIVE",
)
PEDIDO_CANCELADO = PedidoDoCliente(
    id=1002,
    data=date(2026, 11, 10),
    itens=(("GLP 13KG", 1),),
    total=Decimal("110.00"),
    status="CANCEL",
)

# O que pedidos_do_cliente devolveu: o cliente escolhe pelo número (1, 2...).
ENCONTRADOS = PedidosEncontrados(customer_id=77, nome="Cliente Teste", pedidos=(PEDIDO, PEDIDO_CANCELADO))
DO_PEDIDO = {"encontrados": ENCONTRADOS, "escolha": 1}
DO_CANCELADO = {"encontrados": ENCONTRADOS, "escolha": 2}
SEM_PEDIDO = {"encontrados": ENCONTRADOS, "escolha": None}  # "não achei na lista"


@pytest.fixture
def api():
    return ApiRevGasFalsa(relogio=RelogioFalso(AGORA))


@pytest.fixture
def fila(tmp_path, api):
    return FilaDeAtendimento(tmp_path, api.relogio)


@pytest.fixture
def devolucao(api, fila):
    return Devolucao(api=ApiCadenciada(api, api.relogio), relogio=api.relogio, fila=fila)


def test_cancelamento_abre_solicitacao_com_protocolo(devolucao, fila):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DESISTI")

    assert resultado == SolicitacaoAberta(
        protocolo="DEV-20261111-1",
        mensagem_para_cliente=(
            "Sua solicitação foi registrada com o protocolo DEV-20261111-1. "
            "Uma pessoa da nossa equipe vai continuar o atendimento."
        ),
    )
    [aberta] = fila.listar_abertas()
    assert aberta.tipo == "CANCELAMENTO"
    assert aberta.telefone == TELEFONE
    assert "Pedido 1001" in aberta.resumo
    assert "status na API: ACTIVE" in aberta.resumo
    assert "Motivo: desisti" in aberta.resumo


# --- motivos por tipo -------------------------------------------------------------


@pytest.mark.parametrize(
    "tipo, motivo, preferencia",
    [
        ("CANCELAMENTO", "DESISTI", None),
        ("CANCELAMENTO", "DEMOROU", None),
        ("CANCELAMENTO", "PEDI_ERRADO", None),
        ("ESTORNO", "PEDIDO_CANCELADO_JA_PAGO", "DINHEIRO_DE_VOLTA"),
        ("ESTORNO", "PRODUTO_COM_PROBLEMA", "CREDITO_PROXIMA_COMPRA"),
        ("ESTORNO", "COBRANCA_ERRADA", "DINHEIRO_DE_VOLTA"),
    ],
)
def test_motivos_da_lista_curta_sao_aceitos(devolucao, tipo, motivo, preferencia):
    resultado = devolucao.solicitar(tipo, TELEFONE, **DO_PEDIDO, motivo=motivo, preferencia=preferencia)

    assert isinstance(resultado, SolicitacaoAberta)


@pytest.mark.parametrize(
    "tipo, motivo",
    [
        ("CANCELAMENTO", "COBRANCA_ERRADA"),  # motivo de estorno
        ("ESTORNO", "DESISTI"),  # motivo de cancelamento
        ("PROBLEMA_PRODUTO", "PRODUTO_ERRADO"),  # só texto livre
        ("ALTERACAO", "DESISTI"),
        ("VASILHAME", "INVENTADO"),
    ],
)
def test_motivo_que_nao_e_do_tipo_e_invalido_e_nada_vai_para_a_fila(devolucao, fila, tipo, motivo):
    resultado = devolucao.solicitar(
        tipo, TELEFONE, **DO_PEDIDO, motivo=motivo, preferencia="DINHEIRO_DE_VOLTA" if tipo == "ESTORNO" else None
    )

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "motivo" in [p.campo for p in resultado.problemas]
    assert fila.listar_abertas() == []


@pytest.mark.parametrize("tipo", ["PROBLEMA_PRODUTO", "ALTERACAO", "VASILHAME"])
def test_tipos_so_de_texto_aceitam_outro_com_texto(devolucao, fila, tipo):
    resultado = devolucao.solicitar(tipo, TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="veio sem lacre")

    assert isinstance(resultado, SolicitacaoAberta)
    assert fila.listar_abertas()[0].texto_cliente == "veio sem lacre"


@pytest.mark.parametrize("texto", [None, "", "   ", "a" * 201])
def test_outro_exige_texto_de_ate_200_caracteres(devolucao, fila, texto):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto=texto)

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "texto" in [p.campo for p in resultado.problemas]
    assert fila.listar_abertas() == []


def test_outro_com_200_caracteres_e_aceito(devolucao):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="a" * 200)

    assert isinstance(resultado, SolicitacaoAberta)


@pytest.mark.parametrize("tipo", ["VENDA", "COMPROVANTE_PIX", "VAZAMENTO", "QUALQUER"])
def test_tipo_que_nao_e_solicitacao_de_devolucao_e_erro_de_programacao(devolucao, tipo):
    with pytest.raises(ValueError):
        devolucao.solicitar(tipo, TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="x")


# --- preferência, pedido cancelado, sem pedido --------------------------------------


def test_estorno_sem_preferencia_e_invalido(devolucao, fila):
    resultado = devolucao.solicitar("ESTORNO", TELEFONE, **DO_PEDIDO, motivo="COBRANCA_ERRADA")

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "preferencia" in [p.campo for p in resultado.problemas]
    assert fila.listar_abertas() == []


def test_cancelamento_aceita_preferencia_e_ela_vai_no_resumo(devolucao, fila):
    devolucao.solicitar(
        "CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DEMOROU", preferencia="CREDITO_PROXIMA_COMPRA"
    )

    assert "Preferência, se houver valor a devolver: crédito na próxima compra" in fila.listar_abertas()[0].resumo


def test_estorno_traz_a_preferencia_no_resumo(devolucao, fila):
    devolucao.solicitar("ESTORNO", TELEFONE, **DO_PEDIDO, motivo="COBRANCA_ERRADA", preferencia="DINHEIRO_DE_VOLTA")

    assert "Preferência: dinheiro de volta" in fila.listar_abertas()[0].resumo


@pytest.mark.parametrize("tipo", ["PROBLEMA_PRODUTO", "ALTERACAO", "VASILHAME"])
def test_preferencia_em_tipo_que_nao_e_cancelamento_nem_estorno_e_invalida(devolucao, tipo):
    resultado = devolucao.solicitar(
        tipo, TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="x", preferencia="DINHEIRO_DE_VOLTA"
    )

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "preferencia" in [p.campo for p in resultado.problemas]


def test_preferencia_desconhecida_e_invalida(devolucao):
    resultado = devolucao.solicitar("ESTORNO", TELEFONE, **DO_PEDIDO, motivo="COBRANCA_ERRADA", preferencia="PIX")

    assert isinstance(resultado, SolicitacaoInvalida)


def test_cancelar_pedido_ja_cancelado_e_invalido(devolucao, fila):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_CANCELADO, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "tipo" in [p.campo for p in resultado.problemas]
    assert fila.listar_abertas() == []


def test_estorno_de_pedido_cancelado_e_aceito(devolucao):
    resultado = devolucao.solicitar(
        "ESTORNO", TELEFONE, **DO_CANCELADO, motivo="PEDIDO_CANCELADO_JA_PAGO", preferencia="DINHEIRO_DE_VOLTA"
    )

    assert isinstance(resultado, SolicitacaoAberta)


def test_sem_pedido_escolhido_e_aceito_e_o_resumo_diz_isso(devolucao, fila):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **SEM_PEDIDO, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoAberta)
    assert "O cliente não encontrou o pedido na lista." in fila.listar_abertas()[0].resumo


def test_solicitar_nao_chama_a_api(devolucao, api):
    devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DESISTI")
    devolucao.solicitar("ESTORNO", TELEFONE, **SEM_PEDIDO, motivo="OUTRO", texto="x", preferencia="DINHEIRO_DE_VOLTA")

    assert api.operacoes == []


def test_numero_longo_no_texto_do_cliente_chega_mascarado(devolucao, fila):
    devolucao.solicitar(
        "ESTORNO", TELEFONE, **DO_PEDIDO, motivo="OUTRO",
        texto="meu cpf é 123.456.789-01", preferencia="DINHEIRO_DE_VOLTA",
    )

    [aberta] = fila.listar_abertas()
    assert "[número omitido]" in aberta.texto_cliente
    assert "789" not in aberta.texto_cliente + aberta.resumo


@pytest.mark.parametrize("telefone", ["", "   "])
def test_telefone_e_obrigatorio(devolucao, fila, telefone):
    resultado = devolucao.solicitar("CANCELAMENTO", telefone, **DO_PEDIDO, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoInvalida)
    assert "telefone" in [p.campo for p in resultado.problemas]
    assert fila.listar_abertas() == []


# --- revisão do D5 ----------------------------------------------------------------


def test_dados_estruturados_vao_para_a_fila_sem_o_texto_do_cliente(devolucao, fila):
    devolucao.solicitar(
        "ESTORNO", TELEFONE, **DO_PEDIDO, motivo="OUTRO", texto="cobraram duas vezes",
        preferencia="DINHEIRO_DE_VOLTA",
    )

    [aberta] = fila.listar_abertas()
    assert aberta.dados == {
        "pedido_id": 1001,
        "customer_id": 77,
        "motivo": "OUTRO",
        "preferencia": "DINHEIRO_DE_VOLTA",
    }
    assert aberta.nome == "Cliente Teste"


def test_sem_pedido_escolhido_os_dados_trazem_pedido_id_nulo(devolucao, fila):
    devolucao.solicitar("CANCELAMENTO", TELEFONE, **SEM_PEDIDO, motivo="DESISTI")

    assert fila.listar_abertas()[0].dados["pedido_id"] is None


def test_cliente_nao_encontrado_ainda_pode_abrir_solicitacao(devolucao, fila):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, encontrados=None, escolha=None, motivo="DESISTI")

    assert isinstance(resultado, SolicitacaoAberta)
    [aberta] = fila.listar_abertas()
    assert aberta.dados["customer_id"] is None
    assert "O cliente não encontrou o pedido na lista." in aberta.resumo


@pytest.mark.parametrize("escolha", [0, 3, -1])
def test_numero_fora_da_lista_e_invalido(devolucao, fila, escolha):
    resultado = devolucao.solicitar(
        "CANCELAMENTO", TELEFONE, encontrados=ENCONTRADOS, escolha=escolha, motivo="DESISTI"
    )

    assert isinstance(resultado, SolicitacaoInvalida)
    assert resultado.campos() == ["escolha"]
    assert fila.listar_abertas() == []


@pytest.mark.parametrize(
    "argumentos",
    [
        {"telefone": None},
        {"escolha": "1"},
        {"escolha": True},
        {"encontrados": [PEDIDO], "escolha": 1},
        {"encontrados": None, "escolha": 1},  # escolher sem lista
    ],
)
def test_argumento_de_tipo_errado_e_erro_de_programacao(devolucao, argumentos):
    base = {"telefone": TELEFONE, **DO_PEDIDO}
    base.update(argumentos)
    telefone = base.pop("telefone")

    with pytest.raises(TypeError):
        devolucao.solicitar("CANCELAMENTO", telefone, motivo="DESISTI", **base)


def test_texto_tambem_e_aceito_com_motivo_da_lista(devolucao, fila):
    devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DEMOROU", texto="esperei 2 horas")

    assert fila.listar_abertas()[0].texto_cliente == "esperei 2 horas"


def test_texto_longo_demais_com_motivo_da_lista_e_invalido(devolucao, fila):
    resultado = devolucao.solicitar("CANCELAMENTO", TELEFONE, **DO_PEDIDO, motivo="DEMOROU", texto="a" * 201)

    assert isinstance(resultado, SolicitacaoInvalida)
    assert resultado.campos() == ["texto"]

"""Testes da fila de atendimento (marco D1 da devolução).

Seam único combinado: FilaDeAtendimento (abrir, listar_abertas, concluir).
Pasta temporária + relógio falso. Dados fictícios.
"""

import json
from datetime import datetime, timezone

import pytest

from revgas_chatbot.api_falsa import RelogioFalso
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA

TELEFONE = "5586999990000"
ONZE_DE_NOVEMBRO = datetime(2026, 11, 11, 10, 0, tzinfo=FUSO_TERESINA)


@pytest.fixture
def relogio():
    return RelogioFalso(ONZE_DE_NOVEMBRO)


@pytest.fixture
def fila(tmp_path, relogio):
    return FilaDeAtendimento(tmp_path, relogio)


def test_primeira_devolucao_do_dia_recebe_protocolo_dev_1_e_fica_aberta(fila):
    protocolo = fila.abrir("CANCELAMENTO", TELEFONE, "Cliente desistiu do pedido 123.")

    assert protocolo == "DEV-20261111-1"
    [aberta] = fila.listar_abertas()
    assert aberta.protocolo == "DEV-20261111-1"
    assert aberta.tipo == "CANCELAMENTO"
    assert aberta.telefone == TELEFONE
    assert aberta.resumo == "Cliente desistiu do pedido 123."
    assert aberta.prioridade == "NORMAL"


def test_segunda_devolucao_do_dia_recebe_o_numero_seguinte(fila):
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    assert fila.abrir("ESTORNO", TELEFONE, "b") == "DEV-20261111-2"


def test_venda_e_comprovante_usam_atd_com_numeracao_propria(fila):
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.abrir("ESTORNO", TELEFONE, "b")

    assert fila.abrir("VENDA", TELEFONE, "c") == "ATD-20261111-1"
    assert fila.abrir("COMPROVANTE_PIX", TELEFONE, "d") == "ATD-20261111-2"
    assert fila.abrir("ALTERACAO", TELEFONE, "e") == "DEV-20261111-3"


def test_vazamento_e_urgente_e_os_demais_sao_normais(fila):
    fila.abrir("VAZAMENTO", TELEFONE, "cheiro de gás")
    fila.abrir("VASILHAME", TELEFONE, "troca de casco")

    prioridades = {s.tipo: s.prioridade for s in fila.listar_abertas()}
    assert prioridades == {"VAZAMENTO": "URGENTE", "VASILHAME": "NORMAL"}


def test_tipo_desconhecido_e_recusado_e_nada_e_gravado(fila):
    with pytest.raises(ValueError):
        fila.abrir("DEVOLUCAO_QUALQUER", TELEFONE, "x")
    assert fila.listar_abertas() == []


def test_data_do_protocolo_usa_o_fuso_de_teresina(tmp_path):
    # 02:30 UTC do dia 12 = 23:30 do dia 11 em Teresina.
    relogio = RelogioFalso(datetime(2026, 11, 12, 2, 30, tzinfo=timezone.utc))
    fila = FilaDeAtendimento(tmp_path, relogio)

    assert fila.abrir("CANCELAMENTO", TELEFONE, "a") == "DEV-20261111-1"


def test_numeracao_recomeca_no_dia_seguinte(fila, relogio):
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.abrir("CANCELAMENTO", TELEFONE, "b")
    relogio.avancar(24 * 60 * 60)

    assert fila.abrir("CANCELAMENTO", TELEFONE, "c") == "DEV-20261112-1"


def test_fila_sobrevive_a_reiniciar_o_programa(tmp_path, relogio):
    FilaDeAtendimento(tmp_path, relogio).abrir("CANCELAMENTO", TELEFONE, "antes de reiniciar")

    reiniciada = FilaDeAtendimento(tmp_path, relogio)

    assert [s.resumo for s in reiniciada.listar_abertas()] == ["antes de reiniciar"]
    assert reiniciada.abrir("ESTORNO", TELEFONE, "depois") == "DEV-20261111-2"


# --- concluir e ordem ------------------------------------------------------------


def test_concluida_sai_da_lista_de_abertas(fila):
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.abrir("ESTORNO", TELEFONE, "b")
    fila.concluir("DEV-20261111-1", "cancelado no ERP pela equipe")

    assert [s.protocolo for s in fila.listar_abertas()] == ["DEV-20261111-2"]


def test_concluida_continua_concluida_depois_de_reiniciar(tmp_path, relogio):
    fila = FilaDeAtendimento(tmp_path, relogio)
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.concluir("DEV-20261111-1")

    assert FilaDeAtendimento(tmp_path, relogio).listar_abertas() == []


def preparar_uma_aberta_e_uma_concluida(fila):
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.abrir("ESTORNO", TELEFONE, "b")
    fila.concluir("DEV-20261111-2")


@pytest.mark.parametrize(
    "protocolo",
    [
        "DEV-20261111-9",  # nunca existiu
        "DEV-20261111-2",  # já concluído
    ],
)
def test_concluir_protocolo_que_nao_esta_aberto_da_erro_e_nada_muda(fila, tmp_path, protocolo):
    preparar_uma_aberta_e_uma_concluida(fila)
    antes = (tmp_path / "fila.jsonl").read_bytes()

    with pytest.raises(ValueError):
        fila.concluir(protocolo)

    assert (tmp_path / "fila.jsonl").read_bytes() == antes
    assert [s.protocolo for s in fila.listar_abertas()] == ["DEV-20261111-1"]


def test_arquivo_so_cresce(fila, tmp_path):
    arquivo = tmp_path / "fila.jsonl"
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    depois_de_abrir = arquivo.read_bytes()
    fila.abrir("ESTORNO", TELEFONE, "b")
    fila.concluir("DEV-20261111-1")

    assert arquivo.read_bytes().startswith(depois_de_abrir)


def test_urgentes_primeiro_depois_as_mais_antigas(fila, relogio):
    fila.abrir("CANCELAMENTO", TELEFONE, "1º normal")
    relogio.avancar(60)
    fila.abrir("ESTORNO", TELEFONE, "2º normal")
    relogio.avancar(60)
    fila.abrir("VAZAMENTO", TELEFONE, "urgente, aberto por último")

    assert [s.resumo for s in fila.listar_abertas()] == [
        "urgente, aberto por último",
        "1º normal",
        "2º normal",
    ]


# --- dados pessoais: mascarar o texto do cliente antes de gravar (D4) ------------


@pytest.mark.parametrize(
    "digitado",
    [
        "meu cpf é 12345678901",
        "meu cpf é 123.456.789-01",
        "cartão 4111 1111 1111 1111",
        "chave pix 86999990000",
    ],
)
def test_numero_longo_no_texto_do_cliente_e_mascarado_antes_de_gravar(fila, tmp_path, digitado):
    fila.abrir("ESTORNO", TELEFONE, "Estorno pedido pelo cliente.", texto_cliente=digitado)

    [aberta] = fila.listar_abertas()
    assert "[número omitido]" in aberta.texto_cliente
    numero = "".join(c for c in digitado if c.isdigit())
    gravado = (tmp_path / "fila.jsonl").read_text(encoding="utf-8")
    assert numero not in "".join(c for c in gravado.replace(TELEFONE, "") if c.isdigit())


def test_numeros_curtos_no_texto_do_cliente_nao_sao_mascarados(fila):
    texto = "Pedi em 11/11/2026, paguei R$ 110,00 por 2 botijões."
    fila.abrir("CANCELAMENTO", TELEFONE, "Cancelamento.", texto_cliente=texto)

    assert fila.listar_abertas()[0].texto_cliente == texto


@pytest.mark.parametrize(
    "resumo",
    [
        "Resultado incerto; pode existir venda com a marca "
        "[chatbot:12345678-1234-1234-1234-123456789012]: verifique antes de criar outra.",
        "Pedidos 30756651 30756652 do cliente.",
    ],
)
def test_resumo_montado_pelo_sistema_nao_e_mascarado(fila, resumo):
    fila.abrir("VENDA", TELEFONE, resumo)

    assert fila.listar_abertas()[0].resumo == resumo


def test_telefone_nome_e_dados_sao_guardados_como_vieram(fila):
    dados = {"venda_id": 30756651, "marca": "[chatbot:ped-1]", "venda_pode_existir": True}
    fila.abrir("VENDA", TELEFONE, "resultado incerto", nome="Cliente Teste", dados=dados)

    [aberta] = fila.listar_abertas()
    assert aberta.telefone == TELEFONE
    assert aberta.nome == "Cliente Teste"
    assert aberta.dados == dados
    assert aberta.texto_cliente is None


# --- queda no meio da gravação ---------------------------------------------------


def test_linha_final_truncada_nao_impede_abrir_nem_listar(fila, tmp_path, relogio):
    fila.abrir("CANCELAMENTO", TELEFONE, "antes da queda")
    with (tmp_path / "fila.jsonl").open("a", encoding="utf-8") as arquivo:
        arquivo.write('{"evento": "ABERTA", "protocolo": "DEV-2026')  # escrita interrompida

    protocolo = fila.abrir("VAZAMENTO", TELEFONE, "depois da queda")

    assert protocolo == "DEV-20261111-2"
    assert [s.resumo for s in fila.listar_abertas()] == ["depois da queda", "antes da queda"]
    # E continua funcionando depois de reiniciar.
    assert len(FilaDeAtendimento(tmp_path, relogio).listar_abertas()) == 2


def test_protocolo_nunca_se_repete_mesmo_com_evento_estragado_no_arquivo(tmp_path, relogio):
    fila = FilaDeAtendimento(tmp_path, relogio)
    fila.abrir("CANCELAMENTO", TELEFONE, "a")
    fila.abrir("ESTORNO", TELEFONE, "b")  # o cliente recebeu DEV-20261111-2
    arquivo = tmp_path / "fila.jsonl"
    linhas = arquivo.read_text(encoding="utf-8").splitlines()
    estragada = json.loads(linhas[1])
    del estragada["tipo"]  # o evento do -2 fica inválido
    arquivo.write_text("\n".join([linhas[0], json.dumps(estragada)]) + "\n", encoding="utf-8")

    reiniciada = FilaDeAtendimento(tmp_path, relogio)

    assert reiniciada.abrir("CANCELAMENTO", TELEFONE, "c") == "DEV-20261111-3"


def test_numeracao_segue_o_maior_numero_e_nao_a_contagem(fila, tmp_path):
    with (tmp_path / "fila.jsonl").open("a", encoding="utf-8") as arquivo:
        arquivo.write('{"evento": "CONCLUIDA", "protocolo": "DEV-20261111-7"}\n')

    assert fila.abrir("CANCELAMENTO", TELEFONE, "a") == "DEV-20261111-8"

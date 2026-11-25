"""Testes do atendimento pelo WhatsApp (o mesmo para qualquer canal de WhatsApp).

Seam: AtendimentoWhatsApp.receber(MensagemRecebida) — o que o receptor do
WAHA entrega depois de conferir assinatura, filtros e repetidas (esses ficam
em test_canal_waha.py) —, com a API de teste da RevGás, e um
enviador falso. Sem internet. Os números "da equipe" são inventados.
"""

import itertools
from datetime import datetime

import pytest

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.atendimento_whatsapp import NAO_TEXTO, AtendimentoWhatsApp, MensagemRecebida
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.montagem import api_de_demonstracao, montar, regra_da_equipe

MARIA = "5500900000001"  # cadastrada na API de teste
EQUIPE = "5586999990000"  # inventado
EQUIPE_SEM_9 = "558699990000"
OUTRO = "5586988880000"  # inventado, fora da equipe


class EnviadorFalso:
    def __init__(self):
        self.enviadas: list[tuple[str, str]] = []
        self.falhar_em: set[int] = set()  # números de ordem (0, 1, ...) dos envios que falham

    def enviar(self, para, texto):
        ordem = len(self.enviadas)
        self.enviadas.append((para, texto))
        return ordem not in self.falhar_em

    def para(self, telefone):
        return [t for p, t in self.enviadas if p == telefone]


class WhatsAppSimulado:
    """Entrega mensagens ao atendimento, como o receptor faria, e espera ele terminar."""

    def __init__(self, tmp_path, *, criar_conversa=None):
        self.api = api_de_demonstracao(RelogioFalso(datetime(2026, 11, 12, 15, 0, tzinfo=FUSO_TERESINA)))
        self.pasta = tmp_path
        programa = montar(tmp_path, relogio=self.api.relogio, api=self.api, ambiente={})
        self.enviador = EnviadorFalso()
        self.atendimento = AtendimentoWhatsApp(
            criar_conversa or (lambda tel: programa(tel, numero_confiavel=True)), self.enviador)
        self._ids = itertools.count(1)
        self.atendimento.ligar()

    def mandar(self, texto, *, de=MARIA, tipo="text"):
        self.atendimento.receber(MensagemRecebida(
            telefone=de, id=f"msg.{next(self._ids)}", texto=texto if tipo == "text" else None))
        self.atendimento.esperar()

    def conversa(self, *textos, de=MARIA):
        for t in textos:
            self.mandar(t, de=de)
        return self.enviador.para(de)


@pytest.fixture
def wa(tmp_path):
    simulado = WhatsAppSimulado(tmp_path)
    yield simulado
    simulado.atendimento.desligar()


def test_primeira_mensagem_cumprimenta_e_confirma_o_numero(wa):
    respostas = wa.conversa("oi")

    assert respostas[0].startswith("Olá! Sou o atendente virtual")
    assert "Este número é seu: (00) 90000-0001?" in respostas[1]
    assert "Olá, Maria!" in "\n".join(wa.conversa("sim")[2:])


def test_pedido_completo_pelo_whatsapp(wa):
    respostas = wa.conversa("oi", "sim", "2", "sim", "1", "2", "1")

    assert respostas[-1].startswith("Pedido registrado!")
    assert len(wa.api.vendas_criadas()) == 1


def test_dois_telefones_intercalados_tem_conversas_separadas(wa):
    wa.mandar("oi", de=MARIA)
    wa.mandar("oi", de=OUTRO)
    wa.mandar("sim", de=MARIA)
    wa.mandar("sim", de=OUTRO)

    assert "Olá, Maria!" in "\n".join(wa.enviador.para(MARIA))
    assert "Qual é o seu nome?" in "\n".join(wa.enviador.para(OUTRO))
    assert "Maria" not in "\n".join(wa.enviador.para(OUTRO))


def test_resposta_vai_para_o_numero_como_chegou(wa):
    wa.mandar("oi", de=EQUIPE_SEM_9)

    assert {p for p, _ in wa.enviador.enviadas} == {EQUIPE_SEM_9}


def test_conversa_encerrada_recomeca_na_proxima_mensagem(wa):
    wa.conversa("oi", "sair")
    respostas = wa.conversa("oi de novo")

    assert respostas[-2].startswith("Olá! Sou o atendente virtual")


def test_mensagem_que_nao_e_texto_responde_e_mantem_o_passo(wa):
    wa.conversa("oi")
    wa.mandar("", tipo="audio")

    assert wa.enviador.para(MARIA)[-1] == NAO_TEXTO
    assert "Olá, Maria!" in "\n".join(wa.conversa("sim")[-3:])


def test_audio_como_primeira_mensagem_so_responde(wa):
    wa.mandar("", tipo="image")

    assert wa.enviador.para(MARIA) == [NAO_TEXTO]


def test_texto_longo_pede_para_resumir_e_mantem_o_passo(wa):
    wa.conversa("oi")
    wa.mandar("a" * 501)

    assert "até 500 caracteres" in wa.enviador.para(MARIA)[-1]
    assert "Olá, Maria!" in "\n".join(wa.conversa("sim")[-3:])


def test_texto_de_500_caracteres_vai_para_a_conversa(wa):
    wa.conversa("oi")
    wa.mandar("a" * 500)

    assert "até 500 caracteres" not in wa.enviador.para(MARIA)[-1]


# --- I1: primeira mensagem com vazamento ou pedido de atendente ---------------------------


def test_primeira_mensagem_com_cheiro_de_gas_recebe_as_orientacoes_primeiro(wa):
    respostas = wa.conversa("estou sentindo cheiro de gás")

    assert respostas[0].startswith("Atenção, possível vazamento de gás")
    assert not any(r.startswith("Olá! Sou") for r in respostas)
    assert "193" in "\n".join(respostas)


def test_primeira_mensagem_pedindo_atendente_transfere(wa):
    respostas = wa.conversa("quero falar com um atendente")

    assert "equipe" in "\n".join(respostas).lower()
    assert not any(r.startswith("Olá! Sou") for r in respostas)


def test_regra_da_equipe():
    regra = regra_da_equipe(["(86) 99999-0000"])

    assert regra(EQUIPE) and regra(EQUIPE_SEM_9) and regra(MARIA)  # MARIA: fictício (DDD 00)
    assert not regra(OUTRO)


# --- I3 e falhas de envio -----------------------------------------------------------------


class _ConversaQuebrada:
    def iniciar(self):
        raise RuntimeError("defeito")

    encerrada = False


def test_erro_numa_conversa_nao_derruba_o_atendimento(tmp_path, caplog):
    api = api_de_demonstracao(RelogioFalso())
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={})

    def criar(tel):
        return _ConversaQuebrada() if tel == OUTRO else programa(tel, numero_confiavel=True)

    wa = WhatsAppSimulado(tmp_path, criar_conversa=criar)
    with caplog.at_level("DEBUG"):
        wa.mandar("Rua Secreta 7", de=OUTRO)
    wa.mandar("oi", de=MARIA)
    wa.atendimento.desligar()

    assert wa.enviador.para(OUTRO) == ["Tive um problema aqui. Pode mandar de novo?"]
    assert wa.enviador.para(MARIA)[0].startswith("Olá! Sou")
    assert "RuntimeError" in caplog.text
    assert "Rua Secreta" not in caplog.text and OUTRO not in caplog.text


def test_falha_no_envio_para_as_respostas_seguintes_e_o_atendimento_segue(wa):
    wa.enviador.falhar_em = {0}  # a saudação falha
    wa.mandar("oi")

    assert len(wa.enviador.para(MARIA)) == 1  # "Este número é seu?" não foi tentado
    wa.mandar("sim")
    assert "Olá, Maria!" in "\n".join(wa.enviador.para(MARIA))


# --- M5 (D16/D17): venda real pelo WhatsApp -------------------------

from revgas_chatbot.montagem import ConfigVendaReal  # noqa: E402

def test_modo_real_pelo_whatsapp_nao_busca_cadastro_e_usa_o_cliente_fixo(tmp_path):
    api = api_de_demonstracao(RelogioFalso(datetime(2026, 11, 13, 15, 0, tzinfo=FUSO_TERESINA)))
    programa = montar(tmp_path, relogio=api.relogio, api=api, ambiente={},
                      venda_real=ConfigVendaReal(url="https://api.exemplo.test/v1", chave="k", cliente_demo=5700355))
    wa = WhatsAppSimulado(tmp_path, criar_conversa=lambda tel: programa(tel, numero_confiavel=True))
    wa.api = api
    respostas = wa.conversa("oi", "sim", "Fulano", "1", "Rua Inventada", "10", "pular", "Centro", "Teresina",
                            "pular", "pular", "1", "2", "1", de=EQUIPE)
    wa.atendimento.desligar()

    assert "Qual é o seu nome?" in respostas
    assert respostas[-1].startswith("Pedido registrado!")
    assert "buscar_clientes" not in api.operacoes and "criar_venda" in api.operacoes
    assert api.vendas_criadas()[0]["customer_id"] == 5700355


# --- devolução na conversa (fase C): primeira mensagem entra direto ----------------------


def test_primeira_mensagem_pedindo_cancelamento_vai_direto_para_a_devolucao(wa):
    respostas = wa.conversa("quero cancelar meu pedido")

    assert not any(r.startswith("Olá! Sou") for r in respostas)
    assert "Não encontrei pedidos recentes" in "\n".join(respostas)  # Maria não tem pedidos na API de teste

"""Testes do WA3: WhatsApp de ponta a ponta, com a Meta simulada.

Seam combinado: o webhook (criar_app_whatsapp) + AtendimentoWhatsApp, com
mensagens assinadas, a API de teste da RevGás, uma IA falsa e um enviador
falso no lugar da Graph API. Sem internet. Os números "da equipe" são
inventados e nunca recebem nada.
"""

import hashlib
import hmac
import itertools
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.canais.canal_whatsapp import (
    NAO_TEXTO,
    AtendimentoWhatsApp,
    ConfiguracaoInvalida,
    configuracao,
    criar_app_whatsapp,
    main,
    regra_da_ia,
)
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.ia.interpretacao import Interpretacao, InterpretadorFalso
from revgas_chatbot.montagem import api_de_demonstracao, montar

SEGREDO = "segredo-de-teste-do-app"
VERIFY_TOKEN = "token-de-verificacao-de-teste-com-mais-de-32-caracteres"
PHONE_NUMBER_ID = "100000000000001"
DOMINIO = "exemplo-fixo.ngrok-free.app"
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
    """Meta simulada: manda mensagens assinadas e espera o atendimento terminar."""

    def __init__(self, tmp_path, *, ia=None, criar_conversa=None):
        self.api = api_de_demonstracao(RelogioFalso(datetime(2026, 11, 12, 15, 0, tzinfo=FUSO_TERESINA)))
        self.ia = ia or InterpretadorFalso()
        self.pasta = tmp_path
        programa = montar(tmp_path, relogio=self.api.relogio, api=self.api, ambiente={},
                          interpretador=self.ia, ia_permitida=regra_da_ia([EQUIPE]))
        self.enviador = EnviadorFalso()
        self.atendimento = AtendimentoWhatsApp(
            criar_conversa or (lambda tel: programa(tel, numero_confiavel=True)), self.enviador)
        app = criar_app_whatsapp(receber=self.atendimento.receber, app_secret=SEGREDO,
                                 verify_token=VERIFY_TOKEN, phone_number_id=PHONE_NUMBER_ID, dominio=DOMINIO)
        self.cliente = TestClient(app, base_url=f"https://{DOMINIO}")
        self._ids = itertools.count(1)
        self.atendimento.ligar()

    def mandar(self, texto, *, de=MARIA, id=None, tipo="text"):
        id = id or f"wamid.{next(self._ids)}"
        m = {"from": de, "id": id, "timestamp": "1762970000", "type": tipo}
        m.update({"text": {"body": texto}} if tipo == "text" else {tipo: {"id": "1"}})
        corpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{
            "field": "messages",
            "value": {"metadata": {"phone_number_id": PHONE_NUMBER_ID}, "messages": [m]}}]}]}).encode()
        assinatura = "sha256=" + hmac.new(SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
        r = self.cliente.post("/webhook", content=corpo, headers={"X-Hub-Signature-256": assinatura})
        assert r.status_code == 200
        self.atendimento.esperar()
        return id

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
    respostas = wa.conversa("oi", "sim", "1", "2", "sim", "1", "1")

    assert respostas[-1].startswith("Pedido registrado!")
    assert len(wa.api.vendas_criadas()) == 1


def test_sim_repetido_no_resumo_nao_registra_duas_vezes(wa):
    wa.conversa("oi", "sim", "1", "2", "sim", "1")
    id_do_sim = wa.mandar("1")
    enviadas = len(wa.enviador.enviadas)
    wa.mandar("1", id=id_do_sim)  # a Meta reentrega a mesma mensagem

    assert len(wa.api.vendas_criadas()) == 1
    assert len(wa.enviador.enviadas) == enviadas  # nenhuma resposta a mais


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
    assert "URGENTE" in (wa.pasta / "fila.html").read_text(encoding="utf-8")


def test_primeira_mensagem_pedindo_atendente_transfere(wa):
    respostas = wa.conversa("quero falar com um atendente")

    assert "ATD-" in "\n".join(respostas)
    assert not any(r.startswith("Olá! Sou") for r in respostas)


# --- W2: IA só para os números da equipe -------------------------------------------------


@pytest.mark.parametrize("numero", [EQUIPE, EQUIPE_SEM_9])
def test_numero_da_equipe_usa_a_ia(tmp_path, numero):
    ia = InterpretadorFalso({"quero 2 botijões": Interpretacao(intencao="PEDIR", produto="botijão de gás",
                                                               quantidade=2)})
    wa = WhatsAppSimulado(tmp_path, ia=ia)
    wa.conversa("oi", "sim", "Fulano", "quero 2 botijões", de=numero)
    wa.atendimento.desligar()

    assert "quero 2 botijões" in [t for t, _ in ia.recebidos]


def test_numero_fora_da_equipe_nunca_usa_a_ia(tmp_path):
    ia = InterpretadorFalso()
    wa = WhatsAppSimulado(tmp_path, ia=ia)
    wa.conversa("oi", "sim", "Fulano", "quero 2 botijões, por favor", de=OUTRO)
    wa.atendimento.desligar()

    assert ia.recebidos == []


def test_equipe_que_troca_para_outro_numero_perde_a_ia(tmp_path):
    ia = InterpretadorFalso()
    wa = WhatsAppSimulado(tmp_path, ia=ia)
    wa.conversa("oi", "não", "(86) 98888-0000", "sim", "Fulano", "quero 2 botijões, por favor", de=EQUIPE)
    wa.atendimento.desligar()

    assert ia.recebidos == []


def test_regra_da_ia():
    regra = regra_da_ia(["(86) 99999-0000"])

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


# --- configuração e comando --------------------------------------------------------------


AMBIENTE = {
    "WHATSAPP_TOKEN": "token-ficticio",
    "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    "WHATSAPP_APP_SECRET": SEGREDO,
    "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
    "WHATSAPP_NUMEROS_EQUIPE": "(86) 99999-0000, 5586977770000",
    "WHATSAPP_DOMINIO": DOMINIO,
}


def test_configuracao_completa():
    c = configuracao(AMBIENTE)

    assert c.dominio == DOMINIO and c.phone_number_id == PHONE_NUMBER_ID
    assert c.numeros_equipe == ("5586999990000", "5586977770000")


@pytest.mark.parametrize("nome", sorted(AMBIENTE))
def test_variavel_faltando_diz_o_nome_e_nunca_o_valor(nome):
    ambiente = {**AMBIENTE, nome: "  "}
    with pytest.raises(ConfiguracaoInvalida) as erro:
        configuracao(ambiente)

    assert nome in str(erro.value)
    for valor in AMBIENTE.values():
        assert valor.strip() not in str(erro.value)


def test_verify_token_curto_e_recusado():
    with pytest.raises(ConfiguracaoInvalida, match="WHATSAPP_VERIFY_TOKEN"):
        configuracao({**AMBIENTE, "WHATSAPP_VERIFY_TOKEN": "curto"})


@pytest.mark.parametrize("lista", ["123", "5586999990000, abc", "1,2,3,4,5,6"])
def test_lista_da_equipe_invalida_e_recusada(lista):
    with pytest.raises(ConfiguracaoInvalida, match="WHATSAPP_NUMEROS_EQUIPE"):
        configuracao({**AMBIENTE, "WHATSAPP_NUMEROS_EQUIPE": lista})


def test_comando_liga_so_em_127_0_0_1_na_porta_8001(tmp_path, capsys):
    chamadas = []
    main([], rodar=lambda app, **opcoes: chamadas.append(opcoes), pasta_dados=tmp_path, ambiente=AMBIENTE)

    [opcoes] = chamadas
    assert (opcoes["host"], opcoes["port"], opcoes["access_log"]) == ("127.0.0.1", 8001, False)
    saida = capsys.readouterr().out
    assert DOMINIO in saida
    for segredo in (SEGREDO, VERIFY_TOKEN, "token-ficticio", "5586999990000"):
        assert segredo not in saida


def test_comando_sem_configuracao_nao_liga(tmp_path):
    chamadas = []
    with pytest.raises(SystemExit) as saida:
        main([], rodar=lambda app, **o: chamadas.append(o), pasta_dados=tmp_path, ambiente={})

    assert chamadas == []
    assert "WHATSAPP_TOKEN" in str(saida.value)


def test_comando_nao_aceita_outro_endereco(tmp_path):
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"], rodar=lambda app, **o: None, pasta_dados=tmp_path, ambiente=AMBIENTE)

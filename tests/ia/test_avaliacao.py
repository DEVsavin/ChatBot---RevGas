"""Testes do IA1: o medidor da IA (`ia.avaliacao` e o comando `ia.avaliar`), sem rede.

Seam combinado (spec-medidor.md): carregar_frases/avaliar/relatorio e o main
do comando, com o InterpretadorFalso e o relógio falso. Frases fictícias.
"""

import json
from datetime import datetime
from decimal import Decimal

import pytest

from revgas_chatbot.api_revgas.api_falsa import RelogioFalso
from revgas_chatbot.fuso import FUSO_TERESINA
from revgas_chatbot.ia.avaliacao import ArquivoInvalido, avaliar, carregar_frases, comparar, relatorio
from revgas_chatbot.ia.avaliar import main
from revgas_chatbot.ia.interpretacao import Interpretacao, InterpretadorFalso
from revgas_chatbot.vendas.pedido import ProdutoCatalogo

CATALOGO = {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}
FORMAS = {"PIX": "PIX", "MONEY": "Dinheiro", "CARTAO_DE_DEBITO": "Cartão de débito",
          "CARTAO_DE_CREDITO": "Cartão de crédito"}
AGORA = datetime(2026, 11, 18, 20, 0, tzinfo=FUSO_TERESINA)
COMPLETA = "manda 2 botijão pra Rua das Flores 10, Centro, Teresina-PI, pago no pix"


def frase(id="f1", texto=COMPLETA, passo="PRODUTO", grupo="ajuste", **esperado):
    return {"id": id, "texto": texto, "passo": passo, "grupo": grupo, "esperado": esperado}


def gravar(tmp_path, itens):
    caminho = tmp_path / "frases.json"
    caminho.write_text(json.dumps(itens, ensure_ascii=False), encoding="utf-8")
    return caminho


def rodar(tmp_path, itens, ia):
    frases = carregar_frases(gravar(tmp_path, itens))
    relogio = RelogioFalso(AGORA)
    return avaliar(frases, ia, catalogo=CATALOGO, formas=FORMAS, relogio=relogio, modelo="falso"), relogio


ENDERECO = {"rua": "Rua das Flores", "numero": "10", "bairro": "Centro", "cidade": "Teresina", "estado": "PI"}


# --- comparação (depois de validar_interpretacao) -------------------------------------------


def test_frase_certa_conta_como_certa(tmp_path):
    ia = InterpretadorFalso({COMPLETA: Interpretacao(intencao="PEDIR", produto="botijão de gás", quantidade=2,
                                                     endereco=ENDERECO, pagamento="PIX")})
    resultado, _ = rodar(tmp_path, [frase(intencao="PEDIR", quantidade=2, pagamento="PIX",
                                          endereco={"logradouro": "rua das flores", "estado": "Piauí"})], ia)

    assert resultado.placar("ajuste") == (1, 1)
    assert "1 de 1 certas" in relatorio(resultado)


def test_campo_ausente_nao_e_conferido(tmp_path):
    ia = InterpretadorFalso({COMPLETA: Interpretacao(intencao="PEDIR", quantidade=3)})
    resultado, _ = rodar(tmp_path, [frase(intencao="PEDIR")], ia)

    assert resultado.placar("ajuste") == (1, 1)


def test_campo_diferente_e_erro_com_esperado_e_obtido(tmp_path):
    ia = InterpretadorFalso({COMPLETA: Interpretacao(intencao="PEDIR", quantidade=3, pagamento="Dinheiro")})
    resultado, _ = rodar(tmp_path, [frase(intencao="PEDIR", quantidade=2, pagamento="PIX")], ia)
    texto = relatorio(resultado)

    assert resultado.placar("ajuste") == (0, 1)
    assert f"✗ f1 [ajuste]: {COMPLETA}" in texto
    assert "quantidade: esperado 2, obtido 3" in texto
    assert "pagamento: esperado PIX, obtido MONEY" in texto
    assert "intencao" not in texto  # só os campos diferentes


def test_null_exige_vazio(tmp_path):
    texto = "quero gás"
    ia = InterpretadorFalso({texto: Interpretacao(intencao="PEDIR", endereco={"rua": "gás"})})
    # I5 descarta a "rua" que o cliente não disse? "gás" está no texto: passa na validação.
    resultado, _ = rodar(tmp_path, [frase(texto=texto, intencao="PEDIR", endereco=None)], ia)

    assert resultado.placar("ajuste") == (0, 1)
    assert "endereco: esperado vazio" in relatorio(resultado)


def test_compara_depois_da_validacao(tmp_path):
    # A IA "inventa" uma rua que não está na frase: a validação (I5) tira, e o medidor vê vazio.
    texto = "quero 2 botijões"
    ia = InterpretadorFalso({texto: Interpretacao(intencao="PEDIR", quantidade=2, endereco={"rua": "Rua Inventada"})})
    resultado, _ = rodar(tmp_path, [frase(texto=texto, intencao="PEDIR", quantidade=2, endereco=None)], ia)

    assert resultado.placar("ajuste") == (1, 1)


def test_comparar_endereco_sem_diferenca_de_maiusculas_e_acentos():
    assert comparar({"endereco": {"bairro": "Sao Joao"}}, {"endereco": {"bairro": "São João "}}) == []
    assert comparar({"endereco": {"bairro": "Centro"}}, {"endereco": {"bairro": "Fátima"}}) != []


# --- sem resposta, cota (I2), ritmo --------------------------------------------------------


class _IaComFalha(InterpretadorFalso):
    def __init__(self, falhas, respostas=None):
        super().__init__(respostas or {})
        self.falhas = list(falhas)  # uma por chamada: "cota", "http" ou None (responde)
        self.ultima_falha = None

    def interpretar(self, texto, contexto):
        falha = self.falhas.pop(0) if self.falhas else None
        self.ultima_falha = falha
        if falha:
            self.recebidos.append((texto, contexto))
            return None
        return super().interpretar(texto, contexto)


def test_sem_resposta_conta_como_erro_e_segue(tmp_path):
    ia = _IaComFalha(["http", None], {"segunda": Interpretacao(intencao="OUTRA")})
    resultado, _ = rodar(tmp_path, [frase(id="a", intencao="PEDIR"),
                                    frase(id="b", texto="segunda", intencao="OUTRA")], ia)

    assert resultado.placar("ajuste") == (1, 2)
    assert "sem resposta" in relatorio(resultado)


def test_cota_uma_vez_espera_30s_e_mede(tmp_path):
    ia = _IaComFalha(["cota", None], {COMPLETA: Interpretacao(intencao="PEDIR")})
    resultado, relogio = rodar(tmp_path, [frase(intencao="PEDIR")], ia)

    assert 30 in relogio.esperas
    assert resultado.placar("ajuste") == (1, 1)


def test_cota_duas_vezes_fica_fora_da_conta(tmp_path):
    ia = _IaComFalha(["cota", "cota"])
    resultado, _ = rodar(tmp_path, [frase(intencao="PEDIR")], ia)
    texto = relatorio(resultado)

    assert resultado.placar("ajuste") == (0, 0)
    assert "Não medidas (limite de cota): f1" in texto


def test_uma_frase_a_cada_4_segundos_e_tempo_total(tmp_path):
    ia = InterpretadorFalso()
    resultado, relogio = rodar(tmp_path, [frase(id=f"f{i}", intencao="OUTRA") for i in range(3)], ia)

    assert relogio.esperas == [4, 4]
    assert "Tempo: 8 s" in relatorio(resultado)


def test_texto_mascarado_para_a_ia_e_original_para_a_validacao(tmp_path):
    texto = "meu cpf 12345678901, quero 1 botijão"
    ia = InterpretadorFalso()
    rodar(tmp_path, [frase(texto=texto, intencao="OUTRA")], ia)

    [(enviado, contexto)] = ia.recebidos
    assert "12345678901" not in enviado
    assert contexto.passo == "PRODUTO" and contexto.produtos == ("botijão de gás",)


def test_placares_por_grupo(tmp_path):
    ia = InterpretadorFalso({"a": Interpretacao(intencao="OUTRA"), "b": Interpretacao(intencao="OUTRA")})
    resultado, _ = rodar(tmp_path, [frase(id="1", texto="a", grupo="ajuste", intencao="OUTRA"),
                                    frase(id="2", texto="b", grupo="prova", intencao="PEDIR")], ia)

    assert "ajuste: 1 de 1 · prova: 0 de 1" in relatorio(resultado)


# --- arquivo de frases ----------------------------------------------------------------------


@pytest.mark.parametrize("item, trecho", [
    (frase(passo="PASSO_INVENTADO"), "passo"),
    (frase(grupo="treino"), "grupo"),
    (frase(intencao="COMPRAR"), "intencao"),
    (frase(cor="azul"), "cor"),
    (frase(endereco={"pais": "Brasil"}), "pais"),
    ({"id": "f1", "texto": "", "passo": "PRODUTO", "grupo": "ajuste", "esperado": {}}, "texto"),
])
def test_arquivo_invalido_diz_o_item(tmp_path, item, trecho):
    with pytest.raises(ArquivoInvalido) as erro:
        carregar_frases(gravar(tmp_path, [item]))

    assert "f1" in str(erro.value) and trecho in str(erro.value)


def test_ids_repetidos_sao_recusados(tmp_path):
    with pytest.raises(ArquivoInvalido):
        carregar_frases(gravar(tmp_path, [frase(id="x"), frase(id="x")]))


# --- o comando ------------------------------------------------------------------------------


def test_sem_chave_nao_chama_nada(tmp_path, capsys):
    with pytest.raises(SystemExit) as saida:
        main([], ambiente={}, pasta_dados=tmp_path)

    assert saida.value.code != 0
    assert "GEMINI_API_KEY" in capsys.readouterr().out


def test_arquivo_invalido_sai_antes_de_chamar_a_ia(tmp_path, capsys):
    ia = InterpretadorFalso()
    caminho = gravar(tmp_path, [frase(passo="PASSO_INVENTADO")])
    with pytest.raises(SystemExit):
        main(["--frases", str(caminho)], ambiente={"GEMINI_API_KEY": "x"}, interpretador=ia,
             relogio=RelogioFalso(AGORA), pasta_dados=tmp_path)

    assert ia.recebidos == []
    assert "f1" in capsys.readouterr().out


def test_comando_imprime_o_placar_e_salva_sem_a_chave(tmp_path, capsys):
    caminho = gravar(tmp_path, [frase(intencao="OUTRA")])
    main(["--frases", str(caminho), "--salvar", "--modelo", "modelo-teste"], ambiente={"GEMINI_API_KEY": "chave-secreta"},
         interpretador=InterpretadorFalso({COMPLETA: Interpretacao(intencao="OUTRA")}),
         relogio=RelogioFalso(AGORA), pasta_dados=tmp_path)

    saida = capsys.readouterr().out
    assert "1 de 1 certas" in saida and "modelo-teste" in saida
    [salvo] = (tmp_path / "ia-avaliacao").glob("*.json")
    conteudo = salvo.read_text(encoding="utf-8")
    assert "chave-secreta" not in conteudo
    assert json.loads(conteudo)["modelo"] == "modelo-teste"


def test_modelo_vai_para_o_interpretador_gemini(tmp_path, monkeypatch):
    criados = []

    class GeminiFalso(InterpretadorFalso):
        def __init__(self, chave, *, modelo):
            super().__init__()
            criados.append(modelo)

    monkeypatch.setattr("revgas_chatbot.ia.avaliar.InterpretadorGemini", GeminiFalso)
    caminho = gravar(tmp_path, [frase(intencao="OUTRA")])
    main(["--frases", str(caminho), "--modelo", "gemini-3.5-flash"], ambiente={"GEMINI_API_KEY": "x"},
         relogio=RelogioFalso(AGORA), pasta_dados=tmp_path)

    assert criados == ["gemini-3.5-flash"]


# --- IA2: o arquivo real de frases -----------------------------------------------------------


def test_arquivo_real_de_frases():
    from revgas_chatbot.ia.avaliar import FRASES
    from revgas_chatbot.ia.interpretacao import INTENCOES

    frases = carregar_frases(FRASES)  # formato válido (passos, campos e intenções existem)

    assert len(frases) >= 30
    prova = [f for f in frases if f.grupo == "prova"]
    assert len(prova) >= len(frases) // 4  # ~1/3 de prova
    assert {f.esperado.get("intencao") for f in prova} >= INTENCOES  # a prova cobre cada intenção
    assert {f.grupo for f in frases} == {"ajuste", "prova"}

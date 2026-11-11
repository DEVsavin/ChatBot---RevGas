"""Testes da página da fila (marco D2 da devolução).

Seam único combinado: a página `fila.html` que a FilaDeAtendimento grava na
própria pasta a cada mudança. A página é lida como o atendente veria: cada
solicitação é um <article>, com o texto visível e as classes.
"""

from datetime import datetime
from html.parser import HTMLParser

import pytest

from revgas_chatbot.api_falsa import RelogioFalso
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.fuso import FUSO_TERESINA

TELEFONE = "5586999990000"


class _Leitor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.artigos: list[dict] = []
        self.texto_fora: list[str] = []
        self._dentro = 0

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.artigos.append({"classes": (dict(attrs).get("class") or "").split(), "texto": ""})
            self._dentro += 1

    def handle_endtag(self, tag):
        if tag == "article":
            self._dentro -= 1

    def handle_data(self, data):
        if self._dentro:
            self.artigos[-1]["texto"] += data
        else:
            self.texto_fora.append(data)


def ler_pagina(pasta) -> _Leitor:
    leitor = _Leitor()
    leitor.feed((pasta / "fila.html").read_text(encoding="utf-8"))
    return leitor


@pytest.fixture
def relogio():
    return RelogioFalso(datetime(2026, 11, 11, 10, 5, tzinfo=FUSO_TERESINA))


@pytest.fixture
def fila(tmp_path, relogio):
    return FilaDeAtendimento(tmp_path, relogio)


def test_pagina_mostra_os_dados_da_solicitacao_aberta(fila, tmp_path):
    fila.abrir(
        "CANCELAMENTO",
        TELEFONE,
        "Cancelamento do pedido 30756651.",
        nome="Cliente Teste",
        texto_cliente="desisti, comprei em outro lugar",
    )

    [artigo] = ler_pagina(tmp_path).artigos
    for esperado in (
        "DEV-20261111-1",
        "CANCELAMENTO",
        TELEFONE,
        "Cliente Teste",
        "Cancelamento do pedido 30756651.",
        "desisti, comprei em outro lugar",
        "11/11/2026 10:05",
    ):
        assert esperado in artigo["texto"]


def test_concluida_some_da_pagina(fila, tmp_path):
    fila.abrir("CANCELAMENTO", TELEFONE, "primeira")
    fila.abrir("ESTORNO", TELEFONE, "segunda")
    fila.concluir("DEV-20261111-1")

    textos = [a["texto"] for a in ler_pagina(tmp_path).artigos]
    assert len(textos) == 1
    assert "DEV-20261111-2" in textos[0]


def test_sem_nada_aberto_a_pagina_diz_isso(fila, tmp_path):
    fila.abrir("CANCELAMENTO", TELEFONE, "única")
    fila.concluir("DEV-20261111-1")

    pagina = ler_pagina(tmp_path)
    assert pagina.artigos == []
    assert "Nenhuma solicitação aberta" in "".join(pagina.texto_fora)


def test_urgente_vem_primeiro_e_em_destaque(fila, tmp_path, relogio):
    fila.abrir("CANCELAMENTO", TELEFONE, "normal, aberta antes")
    relogio.avancar(60)
    fila.abrir("VAZAMENTO", TELEFONE, "cheiro de gás na cozinha")

    primeiro, segundo = ler_pagina(tmp_path).artigos
    assert "cheiro de gás na cozinha" in primeiro["texto"]
    assert "urgente" in primeiro["classes"]
    assert "URGENTE" in primeiro["texto"]
    assert "urgente" not in segundo["classes"]


MALICIOSO = '<script>alert("x")</script><img src=x onerror=alert(1)>'


@pytest.mark.parametrize("campo", ["resumo", "nome", "texto_cliente"])
def test_texto_com_html_aparece_escapado(fila, tmp_path, campo):
    argumentos = {"resumo": "resumo comum", "nome": "Nome", "texto_cliente": "texto comum"}
    argumentos[campo] = MALICIOSO
    fila.abrir("ESTORNO", TELEFONE, argumentos.pop("resumo"), **argumentos)

    html = (tmp_path / "fila.html").read_text(encoding="utf-8")
    assert "<script" not in html
    assert "<img" not in html
    assert MALICIOSO in ler_pagina(tmp_path).artigos[0]["texto"]  # visível como texto


def test_dados_da_solicitacao_aparecem_escapados(fila, tmp_path):
    fila.abrir(
        "VENDA",
        TELEFONE,
        "Resultado incerto ao criar a venda.",
        dados={"marca": "[chatbot:ped-1]", "venda_pode_existir": True, "obs": "<b>x</b>"},
    )

    [artigo] = ler_pagina(tmp_path).artigos
    assert "[chatbot:ped-1]" in artigo["texto"]
    assert "venda_pode_existir" in artigo["texto"]
    assert "<b>x</b>" in artigo["texto"]
    assert "<b>" not in (tmp_path / "fila.html").read_text(encoding="utf-8")


def test_falha_ao_gravar_a_pagina_nao_desfaz_nem_derruba_o_abrir(fila, tmp_path):
    (tmp_path / "fila.html").unlink()  # a fila já gerou a página ao ser criada
    (tmp_path / "fila.html").mkdir()  # o caminho da página está ocupado: gravar vai falhar

    protocolo = fila.abrir("VAZAMENTO", TELEFONE, "cheiro de gás")

    assert protocolo == "DEV-20261111-1"
    assert [s.protocolo for s in fila.listar_abertas()] == ["DEV-20261111-1"]


# --- robustez: nada pode lançar depois de gravar o evento ------------------------


def test_dados_que_nao_sao_dicionario_sao_recusados_antes_de_gravar(fila, tmp_path):
    with pytest.raises(ValueError):
        fila.abrir("VENDA", TELEFONE, "x", dados=["<a>"])

    assert not (tmp_path / "fila.jsonl").exists()


def test_evento_sem_campos_obrigatorios_no_arquivo_e_ignorado(fila, tmp_path):
    fila.abrir("CANCELAMENTO", TELEFONE, "válida")
    with (tmp_path / "fila.jsonl").open("a", encoding="utf-8") as arquivo:
        arquivo.write('{"evento": "ABERTA", "protocolo": "DEV-20261111-9"}\n')  # sem tipo etc.
        arquivo.write('{"sem_evento": true}\n')

    protocolo = fila.abrir("ESTORNO", TELEFONE, "depois")

    assert protocolo == "DEV-20261111-10"  # o -9 da linha estragada nunca é reaproveitado
    assert [s.resumo for s in fila.listar_abertas()] == ["válida", "depois"]
    assert len(ler_pagina(tmp_path).artigos) == 2


def test_pagina_e_gerada_ao_criar_a_fila(tmp_path, relogio):
    FilaDeAtendimento(tmp_path, relogio).abrir("CANCELAMENTO", TELEFONE, "antes")
    (tmp_path / "fila.html").unlink()  # página perdida ou velha

    FilaDeAtendimento(tmp_path, relogio)

    assert len(ler_pagina(tmp_path).artigos) == 1


def test_telefone_com_html_aparece_escapado(fila, tmp_path):
    fila.abrir("ESTORNO", MALICIOSO, "resumo")

    assert "<script" not in (tmp_path / "fila.html").read_text(encoding="utf-8")

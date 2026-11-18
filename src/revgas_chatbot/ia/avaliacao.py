"""Medidor da IA: quantas frases a IA entende do jeito que o chatbot usa.

Spec: `.scratch/ia/spec-medidor.md`. Cada frase do arquivo de avaliação vai à
IA (com o texto mascarado, como na conversa), a resposta passa por
`validar_interpretacao` (com o texto original, I5) e o resultado é comparado
com o esperado da frase, só nos campos listados.

- Grupos (I1): "ajuste" pode guiar as melhorias; "prova" nunca. O número da
  apresentação é o da prova.
- Limite de cota (I2): HTTP 429 não é erro da IA; espera e tenta uma vez de
  novo; se persistir, a frase fica fora da conta ("não medida").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from revgas_chatbot.api_revgas.api import Relogio
from revgas_chatbot.atendimento.fila import mascarar
from revgas_chatbot.conversa.entrada import normalizar
from revgas_chatbot.ia.interpretacao import (
    INTENCOES,
    Contexto,
    Interpretador,
    PedidoInterpretado,
    validar_interpretacao,
)
from revgas_chatbot.vendas.pedido import ProdutoCatalogo

INTERVALO = 4.0  # segundos entre frases (abaixo do limite da cota grátis)
ESPERA_DA_COTA = 30.0  # I2: depois de um 429, espera e tenta uma vez de novo

GRUPOS = ("ajuste", "prova")
CAMPOS = frozenset({"intencao", "produto", "quantidade", "pagamento", "troco", "endereco"})
CAMPOS_DO_ENDERECO = frozenset({"logradouro", "numero", "bairro", "cidade", "estado", "complemento", "referencia"})
# Passos em que a conversa consulta a IA (o que vai no Contexto).
PASSOS = frozenset({
    "CONFIRMAR_NUMERO", "NOME", "PRODUTO", "QUANTIDADE", "ENDERECO_DO_CADASTRO", "CAMPO_DO_ENDERECO",
    "PAGAMENTO", "TROCO", "RESUMO", "POS_VENDA", "DEV_TEXTO",
})


class ArquivoInvalido(ValueError):
    """O arquivo de frases tem um problema; a mensagem diz o item."""


@dataclass(frozen=True)
class Frase:
    id: str
    texto: str
    passo: str
    grupo: str
    esperado: dict[str, Any]


@dataclass(frozen=True)
class Diferenca:
    campo: str
    esperado: Any
    obtido: Any


@dataclass(frozen=True)
class ResultadoDaFrase:
    frase: Frase
    situacao: str  # "certa", "errada", "sem_resposta" ou "nao_medida"
    diferencas: tuple[Diferenca, ...] = ()
    obtido: dict[str, Any] | None = None


@dataclass
class Resultado:
    modelo: str
    itens: list[ResultadoDaFrase] = field(default_factory=list)
    segundos: float = 0.0

    def placar(self, grupo: str | None = None) -> tuple[int, int]:
        """(certas, medidas) do grupo (ou de todas). Frases não medidas ficam fora."""
        medidas = [i for i in self.itens if i.situacao != "nao_medida" and (grupo is None or i.frase.grupo == grupo)]
        return sum(i.situacao == "certa" for i in medidas), len(medidas)


# --- arquivo de frases ----------------------------------------------------------------------


def carregar_frases(caminho: Path) -> list[Frase]:
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ArquivoInvalido(f"não consegui ler {caminho}: {type(erro).__name__}") from None
    if not isinstance(dados, list) or not dados:
        raise ArquivoInvalido("o arquivo precisa ser uma lista de frases")
    frases, ids = [], set()
    for posicao, item in enumerate(dados, 1):
        frase = _frase(item, posicao)
        if frase.id in ids:
            raise ArquivoInvalido(f"id repetido: {frase.id}")
        ids.add(frase.id)
        frases.append(frase)
    return frases


def _frase(item: Any, posicao: int) -> Frase:
    if not isinstance(item, dict):
        raise ArquivoInvalido(f"item {posicao}: não é um objeto")
    id_ = item.get("id")
    nome = id_ if isinstance(id_, str) and id_ else f"item {posicao}"

    def erro(motivo: str) -> ArquivoInvalido:
        return ArquivoInvalido(f"{nome}: {motivo}")

    if not isinstance(id_, str) or not id_:
        raise erro("falta o id")
    texto, passo, grupo, esperado = item.get("texto"), item.get("passo"), item.get("grupo"), item.get("esperado")
    if not isinstance(texto, str) or not texto.strip():
        raise erro("falta o texto")
    if passo not in PASSOS:
        raise erro(f"passo desconhecido: {passo!r}")
    if grupo not in GRUPOS:
        raise erro(f"grupo deve ser ajuste ou prova, não {grupo!r}")
    if not isinstance(esperado, dict):
        raise erro("esperado deve ser um objeto")
    sobrando = set(esperado) - CAMPOS
    if sobrando:
        raise erro(f"campo desconhecido no esperado: {', '.join(sorted(sobrando))}")
    if "intencao" in esperado and esperado["intencao"] not in INTENCOES:
        raise erro(f"intencao desconhecida: {esperado['intencao']!r}")
    endereco = esperado.get("endereco")
    if endereco is not None:
        if not isinstance(endereco, dict):
            raise erro("endereco deve ser um objeto ou null")
        sobrando = set(endereco) - CAMPOS_DO_ENDERECO
        if sobrando:
            raise erro(f"campo desconhecido no endereco: {', '.join(sorted(sobrando))}")
    return Frase(id=id_, texto=texto, passo=passo, grupo=grupo, esperado=esperado)


# --- avaliação ------------------------------------------------------------------------------


def avaliar(
    frases: list[Frase],
    interpretador: Interpretador,
    *,
    catalogo: dict[int, ProdutoCatalogo],
    formas: dict[str, str],
    relogio: Relogio,
    modelo: str = "",
) -> Resultado:
    resultado = Resultado(modelo=modelo)
    inicio = relogio.agora()
    for indice, frase in enumerate(frases):
        if indice:
            relogio.dormir(INTERVALO)
        resultado.itens.append(_avaliar_frase(frase, interpretador, catalogo, formas, relogio))
    resultado.segundos = (relogio.agora() - inicio).total_seconds()
    return resultado


def _avaliar_frase(frase: Frase, interpretador: Any, catalogo: dict, formas: dict, relogio: Relogio) -> ResultadoDaFrase:
    contexto = Contexto(
        passo=frase.passo,
        produtos=tuple(p.nome for p in catalogo.values()),
        pagamentos=tuple(formas.values()),
    )
    interpretacao = interpretador.interpretar(mascarar(frase.texto), contexto)  # como na conversa
    if interpretacao is None and getattr(interpretador, "ultima_falha", None) == "cota":
        relogio.dormir(ESPERA_DA_COTA)  # I2: limite de cota não é erro da IA
        interpretacao = interpretador.interpretar(mascarar(frase.texto), contexto)
        if interpretacao is None and getattr(interpretador, "ultima_falha", None) == "cota":
            return ResultadoDaFrase(frase, "nao_medida")
    if interpretacao is None:
        return ResultadoDaFrase(frase, "sem_resposta", (Diferenca("resposta", "uma resposta", "sem resposta"),))
    validado = validar_interpretacao(interpretacao, catalogo, formas, texto=frase.texto)  # original (I5)
    obtido = _como_dict(validado, catalogo)
    diferencas = tuple(comparar(frase.esperado, obtido))
    return ResultadoDaFrase(frase, "errada" if diferencas else "certa", diferencas, obtido)


def _como_dict(validado: PedidoInterpretado, catalogo: dict[int, ProdutoCatalogo]) -> dict[str, Any]:
    produto = catalogo.get(validado.product_id) if validado.product_id is not None else None
    return {
        "intencao": validado.intencao,
        "produto": produto.nome if produto else None,
        "quantidade": validado.quantidade,
        "pagamento": validado.pagamento,
        "troco": f"{validado.troco:.2f}" if validado.troco is not None else None,
        "endereco": dict(validado.campos_do_endereco) or None,
    }


def _igual(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return normalizar(a) == normalizar(b)
    return a == b


def comparar(esperado: dict[str, Any], obtido: dict[str, Any]) -> list[Diferenca]:
    """Campos diferentes. Campo ausente no esperado não é conferido; `None` exige vazio."""
    diferencas = []
    for campo, valor in esperado.items():
        veio = obtido.get(campo)
        if campo == "endereco" and valor is not None:
            veio = veio or {}
            for parte, esperado_da_parte in valor.items():
                if not _igual(esperado_da_parte, veio.get(parte)):
                    diferencas.append(Diferenca(f"endereco.{parte}", esperado_da_parte, veio.get(parte)))
        elif not _igual(valor, veio):
            diferencas.append(Diferenca(campo, valor, veio))
    return diferencas


# --- relatório e arquivo do resultado -------------------------------------------------------


def _mostrar(valor: Any) -> str:
    return "vazio" if valor is None else str(valor)


def relatorio(resultado: Resultado) -> str:
    linhas = []
    for item in resultado.itens:
        if item.situacao in ("errada", "sem_resposta"):
            linhas.append(f"✗ {item.frase.id} [{item.frase.grupo}]: {item.frase.texto}")
            linhas += [f"    {d.campo}: esperado {_mostrar(d.esperado)}, obtido {_mostrar(d.obtido)}" for d in item.diferencas]
    certas, medidas = resultado.placar()
    percentual = f" ({100 * certas / medidas:.0f}%)" if medidas else ""
    ajuste, prova = resultado.placar("ajuste"), resultado.placar("prova")
    linhas.append("")
    linhas.append(f"{certas} de {medidas} certas{percentual}")
    linhas.append(f"ajuste: {ajuste[0]} de {ajuste[1]} · prova: {prova[0]} de {prova[1]}")
    nao_medidas = [i.frase.id for i in resultado.itens if i.situacao == "nao_medida"]
    if nao_medidas:
        linhas.append(f"Não medidas (limite de cota): {', '.join(nao_medidas)}")
    linhas.append(f"Modelo: {resultado.modelo} · Tempo: {resultado.segundos:.0f} s")
    return "\n".join(linhas)


def salvar(resultado: Resultado, pasta: Path, agora: datetime) -> Path:
    """Grava em `pasta/ia-avaliacao/` (dentro de `dados/`, fora do git). Sem a chave."""
    destino = Path(pasta) / "ia-avaliacao"
    destino.mkdir(parents=True, exist_ok=True)
    modelo = "".join(c if c.isalnum() or c in "-." else "_" for c in resultado.modelo) or "modelo"
    caminho = destino / f"{agora:%Y%m%d-%H%M%S}-{modelo}.json"
    certas, medidas = resultado.placar()
    conteudo = {
        "modelo": resultado.modelo,
        "quando": agora.isoformat(),
        "certas": certas,
        "medidas": medidas,
        "ajuste": list(resultado.placar("ajuste")),
        "prova": list(resultado.placar("prova")),
        "segundos": resultado.segundos,
        "frases": [
            {"id": i.frase.id, "grupo": i.frase.grupo, "situacao": i.situacao, "obtido": i.obtido,
             "diferencas": [{"campo": d.campo, "esperado": d.esperado, "obtido": d.obtido} for d in i.diferencas]}
            for i in resultado.itens
        ],
    }
    caminho.write_text(json.dumps(conteudo, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return caminho

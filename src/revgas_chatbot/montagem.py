"""Montagem do programa: o ÚNICO lugar que cria as peças e as liga.

Cria UMA `ApiCadenciada` e entrega a mesma instância a todos os fluxos
(conversa e registro de venda), para o limite de 1 requisição por segundo
valer entre eles. Nenhum outro módulo deve criar `ApiCadenciada`.

No C1 a API é a falsa, com clientes de demonstração fictícios (DDD 00, que
não existe no Brasil, então nunca é o número de uma pessoa real).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from revgas_chatbot.api import ApiRevGas, Relogio
from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_falsa import ApiRevGasFalsa
from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.conversa import Conversa, Dependencias
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.interpretador_gemini import InterpretadorGemini
from revgas_chatbot.registro import RegistradorDeVendas
from revgas_chatbot.relogio import RelogioDoSistema

PASTA_CONFIG = Path(__file__).resolve().parents[2] / "config"
ESTABELECIMENTO = 2580  # estabelecimento técnico da RevGás

# Clientes de demonstração (fictícios).
CLIENTES_DE_DEMONSTRACAO = [
    {
        "id": 501,
        "name": "Maria Exemplo",
        "phones": ["5500900000001"],
        "location": "Rua Exemplo",
        "number": "100",
        "neighborhood": "Centro",
        "city": "Teresina",
        "state_federation": "Piauí",
    },
    # 5500900000002 fica sem cadastro, para mostrar o cliente novo.
]


def _orientacoes_de_seguranca(arquivo: Path) -> str:
    """Texto do vazamento. Vazio ou sem o número dos Bombeiros (193) → erro."""
    texto = arquivo.read_text(encoding="utf-8").strip()
    if not texto or "193" not in texto:
        raise ValueError(
            f"{arquivo.name}: as orientações de segurança para vazamento estão vazias "
            "ou não têm o telefone dos Bombeiros (193)"
        )
    return texto


def api_de_demonstracao(relogio: Relogio) -> ApiRevGasFalsa:
    api = ApiRevGasFalsa(relogio=relogio)
    api.clientes.extend(dict(c) for c in CLIENTES_DE_DEMONSTRACAO)
    return api


@dataclass(frozen=True)
class Programa:
    """O programa montado: cria a conversa de um telefone e diz se a IA está ligada."""

    nova_conversa: Callable[[str], Conversa]
    ia_ligada: bool

    def __call__(self, telefone: str) -> Conversa:
        return self.nova_conversa(telefone)


def montar(
    pasta_dados: Path,
    *,
    relogio: Relogio | None = None,
    api: ApiRevGas | None = None,
    pasta_config: Path = PASTA_CONFIG,
    ambiente: Mapping[str, str] | None = None,
) -> Programa:
    """Liga as peças e devolve o programa.

    Configuração inválida (ex.: orientações de segurança vazias, ou a IA
    da cota grátis com a API de verdade) impede o programa de iniciar,
    com um erro claro.
    """
    ambiente = os.environ if ambiente is None else ambiente
    seguranca = _orientacoes_de_seguranca(pasta_config / "seguranca.txt")
    relogio = relogio or RelogioDoSistema()
    api = api if api is not None else api_de_demonstracao(relogio)
    interpretador = _interpretador(ambiente, api)
    cadenciada = ApiCadenciada(api, relogio)  # a única do programa
    catalogo = carregar_catalogo(pasta_config / "catalogo.json")
    pagamentos = carregar_pagamentos(pasta_config / "pagamentos.json")
    dependencias = Dependencias(
        api=cadenciada,
        registrador=RegistradorDeVendas(
            api=cadenciada,
            relogio=relogio,
            catalogo=catalogo,
            pagamentos_permitidos=pagamentos,
            establishment_id=ESTABELECIMENTO,
        ),
        fila=FilaDeAtendimento(Path(pasta_dados), relogio),
        catalogo=catalogo,
        pagamentos=pagamentos,
        seguranca=seguranca,
        interpretador=interpretador,
    )
    return Programa(
        nova_conversa=lambda telefone: Conversa(telefone, dependencias),
        ia_ligada=interpretador is not None,
    )


def _interpretador(ambiente: Mapping[str, str], api: ApiRevGas) -> InterpretadorGemini | None:
    """IA (C2d): ligada só se houver GEMINI_API_KEY no ambiente.

    Cota grátis do Gemini → o Google pode usar as mensagens (C2-1). Por isso
    só com a API falsa (clientes fictícios): com a API de verdade, não inicia.
    """
    chave = (ambiente.get("GEMINI_API_KEY") or "").strip()
    if not chave:
        return None
    if not isinstance(api, ApiRevGasFalsa):
        raise ValueError(
            "a IA da cota grátis do Gemini só pode ser usada com a API de teste "
            "(clientes fictícios); para clientes reais é preciso um plano pago"
        )
    return InterpretadorGemini(chave)

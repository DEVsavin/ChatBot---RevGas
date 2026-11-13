"""Montagem do programa: o ÚNICO lugar que cria as peças e as liga.

Cria UMA `ApiCadenciada` e entrega a mesma instância a todos os fluxos
(conversa e registro de venda), para o limite de 1 requisição por segundo
valer entre eles. Nenhum outro módulo deve criar `ApiCadenciada`.

No C1 a API é a falsa, com clientes de demonstração fictícios (DDD 00, que
não existe no Brasil, então nunca é o número de uma pessoa real).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from revgas_chatbot.api_revgas.api import ApiRevGas, Relogio
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa
from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.conversa.entrada import numero_ficticio, telefone
from revgas_chatbot.atendimento.fila import FilaDeAtendimento
from revgas_chatbot.ia.interpretacao import Interpretador
from revgas_chatbot.ia.interpretador_gemini import InterpretadorGemini
from revgas_chatbot.vendas.registro import RegistradorDeVendas, VendaReal
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


AVISO_VENDA_REAL = (
    "VENDA REAL LIGADA: cada pedido confirmado cria uma venda no estabelecimento "
    f"{ESTABELECIMENTO} (cliente de demonstração, sem avisar entregadores)."
)
MAX_EQUIPE = 5  # destinatários de teste da Meta (W12)


class ConfiguracaoInvalida(ValueError):
    """Mensagem com o NOME da variável, nunca o valor."""


@dataclass(frozen=True)
class ConfigVendaReal:
    """M5 (D16): venda real no 2580, ligada só com `--venda-real`."""

    url: str
    chave: str = field(repr=False)
    cliente_demo: int  # REVGAS_CLIENTE_DEMO: o cliente fixo enquanto /customers der 403


def config_venda_real(ambiente: Mapping[str, str]) -> ConfigVendaReal:
    valores = {n: (ambiente.get(n) or "").strip() for n in ("REVGAS_API_KEY", "REVGAS_API_URL", "REVGAS_CLIENTE_DEMO")}
    faltam = [nome for nome, valor in valores.items() if not valor]
    if faltam:
        raise ConfiguracaoInvalida("faltam no .env: " + ", ".join(faltam))
    if not valores["REVGAS_API_URL"].startswith("https://"):
        raise ConfiguracaoInvalida("REVGAS_API_URL precisa começar com https://")
    cliente = valores["REVGAS_CLIENTE_DEMO"]
    if not cliente.isdigit() or int(cliente) <= 0:
        raise ConfiguracaoInvalida("REVGAS_CLIENTE_DEMO: o número do cliente de demonstração, ex.: 5700355")
    return ConfigVendaReal(url=valores["REVGAS_API_URL"], chave=valores["REVGAS_API_KEY"], cliente_demo=int(cliente))


def numeros_da_equipe(ambiente: Mapping[str, str]) -> tuple[str, ...]:
    """WHATSAPP_NUMEROS_EQUIPE: até 5 telefones com DDD, separados por vírgula (pode faltar)."""
    brutos = [n for n in (ambiente.get("WHATSAPP_NUMEROS_EQUIPE") or "").split(",") if n.strip()]
    numeros = tuple(telefone(n) for n in brutos)
    if len(numeros) > MAX_EQUIPE or None in numeros:
        raise ConfiguracaoInvalida(
            f"WHATSAPP_NUMEROS_EQUIPE: de 1 a {MAX_EQUIPE} telefones com DDD, separados por vírgula"
        )
    return numeros  # type: ignore[return-value]


def _mesmo_celular(numero: str) -> str:
    """55 + DDD + os últimos 8 dígitos: iguala as formas com e sem o 9."""
    return numero[:4] + numero[-8:]


def regra_da_ia(numeros_equipe: list[str] | tuple[str, ...]) -> Callable[[str], bool]:
    """IA permitida para números fictícios (DDD 00) ou da equipe (com ou sem o 9). W2, D17b."""
    equipe = set()
    for bruto in numeros_equipe:
        numero = telefone(bruto)
        if numero is None:
            raise ValueError("número inválido na lista da equipe")
        equipe.add(_mesmo_celular(numero))

    def permitida(numero: str) -> bool:
        return numero_ficticio(numero) or _mesmo_celular(numero) in equipe

    return permitida


@dataclass(frozen=True)
class Programa:
    """O programa montado: cria conversas e diz se a IA e a venda real estão ligadas."""

    dependencias: Dependencias
    ia_ligada: bool
    venda_real: bool = False
    ia_permitida: Callable[[str], bool] = numero_ficticio

    def __call__(self, telefone: str, *, numero_confiavel: bool = True, com_ia: bool = True) -> Conversa:
        """`com_ia=False` cria a conversa sem a IA (H2: número real na web).

        Venda real: o número nunca busca cadastro (`/customers` dá 403; M5 10.3).
        """
        dependencias = self.dependencias if com_ia else replace(self.dependencias, interpretador=None)
        return Conversa(telefone, dependencias, numero_confiavel=numero_confiavel and not self.venda_real)


def montar(
    pasta_dados: Path,
    *,
    relogio: Relogio | None = None,
    api: ApiRevGas | None = None,
    pasta_config: Path = PASTA_CONFIG,
    ambiente: Mapping[str, str] | None = None,
    interpretador: Interpretador | None = None,  # testes: a IA falsa; senão, decide pelo ambiente
    ia_permitida: Callable[[str], bool] = numero_ficticio,  # WhatsApp: fictício ou equipe (W2)
    venda_real: ConfigVendaReal | None = None,  # M5: só com --venda-real
) -> Programa:
    """Liga as peças e devolve o programa.

    Configuração inválida (ex.: orientações de segurança vazias, ou a IA
    da cota grátis com a API de verdade) impede o programa de iniciar,
    com um erro claro.
    """
    ambiente = os.environ if ambiente is None else ambiente
    seguranca = _orientacoes_de_seguranca(pasta_config / "seguranca.txt")
    relogio = relogio or RelogioDoSistema()
    if api is None:
        if venda_real is not None:
            from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp

            api = ApiRevGasHttp(venda_real.url, venda_real.chave)
        else:
            api = api_de_demonstracao(relogio)
    if interpretador is None:
        interpretador = _interpretador(ambiente, api, venda_real=venda_real is not None)
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
            venda_real=VendaReal(cliente_fixo=venda_real.cliente_demo) if venda_real else None,
        ),
        fila=FilaDeAtendimento(Path(pasta_dados), relogio),
        catalogo=catalogo,
        pagamentos=pagamentos,
        seguranca=seguranca,
        interpretador=interpretador,
        # Toda IA deste programa é a da cota grátis: só para os telefones da regra
        # (padrão: fictícios; C2-1, H3, W2).
        ia_permitida=ia_permitida,
    )
    return Programa(
        dependencias=dependencias,
        ia_ligada=interpretador is not None,
        venda_real=venda_real is not None,
        ia_permitida=ia_permitida,
    )


def montar_do_ambiente(pasta_dados: Path, ambiente: Mapping[str, str], *, venda_real: bool) -> Programa:
    """Montagem dos comandos: lê a equipe (opcional) e, com `--venda-real`, a configuração real."""
    regra = regra_da_ia(numeros_da_equipe(ambiente))
    config = config_venda_real(ambiente) if venda_real else None
    return montar(pasta_dados, ambiente=ambiente, ia_permitida=regra, venda_real=config)


def _interpretador(ambiente: Mapping[str, str], api: ApiRevGas, *, venda_real: bool) -> InterpretadorGemini | None:
    """IA (C2d): ligada só se houver GEMINI_API_KEY no ambiente.

    Cota grátis do Gemini → o Google pode usar as mensagens (C2-1). Por isso
    só com a API falsa (clientes fictícios), ou na venda real (D17b), em que a
    regra `ia_permitida` limita a IA aos números fictícios e da equipe.
    Qualquer outro uso da API de verdade com o Gemini: não inicia.
    """
    chave = (ambiente.get("GEMINI_API_KEY") or "").strip()
    if not chave:
        return None
    if not isinstance(api, ApiRevGasFalsa) and not venda_real:
        raise ValueError(
            "a IA da cota grátis do Gemini só pode ser usada com a API de teste "
            "(clientes fictícios); para clientes reais é preciso um plano pago"
        )
    return InterpretadorGemini(chave)

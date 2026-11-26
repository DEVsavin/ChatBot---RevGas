"""Montagem do programa: o ÚNICO lugar que cria as peças e as liga.

Cria UMA `ApiCadenciada` e entrega a mesma instância a todos os fluxos
(catálogo, conversa, histórico, devolução e registro de venda), para o limite
de 1 requisição por segundo valer entre eles. Nenhum outro módulo deve criar
`ApiCadenciada`.

100% integrado à API RevGás (sem banco/arquivo local e sem LLM).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from revgas_chatbot.api_revgas.api import ApiRevGas, Relogio
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_revgas.api_falsa import ApiRevGasFalsa
from revgas_chatbot.api_revgas.api_http import MonitorApi
from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.conversa.conversa import Conversa, Dependencias
from revgas_chatbot.conversa.entrada import numero_ficticio, telefone
from revgas_chatbot.devolucao.devolucao import Devolucao
from revgas_chatbot.relogio import RelogioDoSistema
from revgas_chatbot.vendas.catalogo_api import carregar_catalogo_da_api
from revgas_chatbot.vendas.historico import HistoricoDeVendas
from revgas_chatbot.vendas.registro import RegistradorDeVendas, VendaReal

_log = logging.getLogger(__name__)

PASTA_CONFIG = Path(__file__).resolve().parents[2] / "config"
ESTABELECIMENTO = 2580  # estabelecimento técnico da RevGás

# Clientes e catálogo de demonstração (fictícios, para a API falsa).
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
PRODUTOS_DE_DEMONSTRACAO = [
    {"id": 26717, "description": "botijão de gás", "establishment_id": ESTABELECIMENTO},
]
ACORDOS_DE_DEMONSTRACAO = [
    {
        "id": 1,
        "product_id": 26717,
        "establishment_id": ESTABELECIMENTO,
        "suggested_value": "110.00",
        "minimum_value": "100.00",
        "is_default": True,
        "type": "PAYMENT_TYPE",
    },
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
    api.produtos.extend(dict(p) for p in PRODUTOS_DE_DEMONSTRACAO)
    api.acordos.extend(dict(a) for a in ACORDOS_DE_DEMONSTRACAO)
    return api


AVISO_VENDA_REAL = (
    "VENDA REAL LIGADA: clientes e vendas reais no estabelecimento "
    f"{ESTABELECIMENTO} (sem avisar entregadores)."
)
AVISO_ENTREGADORES = (
    "ATENÇÃO: cada pedido confirmado AVISA os entregadores do estabelecimento "
    f"{ESTABELECIMENTO} (broadcast NEW_SALE)."
)
MAX_EQUIPE = 5


class ConfiguracaoInvalida(ValueError):
    """Mensagem com o NOME da variável, nunca o valor."""


@dataclass(frozen=True)
class ConfigVendaReal:
    """Venda real no 2580, ligada só com `--venda-real`."""

    url: str
    chave: str = field(repr=False)
    cliente_demo: int | None = None  # opcional: se None, usa /v1/customers real
    avisar_entregadores: bool = False  # --avisar-entregadores: broadcast NEW_SALE


def config_venda_real(ambiente: Mapping[str, str]) -> ConfigVendaReal:
    valores = {n: (ambiente.get(n) or "").strip() for n in ("REVGAS_API_KEY", "REVGAS_API_URL")}
    faltam = [nome for nome, valor in valores.items() if not valor]
    if faltam:
        raise ConfiguracaoInvalida("faltam no .env: " + ", ".join(faltam))
    if not valores["REVGAS_API_URL"].startswith("https://"):
        raise ConfiguracaoInvalida("REVGAS_API_URL precisa começar com https://")
    return ConfigVendaReal(url=valores["REVGAS_API_URL"], chave=valores["REVGAS_API_KEY"], cliente_demo=None)


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


def regra_da_equipe(numeros_equipe: list[str] | tuple[str, ...]) -> Callable[[str], bool]:
    """Permite números fictícios (DDD 00) ou da equipe (com ou sem o 9)."""
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
    """O programa montado: cria conversas e diz se a venda real está ligada."""

    dependencias: Dependencias
    venda_real: bool = False
    avisar_entregadores: bool = False
    monitor_api: MonitorApi = field(default_factory=MonitorApi)

    def __call__(self, telefone: str, *, numero_confiavel: bool = True) -> Conversa:
        return Conversa(telefone, self.dependencias, numero_confiavel=numero_confiavel)


def montar(
    pasta_dados: Path | None = None,
    *,
    relogio: Relogio | None = None,
    api: ApiRevGas | None = None,
    pasta_config: Path = PASTA_CONFIG,
    ambiente: Mapping[str, str] | None = None,
    venda_real: ConfigVendaReal | None = None,
) -> Programa:
    """Liga as peças e devolve o programa (catálogo carregado da API RevGás, sem fila local)."""
    ambiente = os.environ if ambiente is None else ambiente
    _ = pasta_dados
    seguranca = _orientacoes_de_seguranca(pasta_config / "seguranca.txt")
    relogio = relogio or RelogioDoSistema()
    monitor = MonitorApi()
    api_injetada = api is not None
    if api is None:
        if venda_real is not None:
            from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp

            api = ApiRevGasHttp(venda_real.url, venda_real.chave, monitor=monitor)
        else:
            api = api_de_demonstracao(relogio)
    cadenciada = ApiCadenciada(api, relogio)  # a única do programa
    catalogo = None
    if not api_injetada and venda_real is None:
        # Mesmo em modo de teste para vendas, se houver REVGAS_API_KEY no ambiente,
        # carrega o catálogo REAL (somente leitura: GET /products + GET /deals).
        chave_real = (ambiente.get("REVGAS_API_KEY") or "").strip()
        url_real = (ambiente.get("REVGAS_API_URL") or "https://api.revgas.com/v1").strip()
        if chave_real and url_real.startswith("https://"):
            from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp

            catalogo = carregar_catalogo_da_api(
                ApiCadenciada(ApiRevGasHttp(url_real, chave_real, monitor=monitor), relogio),
                ESTABELECIMENTO,
            )
    if catalogo is None:
        catalogo = carregar_catalogo_da_api(cadenciada, ESTABELECIMENTO)
    if catalogo is None:
        _log.warning("Catálogo não veio da API RevGás; usando config/catalogo.json como fallback")
        catalogo = carregar_catalogo(pasta_config / "catalogo.json")
    pagamentos = carregar_pagamentos(pasta_config / "pagamentos.json")
    if isinstance(api, ApiRevGasFalsa) and not api.descricoes:
        api.descricoes.update({pid: produto.nome for pid, produto in catalogo.items()})
    cliente_fixo = venda_real.cliente_demo if venda_real else None
    historico = HistoricoDeVendas(
        api=cadenciada,
        relogio=relogio,
        cliente_fixo=cliente_fixo,
        catalogo=catalogo,
    )
    dependencias = Dependencias(
        api=cadenciada,
        registrador=RegistradorDeVendas(
            api=cadenciada,
            relogio=relogio,
            catalogo=catalogo,
            pagamentos_permitidos=pagamentos,
            establishment_id=ESTABELECIMENTO,
            venda_real=VendaReal(
                cliente_fixo=venda_real.cliente_demo,
                broadcast="NEW_SALE" if venda_real.avisar_entregadores else "NONE",
            )
            if venda_real
            else None,
        ),
        catalogo=catalogo,
        pagamentos=pagamentos,
        seguranca=seguranca,
        devolucao=Devolucao(
            api=cadenciada,
            relogio=relogio,
            cliente_fixo=cliente_fixo,
            catalogo=catalogo,
        ),
        historico=historico,
    )
    return Programa(
        dependencias=dependencias,
        venda_real=venda_real is not None,
        avisar_entregadores=bool(venda_real and venda_real.avisar_entregadores),
        monitor_api=monitor,
    )


def confirmar_aviso(ler: Callable[[str], str], escrever: Callable[[str], None], digitado: str | None = None) -> None:
    """--avisar-entregadores: mostra o aviso e só segue se a pessoa digitar AVISAR."""
    escrever(f"*** {AVISO_ENTREGADORES} ***")
    if digitado is None:
        digitado = ler("Para confirmar, digite AVISAR: ")
    if (digitado or "").strip() != "AVISAR":
        raise ConfiguracaoInvalida("confirmação não recebida: digite AVISAR para ligar o aviso aos entregadores")


def montar_do_ambiente(
    pasta_dados: Path | None, ambiente: Mapping[str, str], *, venda_real: bool, avisar_entregadores: bool = False
) -> Programa:
    """Montagem dos comandos: com `--venda-real`, lê a configuração real."""
    if avisar_entregadores and not venda_real:
        raise ConfiguracaoInvalida("--avisar-entregadores só funciona junto com --venda-real")
    config = config_venda_real(ambiente) if venda_real else None
    if config is not None and avisar_entregadores:
        config = replace(config, avisar_entregadores=True)
    return montar(pasta_dados, ambiente=ambiente, venda_real=config)

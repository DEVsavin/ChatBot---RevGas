"""Venda de teste na API real (M4), rodada pelo usuário no terminal dele.

    uv run --env-file .env python -m revgas_chatbot.vendas.venda_de_teste --broadcast NONE
    uv run --env-file .env python -m revgas_chatbot.vendas.venda_de_teste --sem-cliente
    uv run --env-file .env python -m revgas_chatbot.vendas.venda_de_teste --cancelar <id>

`--sem-cliente` (D12): a nossa chave recebe 403 em `/customers`. Este modo
não pede telefone nem toca em clientes: manda só o POST /sales do mesmo
formato, sem `customer_id`, para ver se a API aceita. Em timeout ou 5xx,
nunca reenvia: mostra a marca para conferir.

O que faz, nesta ordem:
1. pede o telefone sem mostrar na tela (o cliente da venda de teste é o
   próprio usuário, D9) e nunca o escreve na saída nem em arquivo;
2. busca o cliente em 4 formatos do telefone e mostra só QUANTOS achou em
   cada um (D8). Se achar só num formato diferente, para: um cadastro novo
   duplicaria o cliente;
3. registra a venda pelo mesmo caminho do chatbot (`RegistradorDeVendas`):
   2 botijões em 2 linhas `quantity: 1`, cartão de crédito, `broadcast:
   NONE` (D11: só NONE foi autorizado), endereço e observação de TESTE. Antes de CADA POST, mostra o que vai e pergunta
   "Enviar?". Sem "sim", nada sai. Todas as chamadas passam pela mesma
   `ApiCadenciada` (1 req/s na execução inteira);
4. busca de novo nos 4 formatos (o que a API guardou) e oferece o
   cancelamento (dúvida 8), que também pede "sim".
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any, Callable

from revgas_chatbot.api_revgas.api import Relogio, Resposta, TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.atendimento.atendimento import TransferirAtendente
from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.conversa.entrada import sim_ou_nao, telefone as ler_telefone
from revgas_chatbot.montagem import ESTABELECIMENTO, PASTA_CONFIG
from revgas_chatbot.vendas.pedido import Endereco, Item, Pagamento, Pedido
from revgas_chatbot.vendas.registro import PedidoInvalido, RegistradorDeVendas, VendaCriada
from revgas_chatbot.vendas.venda import marca, montar_corpo_venda, reais

PRODUTO = 26717
QUANTIDADE = 2
PAGAMENTO = "CARTAO_DE_CREDITO"
BROADCAST = "NONE"  # D11: única opção autorizada; omitir pode avisar entregador real (D5)
NOME = "Teste Chatbot Extensao"
ENDERECO = Endereco(
    logradouro="Rua de Teste do Chatbot",
    numero="S/N",
    bairro="Centro",
    cidade="Teresina",
    estado="PI",
    referencia="VENDA DE TESTE - NAO ENTREGAR",
)
PONTO_GENERICO = (-5.0892, -42.8016)  # centro de Teresina, como número (D6, 4º envio)
OBSERVACAO = "VENDA DE TESTE do chatbot (projeto de extensao). Nao entregar."

Perguntar = Callable[[str], str]
Escrever = Callable[[str], None]


class Recusado(Exception):
    """O usuário não autorizou a chamada: nada foi enviado."""


def _sem_numeros(texto: str) -> str:
    """Esconde sequências longas de dígitos (telefones) antes de mostrar."""
    return re.sub(r"\d{8,}", "[número]", texto)


def _mostrar_erro(rotulo: str, resposta: Resposta, escrever: Escrever) -> None:
    if resposta.status >= 300:
        detalhe = json.dumps(resposta.dados, ensure_ascii=False, default=str) if resposta.dados is not None else ""
        escrever(f"{rotulo}: HTTP {resposta.status} {_sem_numeros(detalhe)[:500]}")


class _ApiComConfirmacao:
    """Embrulha a API: GET passa direto; cada POST pede "sim" antes de sair."""

    def __init__(self, api: Any, perguntar: Perguntar, escrever: Escrever) -> None:
        self._api = api
        self._perguntar = perguntar
        self._escrever = escrever
        self.recusou_a_venda = False

    def buscar_clientes(self, telefone: str) -> Resposta:
        return self._api.buscar_clientes(telefone)

    def buscar_vendas(self, **filtros: Any) -> Resposta:
        return self._api.buscar_vendas(**filtros)

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta:
        self._autorizar(
            "POST /customers: cadastrar o seu telefone como cliente "
            f"(nome '{dados.get('name')}', endereço de teste). Enviar? (sim/não) "
        )
        resposta = self._api.cadastrar_cliente(dados)
        _mostrar_erro("POST /customers", resposta, self._escrever)
        return resposta

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta:
        corpo = {**corpo, "broadcast": BROADCAST}
        linhas = "; ".join(f"{p['product_id']} x {p['quantity']} a {reais(Decimal(str(p['value'])))}" for p in corpo["products"])
        pago = sum((Decimal(str(p["value"])) for p in corpo["payments"]), Decimal(0))
        cliente = f"cliente {corpo['customer_id']}" if "customer_id" in corpo else "sem cliente (sem customer_id)"
        try:
            self._autorizar(
                f"POST /sales, {cliente}: {len(corpo['products'])} linha(s) [{linhas}], "
                f"{PAGAMENTO} pagamento {reais(pago)}, broadcast {BROADCAST}, "
                + (f"lat/lon {corpo['lat']}, {corpo['lon']}, " if "lat" in corpo else "")
                + "endereço e observação de TESTE. Enviar? (sim/não) "
            )
        except Recusado:
            self.recusou_a_venda = True
            raise
        resposta = self._api.criar_venda(corpo)
        _mostrar_erro("POST /sales", resposta, self._escrever)
        if resposta.status in (400, 422):
            self._escrever(
                "A API recusou a venda. Se o motivo for o broadcast, não rode de novo sem o "
                "broadcast NONE: essa decisão volta para o usuário (D5)."
            )
        return resposta

    def _autorizar(self, pergunta: str) -> None:
        if sim_ou_nao(self._perguntar(pergunta)) is not True:
            raise Recusado


def formas_do_telefone(numero: str) -> dict[str, str]:
    """O mesmo telefone em até 4 formatos: com/sem 55, com/sem o 9º dígito."""
    formas = {"com 55": numero, "sem 55": numero[2:]}
    if len(numero) == 13 and numero[4] == "9":
        sem_o_9 = numero[:4] + numero[5:]
        formas.update({"sem o 9": sem_o_9, "sem 55 e sem o 9": sem_o_9[2:]})
    return formas


def _contar(api: Any, numero: str, escrever: Escrever, momento: str) -> dict[str, int | None]:
    contagem: dict[str, int | None] = {}
    for rotulo, forma in formas_do_telefone(numero).items():
        resposta = api.buscar_clientes(forma)
        dados = resposta.dados if isinstance(resposta.dados, dict) else {}
        contagem[rotulo] = len(dados.get("data") or []) if resposta.status == 200 else None
    partes = [f"{r}: {n if n is not None else 'erro'}" for r, n in contagem.items()]
    escrever(f"Clientes achados {momento} ({' · '.join(partes)})")
    return contagem


def _cancelar(api: Any, venda_id: int, perguntar: Perguntar, escrever: Escrever) -> None:
    pergunta = f"PUT /sales/{venda_id}/cancel com motive_cancel_sale_id nulo. Enviar? (sim/não) "
    if sim_ou_nao(perguntar(pergunta)) is not True:
        escrever("Cancelamento não enviado.")
        return
    resposta = api.cancelar_venda(venda_id)
    status = resposta.dados.get("status") if isinstance(resposta.dados, dict) else None
    escrever(f"Cancelamento: HTTP {resposta.status}" + (f", status {status}" if status else ""))
    _mostrar_erro("PUT /cancel", resposta, escrever)


def _pedido_de_teste(telefone: str, total: Decimal) -> Pedido:
    return Pedido(
        pedido_id=str(uuid.uuid4()),
        telefone=telefone,
        nome=NOME,
        itens=(Item(PRODUTO, QUANTIDADE),),
        endereco=ENDERECO,
        pagamentos=(Pagamento(PAGAMENTO, total),),
        observacao=OBSERVACAO,
    )


def _venda_sem_cliente(api: Any, relogio: Relogio, perguntar: Perguntar, escrever: Escrever) -> None:
    """D12: 1 POST /sales sem customer_id. Nunca reenvia."""
    catalogo = carregar_catalogo(PASTA_CONFIG / "catalogo.json")
    pedido = _pedido_de_teste("", catalogo[PRODUTO].preco * QUANTIDADE)
    corpo = montar_corpo_venda(pedido, customer_id=0, catalogo=catalogo, establishment_id=ESTABELECIMENTO)
    del corpo["customer_id"]
    # A API não soma linhas repetidas do mesmo produto (1º envio: 400, total 110).
    # 2º envio: 1 linha com quantity 2 a 110 e pagamento 220 (Dv5: 201 = value unitário;
    # 400 com total 110 = value é o total da linha). O M2 só muda depois do resultado.
    corpo["products"] = [{"product_id": PRODUTO, "quantity": QUANTIDADE, "value": catalogo[PRODUTO].preco}]
    # 2º envio: 400 "Dados da Entrega da Venda estão incompletos!". 3º envio: só acrescenta
    # lat/lon de um ponto genérico do centro de Teresina (não é o endereço de ninguém).
    # 3º envio como texto: mesmo 400. 4º envio: como número, como nas vendas reais.
    corpo["lat"], corpo["lon"] = PONTO_GENERICO
    confirmando = _ApiComConfirmacao(api, perguntar, escrever)
    incerto = (
        f"Resultado incerto. Não reenvie: confira no painel da RevGás se existe venda com a marca "
        f"{marca(pedido.pedido_id)}."
    )
    try:
        resposta = ApiCadenciada(confirmando, relogio).criar_venda(corpo)
    except Recusado:
        escrever("Nada foi enviado: você não autorizou a venda.")
        return
    except TempoEsgotado:
        escrever("Sem resposta da API. " + incerto)
        return
    if 500 <= resposta.status <= 599:
        escrever(incerto)
        return
    venda = resposta.dados if isinstance(resposta.dados, dict) else {}
    if resposta.status != 201 or "id" not in venda:
        if 200 <= resposta.status < 300:
            escrever(f"Resposta inesperada (HTTP {resposta.status}). " + incerto)
        return  # 4xx: a mensagem da API já foi mostrada
    escrever(
        f"Venda criada: id {venda['id']}, status {venda.get('status')}, "
        f"total {reais(Decimal(str(venda.get('total', 0))))}"
    )
    relogio.dormir(1.0)  # o cancelamento não passa pela ApiCadenciada: espera 1 s
    _cancelar(api, venda["id"], perguntar, escrever)


def rodar(
    argv: list[str] | None,
    *,
    api: Any,
    relogio: Relogio,
    perguntar: Perguntar,
    ler_segredo: Perguntar,
    escrever: Escrever,
) -> None:
    parser = argparse.ArgumentParser(description="Venda de teste na API real (só com o seu OK a cada envio)")
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--broadcast", choices=[BROADCAST], help="sempre NONE: não notificar entregadores (D11)")
    modo.add_argument("--sem-cliente", action="store_true", help="venda sem customer_id, sem telefone (D12)")
    modo.add_argument("--cancelar", type=int, metavar="ID", help="só tentar cancelar a venda ID")
    argumentos = parser.parse_args(argv)

    if argumentos.cancelar is not None:
        _cancelar(api, argumentos.cancelar, perguntar, escrever)
        return
    if argumentos.sem_cliente:
        _venda_sem_cliente(api, relogio, perguntar, escrever)
        return

    numero = ler_telefone(ler_segredo("Seu telefone com DDD (não aparece na tela): "))
    if numero is None:
        escrever("Telefone inválido. Nada foi enviado.")
        return

    # UMA ApiCadenciada para a execução inteira: 1 req/s entre contagem, cadastro e venda.
    # Os GETs passam direto pela confirmação; só POST pede "sim".
    confirmando = _ApiComConfirmacao(api, perguntar, escrever)
    cadenciada = ApiCadenciada(confirmando, relogio)
    antes = _contar(cadenciada, numero, escrever, "antes")
    if not antes["com 55"] and any(antes.values()):
        escrever("Achei você em outro formato do telefone: um cadastro novo duplicaria o cliente. Parei. Nada foi enviado.")
        return

    catalogo = carregar_catalogo(PASTA_CONFIG / "catalogo.json")
    registrador = RegistradorDeVendas(
        api=cadenciada,
        relogio=relogio,
        catalogo=catalogo,
        pagamentos_permitidos=carregar_pagamentos(PASTA_CONFIG / "pagamentos.json"),
        establishment_id=ESTABELECIMENTO,
    )
    pedido = _pedido_de_teste(numero, catalogo[PRODUTO].preco * QUANTIDADE)

    try:
        resultado = registrador.registrar(pedido)
    except Recusado:
        escrever("Nada foi enviado: você não autorizou o cadastro.")
        return
    if confirmando.recusou_a_venda:
        escrever("Nada foi enviado ao POST /sales: você não autorizou a venda.")
        return

    if isinstance(resultado, VendaCriada):
        escrever(f"Venda criada: id {resultado.id}, status {resultado.status}, total {reais(resultado.total)}")
    elif isinstance(resultado, TransferirAtendente):
        escrever(f"A venda não foi criada: {_sem_numeros(resultado.motivo)}")
        if resultado.venda_pode_existir:
            escrever(f"Atenção: pode existir venda com a marca [chatbot:{pedido.pedido_id}].")
    elif isinstance(resultado, PedidoInvalido):
        escrever("A API recusou dados do pedido: " + ", ".join(p.campo for p in resultado.problemas))
    else:
        escrever(f"Resultado: {type(resultado).__name__}")

    _contar(cadenciada, numero, escrever, "depois")
    if isinstance(resultado, VendaCriada):
        relogio.dormir(1.0)  # o cancelamento não passa pela ApiCadenciada: espera 1 s
        _cancelar(api, resultado.id, perguntar, escrever)


def main(argv: list[str] | None = None) -> None:
    from revgas_chatbot.api_revgas.api_http import ApiRevGasHttp
    from revgas_chatbot.relogio import RelogioDoSistema

    url = os.environ.get("REVGAS_API_URL", "")
    if not url.startswith("https://"):
        raise SystemExit("REVGAS_API_URL precisa começar com https:// (rode com --env-file .env)")
    api = ApiRevGasHttp(url, os.environ.get("REVGAS_API_KEY", ""))
    rodar(argv, api=api, relogio=RelogioDoSistema(), perguntar=input, ler_segredo=getpass.getpass, escrever=print)


if __name__ == "__main__":
    main()

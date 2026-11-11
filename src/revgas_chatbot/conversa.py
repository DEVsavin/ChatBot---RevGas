"""Conversa de pedido (C1): o motor do atendimento, sem canal.

`Conversa.iniciar()` e `Conversa.responder(texto)` devolvem a lista de
mensagens do chatbot. Quem lê o teclado ou o WhatsApp e mostra as
mensagens é o canal (ex.: `chat_terminal.py`), não este módulo.

Roteiro fixo, uma pergunta por vez (sem modelo de linguagem no C1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from revgas_chatbot.api import TempoEsgotado
from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.entrada import (
    normalizar,
    opcao,
    sim_ou_nao,
    telefone,
    telefone_legivel,
    valor_em_reais,
)
from revgas_chatbot.estados import nome_do_estado
from revgas_chatbot.fila import FilaDeAtendimento
from revgas_chatbot.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.registro import RegistradorDeVendas, VendaCriada
from revgas_chatbot.venda import reais

MAX_QUANTIDADE = 10  # C1-D2

# Rótulos em português, na ordem em que aparecem para o cliente.
ROTULOS_DE_PAGAMENTO = {
    "PIX": "PIX",
    "MONEY": "Dinheiro",
    "CARTAO_DE_DEBITO": "Cartão de débito",
    "CARTAO_DE_CREDITO": "Cartão de crédito",
}


OPCOES_DO_RESUMO = ["confirmar", "corrigir", "cancelar"]

# Explicações quando a resposta não serve (a pergunta é repetida em seguida).
SIM_OU_NAO = "Responda sim ou não."
PERGUNTA_DO_NUMERO = "Qual é o seu número com DDD?"
PERGUNTA_DO_TROCO = "Vai precisar de troco? Responda o valor (ex.: 200) ou 'não'."
DINHEIRO = "MONEY"
DA_LISTA = "Escolha um número da lista."


# Endereço campo a campo: (campo do Endereco, pergunta, opcional?, explicação se não servir).
CAMPOS_DO_ENDERECO = [
    ("logradouro", "Qual é a rua?", False, "Preciso da rua para a entrega."),
    ("numero", "Qual é o número? (ou S/N)", False, "Preciso do número (ou S/N)."),
    ("bairro", "Qual é o bairro?", False, "Preciso do bairro para a entrega."),
    ("cidade", "Qual é a cidade?", False, "Preciso da cidade para a entrega."),
    ("estado", "Qual é o estado? (ex.: PI ou Piauí)", False,
     "Não reconheci o estado. Envie a sigla (PI) ou o nome (Piauí)."),
    ("complemento", "Complemento? (ou 'pular')", True, ""),
    ("referencia", "Ponto de referência? (ou 'pular')", True, ""),
]


@dataclass(frozen=True)
class Dependencias:
    api: ApiCadenciada  # a MESMA instância usada pelo registrador
    registrador: RegistradorDeVendas
    fila: FilaDeAtendimento
    catalogo: dict[int, ProdutoCatalogo]
    pagamentos: set[str]


class Conversa:
    def __init__(self, telefone_do_canal: str, dependencias: Dependencias) -> None:
        self._telefone = telefone_do_canal
        self._d = dependencias
        self._passo = "CONFIRMAR_NUMERO"
        self._nome = ""
        self._endereco: Endereco | None = None
        self._endereco_do_cadastro: Endereco | None = None
        self._produto_id: int | None = None
        self._quantidade = 0
        self._pagamento = ""
        self._troco: Decimal | None = None
        self._pedido_id = str(uuid.uuid4())
        self._produtos = list(self._d.catalogo.items())
        self._formas = [t for t in ROTULOS_DE_PAGAMENTO if t in self._d.pagamentos]

    # --- interface -------------------------------------------------------------

    def iniciar(self) -> list[str]:
        return [
            "Olá! Sou o atendente virtual da revenda de gás. Vou anotar seu pedido.",
            self._pergunta_numero(),
        ]

    def responder(self, texto: str) -> list[str]:
        return getattr(self, f"_passo_{self._passo.lower()}")(texto)

    # --- passos ------------------------------------------------------------------

    def _passo_confirmar_numero(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_numero()]
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        clientes = self._buscar_cliente_do_canal()
        if clientes is None or len(clientes) > 1:
            return self._transferir(
                "Não consegui identificar seu cadastro agora.",
                "Cliente não identificado com segurança pelo número do canal.",
            )
        if not clientes:
            self._passo = "NOME"
            return ["Qual é o seu nome?"]
        [cliente] = clientes
        self._nome = cliente["name"]
        self._endereco_do_cadastro = _endereco_do_cadastro(cliente)
        self._passo = "PRODUTO"
        return [f"Olá, {self._nome.split()[0]}!", self._pergunta_produto()]

    def _passo_digitar_numero(self, texto: str) -> list[str]:
        numero = telefone(texto)
        if numero is None:
            return ["Não entendi o número. Envie com DDD, por exemplo (86) 99999-0000.", PERGUNTA_DO_NUMERO]
        self._telefone = numero
        self._passo = "CONFIRMAR_NUMERO_DIGITADO"
        return [self._pergunta_numero_digitado()]

    def _passo_confirmar_numero_digitado(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_numero_digitado()]
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        # V1: número digitado NÃO busca o cadastro (não revela nome nem endereço
        # de quem é dono do número). O registrador acha o cliente ao registrar.
        self._passo = "NOME"
        return ["Qual é o seu nome?"]

    def _passo_nome(self, texto: str) -> list[str]:
        nome = " ".join(texto.split())
        if not nome:
            return ["Preciso do seu nome para o pedido.", "Qual é o seu nome?"]
        self._nome = nome
        self._passo = "PRODUTO"
        return [f"Olá, {nome.split()[0]}!", self._pergunta_produto()]

    def _passo_produto(self, texto: str) -> list[str]:
        indice = opcao(texto, [p.nome for _, p in self._produtos])
        if indice is None:
            return [DA_LISTA, self._pergunta_produto()]
        self._produto_id = self._produtos[indice][0]
        self._passo = "QUANTIDADE"
        return ["Quantos?"]

    def _passo_quantidade(self, texto: str) -> list[str]:
        quantidade = texto.strip()
        if not quantidade.isdigit() or not 1 <= int(quantidade) <= MAX_QUANTIDADE:
            return [f"Envie um número de 1 a {MAX_QUANTIDADE}.", "Quantos?"]
        self._quantidade = int(quantidade)
        if self._endereco_do_cadastro is not None:
            self._passo = "ENDERECO_DO_CADASTRO"
            return [self._pergunta_endereco_do_cadastro()]
        return self._comecar_endereco()

    def _passo_endereco_do_cadastro(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_endereco_do_cadastro()]
        if resposta is False:
            return self._comecar_endereco()
        self._endereco = self._endereco_do_cadastro
        self._passo = "PAGAMENTO"
        return [self._pergunta_pagamento()]

    def _comecar_endereco(self) -> list[str]:
        self._campos: dict[str, str | None] = {}
        self._passo = "CAMPO_DO_ENDERECO"
        return ["Agora o endereço de entrega.", self._pergunta_do_campo()]

    def _passo_campo_do_endereco(self, texto: str) -> list[str]:
        campo, pergunta, opcional, explicacao = CAMPOS_DO_ENDERECO[len(self._campos)]
        valor = " ".join(texto.split())
        if opcional and normalizar(valor) == "pular":
            valor = None
        elif not valor:
            return [explicacao, pergunta]
        elif campo == "estado":
            valor = nome_do_estado(valor)
            if valor is None:
                return [explicacao, pergunta]
        self._campos[campo] = valor
        if len(self._campos) < len(CAMPOS_DO_ENDERECO):
            return [self._pergunta_do_campo()]
        self._endereco = Endereco(**self._campos)
        self._passo = "PAGAMENTO"
        return [self._pergunta_pagamento()]

    def _passo_pagamento(self, texto: str) -> list[str]:
        indice = opcao(texto, [ROTULOS_DE_PAGAMENTO[t] for t in self._formas])
        if indice is None:
            return [DA_LISTA, self._pergunta_pagamento()]
        self._pagamento = self._formas[indice]
        self._troco = None
        if self._pagamento == DINHEIRO:
            self._passo = "TROCO"
            return [PERGUNTA_DO_TROCO]
        self._passo = "RESUMO"
        return [self._resumo()]

    def _passo_troco(self, texto: str) -> list[str]:
        if sim_ou_nao(texto) is False:
            self._troco = None
        else:
            valor = valor_em_reais(texto)
            if valor is None or valor < self._total():
                return [
                    f"O troco precisa ser para pelo menos {reais(self._total())}, ou responda 'não'.",
                    PERGUNTA_DO_TROCO,
                ]
            self._troco = valor
        self._passo = "RESUMO"
        return [self._resumo()]

    def _passo_resumo(self, texto: str) -> list[str]:
        if opcao(texto, OPCOES_DO_RESUMO) is None:
            return [DA_LISTA, self._resumo()]
        resultado = self._d.registrador.registrar(self._pedido())
        assert isinstance(resultado, VendaCriada)
        self._passo = "ENCERRADA"
        return [f"Pedido registrado! Número {resultado.id}, total {reais(resultado.total)}."]

    # --- cliente e transferência ---------------------------------------------------

    def _buscar_cliente_do_canal(self) -> list[dict] | None:
        """Clientes com o número do canal; None se a resposta não for confiável."""
        try:
            resposta = self._d.api.buscar_clientes(self._telefone)
        except TempoEsgotado:
            return None
        dados = resposta.dados if resposta.status == 200 else None
        clientes = dados.get("data") if isinstance(dados, dict) else None
        if not isinstance(clientes, list):
            return None
        if not all(isinstance(c, dict) and isinstance(c.get("id"), int) for c in clientes):
            return None
        return clientes

    def _transferir(self, aviso_ao_cliente: str, resumo: str) -> list[str]:
        """Abre a solicitação ATENDIMENTO na fila e encerra a conversa."""
        protocolo = self._d.fila.abrir("ATENDIMENTO", self._telefone, resumo)
        self._passo = "ENCERRADA"
        return [
            f"{aviso_ao_cliente} Uma pessoa da nossa equipe vai continuar o atendimento. "
            f"Protocolo {protocolo}."
        ]

    # --- textos e montagem -------------------------------------------------------

    def _pergunta_do_campo(self) -> str:
        return CAMPOS_DO_ENDERECO[len(self._campos)][1]

    def _pergunta_numero_digitado(self) -> str:
        return f"É este: {telefone_legivel(self._telefone)}? (sim/não)"

    def _pergunta_numero(self) -> str:
        return f"Este número é seu: {telefone_legivel(self._telefone)}? (sim/não)"

    def _pergunta_endereco_do_cadastro(self) -> str:
        e = self._endereco_do_cadastro
        return f"Entregar em {e.logradouro}, {e.numero} — {e.bairro}, {e.cidade}? (sim/não)"

    def _pergunta_produto(self) -> str:
        linhas = [f"{n}) {p.nome} — {reais(p.preco)}" for n, (_, p) in enumerate(self._produtos, 1)]
        return "O que você quer pedir?\n" + "\n".join(linhas)

    def _pergunta_pagamento(self) -> str:
        linhas = [f"{n}) {ROTULOS_DE_PAGAMENTO[t]}" for n, t in enumerate(self._formas, 1)]
        return "Como vai pagar?\n" + "\n".join(linhas)

    def _total(self) -> Decimal:
        return self._d.catalogo[self._produto_id].preco * self._quantidade

    def _resumo(self) -> str:
        produto = self._d.catalogo[self._produto_id]
        e = self._endereco
        return "\n".join(
            [
                "Confira seu pedido:",
                f"Nome: {self._nome}",
                f"Telefone: {telefone_legivel(self._telefone)}",
                f"{self._quantidade}x {produto.nome} — {reais(self._total())}",
                f"Entrega: {e.logradouro}, {e.numero} — {e.bairro}, {e.cidade}, {e.estado}",
                f"Pagamento: {self._descricao_do_pagamento()}",
                "Responda: 1) confirmar  2) corrigir  3) cancelar",
            ]
        )

    def _descricao_do_pagamento(self) -> str:
        rotulo = ROTULOS_DE_PAGAMENTO[self._pagamento]
        if self._pagamento != DINHEIRO:
            return rotulo
        return f"{rotulo}, troco para {reais(self._troco)}" if self._troco else f"{rotulo}, sem troco"

    def _pedido(self) -> Pedido:
        return Pedido(
            pedido_id=self._pedido_id,
            telefone=self._telefone,
            nome=self._nome,
            itens=(Item(product_id=self._produto_id, quantidade=self._quantidade),),
            endereco=self._endereco,
            pagamentos=(Pagamento(tipo=self._pagamento, valor=self._total()),),
            troco_para=self._troco,
        )


_CAMPOS_OBRIGATORIOS_DO_CADASTRO = ("location", "number", "neighborhood", "city", "state_federation")


def _endereco_do_cadastro(cliente: dict) -> Endereco | None:
    """Endereço do cadastro, se estiver completo; senão None (pergunta campo a campo)."""
    if not all(isinstance(cliente.get(c), str) and cliente[c].strip() for c in _CAMPOS_OBRIGATORIOS_DO_CADASTRO):
        return None
    return Endereco(
        logradouro=cliente["location"],
        numero=cliente["number"],
        bairro=cliente["neighborhood"],
        cidade=cliente["city"],
        estado=cliente["state_federation"],
        complemento=cliente.get("complement") or None,
        referencia=cliente.get("landmark") or None,
    )

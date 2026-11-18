"""Conversa de pedido (C1): o motor do atendimento, sem canal.

`Conversa.iniciar()` e `Conversa.responder(texto)` devolvem a lista de
mensagens do chatbot. Quem lê o teclado ou o WhatsApp e mostra as
mensagens é o canal (ex.: `chat_terminal.py`), não este módulo.

Roteiro fixo, uma pergunta por vez. Com um interpretador (C2), a IA é
consultada só quando o roteiro não entendeu: ela sugere, o código decide.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from revgas_chatbot.api_revgas.api import TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.conversa.entrada import (
    com_nono_digito,
    comando,
    fala_de_apartamento,
    ja_informado,
    quantidade_do_texto,
    rua_do_texto,
    PedidoDeDevolucao,
    pedido_de_devolucao,
    normalizar,
    numero_do_endereco,
    opcao,
    pergunta_ou_nao_sei,
    sim_ou_nao,
    telefone,
    telefone_legivel,
    valor_em_reais,
)
from revgas_chatbot.conversa.estados import nome_do_estado
from revgas_chatbot.devolucao.devolucao import (
    PREFERENCIAS,
    REGRAS,
    Devolucao,
    NenhumPedido,
    PedidoDoCliente,
    PedidosEncontrados,
    SolicitacaoAberta,
)
from revgas_chatbot.atendimento.fila import FilaDeAtendimento, mascarar
from revgas_chatbot.ia.interpretacao import (
    Contexto,
    Interpretador,
    PedidoInterpretado,
    validar_interpretacao,
)
from revgas_chatbot.vendas.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.atendimento.atendimento import TransferirAtendente
from revgas_chatbot.vendas.registro import EmAndamento, PedidoInvalido, RegistradorDeVendas, VendaCriada
from revgas_chatbot.vendas.venda import marca, reais

MAX_QUANTIDADE = 10  # C1-D2

_log = logging.getLogger(__name__)

# Rótulos em português, na ordem em que aparecem para o cliente.
ROTULOS_DE_PAGAMENTO = {
    "PIX": "PIX",
    "MONEY": "Dinheiro",
    "CARTAO_DE_DEBITO": "Cartão de débito",
    "CARTAO_DE_CREDITO": "Cartão de crédito",
}


OPCOES_DO_RESUMO = ["confirmar", "corrigir", "cancelar"]

# Passos em que o cliente digita texto livre: comandos (exceto vazamento)
# só valem se forem a mensagem inteira (V3).
PASSOS_DE_TEXTO_LIVRE = frozenset({"NOME", "CAMPO_DO_ENDERECO", "DEV_TEXTO"})

# Devolução na conversa (fase C, spec-conversa.md).
# Passos com um pedido em andamento (rascunho ainda não confirmado).
PASSOS_DO_RASCUNHO = frozenset({"PRODUTO", "QUANTIDADE", "ENDERECO_DO_CADASTRO", "CAMPO_DO_ENDERECO", "PAGAMENTO", "TROCO"})
OPCOES_DO_RASCUNHO = ["continuar este pedido", "desistir deste pedido", "falar de um pedido já feito"]
PERGUNTA_DO_RASCUNHO = (
    "Você está no meio de um pedido. Quer:\n1) continuar este pedido\n"
    "2) desistir deste pedido (nada foi enviado)\n3) falar de um pedido já feito (cancelar, problema ou cobrança)"
)
RASCUNHO_DESCARTADO = "O pedido que você estava fazendo foi descartado (nada tinha sido enviado)."
OPCOES_DA_DEVOLUCAO = ["cancelar um pedido", "problema com o produto", "cobrança ou devolução de dinheiro",
                       "falar com um atendente"]
MENU_DA_DEVOLUCAO = "Posso ajudar com:\n" + "\n".join(f"{n}) {o}" for n, o in enumerate(OPCOES_DA_DEVOLUCAO, 1))
PERGUNTA_DA_PREFERENCIA = (
    "Se houver valor a devolver, você prefere:\n1) crédito na próxima compra\n2) dinheiro de volta"
)
ENSINA_O_POS_VENDA = "Para cancelar, escreva cancelar. Para outra coisa, escreva atendente."  # ticket 09
ACAO_DA_DEVOLUCAO = {
    "CANCELAMENTO": "cancelar o pedido",
    "ESTORNO": "devolver o valor do pedido",
    "PROBLEMA_PRODUTO": "verificar um problema no produto",
}

# Explicações quando a resposta não serve (a pergunta é repetida em seguida).
SIM_OU_NAO = "Responda sim ou não."
PERGUNTA_DO_NUMERO = "Qual é o seu número com DDD?"
O_QUE_CORRIGIR = ["produto", "endereço", "pagamento", "nome"]
PERGUNTA_DO_QUE_CORRIGIR = "O que quer corrigir?\n" + "\n".join(
    f"{n}) {item}" for n, item in enumerate(O_QUE_CORRIGIR, 1)
)
PERGUNTA_DO_TROCO = "Vai precisar de troco? Responda o valor (ex.: 200) ou 'não'."
DINHEIRO = "MONEY"
NAO_REFACA_O_PEDIDO = "Não faça um novo pedido: nossa equipe vai verificar se ele foi registrado."
TENTE_DE_NOVO = (
    "Não consegui registrar sua solicitação agora. "
    "Tente de novo em alguns minutos ou ligue para a revenda."
)
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


# Ticket 05 (Q4): exemplo do que se espera em cada pergunta (2ª recusa seguida).
_OPCAO = "envie só o número da opção, como 1"
EXEMPLOS_DAS_PERGUNTAS = {
    "CONFIRMAR_NUMERO": "responda sim ou não",
    "CONFIRMAR_NUMERO_DIGITADO": "responda sim ou não",
    "DIGITAR_NUMERO": "(86) 99999-0000",
    "NOME": "Maria Silva",
    "PRODUTO": _OPCAO,
    "QUANTIDADE": "2",
    "ENDERECO_DO_CADASTRO": "responda sim ou não",
    "PAGAMENTO": _OPCAO,
    "TROCO": "200, ou não",
    "RESUMO": _OPCAO,
    "CORRIGIR": _OPCAO,
    "RASCUNHO_OU_PEDIDO": _OPCAO,
    "DEV_MENU": _OPCAO,
    "DEV_PEDIDO": _OPCAO,
    "DEV_MOTIVO": _OPCAO,
    "DEV_TEXTO": "o botijão veio sem lacre",
    "DEV_PAGOU": "responda sim ou não",
    "DEV_PREFERENCIA": _OPCAO,
    "DEV_CONFIRMAR": "1 para confirmar ou 2 para não registrar",
}
EXEMPLOS_DO_ENDERECO = {
    "logradouro": "Rua das Flores",
    "numero": "123, ou S/N",
    "bairro": "Centro",
    "cidade": "Teresina",
    "estado": "PI",
    "complemento": "Apto 12, ou pular",
    "referencia": "perto da praça, ou pular",
}

# A1: como cada campo aparece na explicação ("Me diga <rótulo> (ex.: <exemplo>)").
ROTULOS_DOS_CAMPOS = {
    "logradouro": ("o nome da rua", "Rua das Flores"),
    "numero": ("o número", "120, ou S/N"),
    "bairro": ("o bairro", "Centro"),
    "cidade": ("a cidade", "Teresina"),
    "complemento": ("o complemento", "Apto 12, ou 'pular'"),
    "referencia": ("um ponto de referência", "perto da praça, ou 'pular'"),
}


@dataclass(frozen=True)
class Dependencias:
    api: ApiCadenciada  # a MESMA instância usada pelo registrador
    registrador: RegistradorDeVendas
    fila: FilaDeAtendimento
    catalogo: dict[int, ProdutoCatalogo]
    pagamentos: set[str]
    seguranca: str  # orientações para vazamento (config/seguranca.txt)
    interpretador: Interpretador | None = None  # IA (C2); None = só o roteiro do C1
    # Cota grátis (H3): a IA só vale enquanto o telefone ATUAL da conversa passar nesta regra
    # (web: só fictício; WhatsApp: fictício ou da equipe). None = sem restrição (testes).
    ia_permitida: Callable[[str], bool] | None = None
    devolucao: Devolucao | None = None  # devolução na conversa (fase C); None = só transfere


class Conversa:
    def __init__(
        self,
        telefone_do_canal: str,
        dependencias: Dependencias,
        *,
        numero_confiavel: bool = True,
    ) -> None:
        """`numero_confiavel=False`: o número veio de alguém que pode digitar
        qualquer um (ex.: a página web com número real). A conversa o trata
        como número digitado (V1): não busca nem revela o cadastro."""
        self._telefone_do_canal = com_nono_digito(telefone_do_canal)  # problema 3: mostra e salva com o 9
        self._numero_confiavel = numero_confiavel
        self._d = dependencias
        self._ultima_venda_id: int | None = None  # para "quero cancelar" depois da venda
        self._recusas_seguidas = 0  # ticket 05: a mesma pergunta recusada seguidas vezes
        self._rua_recusada: str | None = None  # ticket 01: a mesma resposta de novo vira a rua
        self._tentativa_de_devolucao: str | None = None  # ticket 09: contexto para o atendente
        self._travou_em: str | None = None  # ticket 09: a pergunta em que a pessoa travou
        self._produtos = list(self._d.catalogo.items())
        self._formas = [t for t in ROTULOS_DE_PAGAMENTO if t in self._d.pagamentos]
        self._novo_rascunho()

    def _novo_rascunho(self) -> None:
        """Estado de um pedido novo. Um rascunho = um pedido_id (a trava do registrador)."""
        self._telefone = self._telefone_do_canal
        self._passo = "CONFIRMAR_NUMERO"
        self._corrigindo = False
        self._nome = ""
        self._endereco: Endereco | None = None
        self._endereco_do_cadastro: Endereco | None = None
        self._produto_id: int | None = None
        self._quantidade = 0
        self._pagamento = ""
        self._troco: Decimal | None = None
        self._troco_respondido = False
        self._pedido_id = str(uuid.uuid4())
        # Problemas 1 e 2: partes do endereço que a IA achou no que o cliente escreveu.
        self._enderecos_ditos: dict[str, str] = {}  # tudo o que apareceu na conversa ("já informei")
        self._endereco_do_texto: dict[str, str] | None = None  # dito junto do pedido: usado direto

    # --- interface -------------------------------------------------------------

    @property
    def encerrada(self) -> bool:
        return self._passo == "ENCERRADA"

    def iniciar(self) -> list[str]:
        return [
            "Olá! Sou o atendente virtual da revenda de gás. Vou anotar seu pedido.",
            self._pergunta_numero(),
        ]

    def responder(self, texto: str) -> list[str]:
        """Responde a mensagem. Ticket 05 (Q4): na 2ª recusa seguida da mesma pergunta,
        acrescenta um exemplo e lembra do atendente (nunca transfere sozinho)."""
        antes = self._chave_da_pergunta()
        respostas = self._responder(texto)
        if self._chave_da_pergunta() == antes and self._passo not in ("ENCERRADA", "POS_VENDA"):
            self._recusas_seguidas += 1
        else:
            self._recusas_seguidas = 0
        self._travou_em = respostas[-1] if self._recusas_seguidas >= 2 and respostas else None
        if self._recusas_seguidas >= 2 and respostas:
            exemplo = self._exemplo_da_pergunta()
            ajuda = (f"Exemplo: {exemplo} — " if exemplo else "") + "ou escreva atendente para falar com uma pessoa."
            ajuda = ajuda[:1].upper() + ajuda[1:]
            respostas = [*respostas[:-1], ajuda, respostas[-1]]
        return respostas

    def _contexto_para_a_equipe(self) -> list[str]:
        """Ticket 09 (Q2): o que a equipe precisa saber ao receber o repasse (vazio = como antes)."""
        linhas = []
        if self._ultima_venda_id is not None:
            linhas.append(f"Último pedido: {self._ultima_venda_id}.")
        if self._tentativa_de_devolucao:
            linhas.append(self._tentativa_de_devolucao)
        if self._travou_em:
            linhas.append(f"Travou na pergunta: {self._travou_em}")
        return linhas

    def _chave_da_pergunta(self) -> tuple:
        if self._passo == "CAMPO_DO_ENDERECO":
            try:
                return (self._passo, self._campo_atual()[0])
            except StopIteration:
                return (self._passo, None)
        return (self._passo,)

    def _exemplo_da_pergunta(self) -> str | None:
        if self._passo == "CAMPO_DO_ENDERECO":
            return EXEMPLOS_DO_ENDERECO.get(self._chave_da_pergunta()[1])
        return EXEMPLOS_DAS_PERGUNTAS.get(self._passo)

    def _responder(self, texto: str) -> list[str]:
        # Vazamento tem prioridade sempre, em qualquer passo (inclusive texto livre).
        pedido = comando(texto, livre=self._passo in PASSOS_DE_TEXTO_LIVRE)
        if pedido == "VAZAMENTO":
            return self._vazamento(texto)
        if self._passo.startswith("DEV_") or self._passo == "RASCUNHO_OU_PEDIDO":
            # Dentro da devolução, as respostas são da lista; só atendente e sair valem como comando.
            if pedido in ("ATENDENTE", "SAIR"):
                return self._comando(pedido, texto)
            return getattr(self, f"_passo_{self._passo.lower()}")(texto)
        # Nos passos de opção, a resposta válida do passo vem antes do comando (V3):
        # no resumo, "cancelar" é a opção 3.
        resposta_do_passo = self._passo == "RESUMO" and opcao(texto, OPCOES_DO_RESUMO) is not None
        if not resposta_do_passo:
            devolucao = self._devolucao_pedida(texto, pedido)
            if devolucao is not None:
                return devolucao
        if pedido and not resposta_do_passo:
            return self._comando(pedido, texto)
        return getattr(self, f"_passo_{self._passo.lower()}")(texto)

    def _comando(self, pedido: str, texto: str) -> list[str]:
        if pedido == "SAIR":
            self._passo = "ENCERRADA"
            return ["Tudo bem, até mais!"]
        if pedido == "CANCELAR_OU_DEVOLVER" and self._passo in ("RESUMO", "CORRIGIR"):
            self._passo = "ENCERRADA"  # nada foi enviado ainda: só descarta o rascunho
            return ["Pedido descartado. Nada foi enviado."]
        motivo = "Cliente quer cancelar ou devolver." if pedido == "CANCELAR_OU_DEVOLVER" else "Cliente pediu atendente."
        motivo = "\n".join([motivo, *self._contexto_para_a_equipe()])  # ticket 09 (Q2)
        dados = {"venda_id": self._ultima_venda_id} if self._ultima_venda_id is not None else None
        return self._transferir("", motivo, texto_cliente=texto, dados=dados)

    # --- passos ------------------------------------------------------------------

    def _passo_confirmar_numero(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return self._nao_entendeu(texto, [SIM_OU_NAO, self._pergunta_numero()])
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        if not self._numero_confiavel:
            # V1: número sem garantia de ser do cliente → não busca o cadastro.
            self._passo = "NOME"
            return ["Qual é o seu nome?"]
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
        return [f"Olá, {self._nome.split()[0]}!", *self._perguntar_produto()]

    def _passo_digitar_numero(self, texto: str) -> list[str]:
        numero = telefone(texto)
        if numero is None:
            return self._nao_entendeu(
                texto, ["Não entendi o número. Envie com DDD, por exemplo (86) 99999-0000.", PERGUNTA_DO_NUMERO]
            )
        self._telefone = com_nono_digito(numero)
        self._passo = "CONFIRMAR_NUMERO_DIGITADO"
        return [self._pergunta_numero_digitado()]

    def _passo_confirmar_numero_digitado(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return self._nao_entendeu(texto, [SIM_OU_NAO, self._pergunta_numero_digitado()])
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        # V1: número digitado NÃO busca o cadastro (não revela nome nem endereço
        # de quem é dono do número). O registrador acha o cliente ao registrar.
        if self._corrigindo:
            return self._voltar_ao_resumo()
        self._passo = "NOME"
        return ["Qual é o seu nome?"]

    def _passo_nome(self, texto: str) -> list[str]:
        nome = " ".join(texto.split())
        if not nome:
            return ["Preciso do seu nome para o pedido.", "Qual é o seu nome?"]
        intencao = self._intencao_em_texto_livre(texto)
        if intencao == "VAZAMENTO":
            return self._vazamento(texto)
        if intencao == "PERGUNTA" or pergunta_ou_nao_sei(nome):  # A1: "não sei" não é nome
            return [
                "Não consigo descobrir o seu nome sozinho. Me diga como você se chama. "
                "Se preferir, escreva atendente.",
                "Qual é o seu nome?",
            ]
        self._nome = nome
        if self._corrigindo:
            return self._voltar_ao_resumo()
        return [f"Olá, {nome.split()[0]}!", *self._perguntar_produto()]

    def _passo_produto(self, texto: str) -> list[str]:
        indice = opcao(texto, [p.nome for _, p in self._produtos])
        if indice is None:
            return self._nao_entendeu(texto, [DA_LISTA, self._pergunta_produto()], extrair=True)
        self._produto_id = self._produtos[indice][0]
        self._passo = "QUANTIDADE"
        return ["Quantos?"]

    def _passo_quantidade(self, texto: str) -> list[str]:
        quantidade = quantidade_do_texto(texto)  # ticket 03: "4 botijões na rua…" → 4
        if quantidade is None or not 1 <= quantidade <= MAX_QUANTIDADE:
            # extrair: com um produto só, a frase toda ("quero 2 botijões...") chega aqui (ticket 04).
            return self._nao_entendeu(texto, [f"Envie um número de 1 a {MAX_QUANTIDADE}.", "Quantos?"], extrair=True)
        self._quantidade = quantidade
        return self._seguir()

    def _passo_endereco_do_cadastro(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return self._nao_entendeu(texto, [SIM_OU_NAO, self._pergunta_endereco_do_cadastro()])
        if resposta is False:
            return self._comecar_endereco()
        self._endereco = self._endereco_do_cadastro
        return self._seguir()

    def _comecar_endereco(self, ditos: dict[str, str] | None = None) -> list[str]:
        self._campos: dict[str, str | None] = {}
        self._passo = "CAMPO_DO_ENDERECO"
        if ditos:
            # Problema 2: usa o que o cliente já escreveu e pergunta só o que falta.
            self._usar_campos(ditos)
            if any(c[0] not in self._campos for c in CAMPOS_DO_ENDERECO):
                return ["Anotei o endereço que você mandou.", self._pergunta_do_campo()]
            return self._proximo_campo_ou_fim()
        return ["Agora o endereço de entrega.", self._pergunta_do_campo()]

    def _usar_campos(self, ditos: dict[str, str]) -> None:
        """Guarda os campos ditos; complemento e referência não ditos ficam vazios (não pergunta)."""
        self._campos.update({k: v for k, v in ditos.items() if k not in self._campos})
        for nome, _, opcional, _ in CAMPOS_DO_ENDERECO:
            if opcional and nome not in self._campos:
                self._campos[nome] = None

    def _ja_informado(self, campo: str, pergunta: str) -> list[str]:
        """Problema 1: "já informei" nunca vira dado; usa o endereço dito antes, se houver."""
        faltam = {k: v for k, v in self._enderecos_ditos.items() if k not in self._campos}
        if campo in faltam:
            self._usar_campos(faltam)
            return ["Certo, usei o endereço que você mandou antes.", *self._proximo_campo_ou_fim()]
        rotulo, exemplo = ROTULOS_DOS_CAMPOS[campo]
        return [
            f"Desculpe, não consegui aproveitar o endereço da mensagem anterior. Me diga {rotulo} "
            f"(ex.: {exemplo}). Se preferir, escreva atendente.",
            pergunta,
        ]

    def _passo_campo_do_endereco(self, texto: str) -> list[str]:
        campo, pergunta, opcional, explicacao = self._campo_atual()
        valor = " ".join(texto.split())
        if valor and ja_informado(valor):
            return self._ja_informado(campo, pergunta)
        if valor and normalizar(valor) != "pular":
            if campo == "logradouro":
                # Rua (3.2): UMA consulta à IA serve para vazamento e para o endereço numa frase.
                interpretado = self._interpretar(texto)
                if interpretado is not None and interpretado.intencao == "VAZAMENTO":
                    return self._vazamento(texto)
                if (interpretado is not None and interpretado.intencao == "PERGUNTA") or pergunta_ou_nao_sei(valor):
                    return self._nao_e_um_dado(campo, pergunta)  # A1
                if interpretado is not None and interpretado.campos_do_endereco:
                    self._campos.update(interpretado.campos_do_endereco)
                    return self._proximo_campo_ou_fim()
                # Ticket 01: sem IA, o endereço inteiro não vira a rua.
                mais_que_a_rua, trecho = rua_do_texto(valor)
                repetida = "," not in valor and normalizar(valor) == self._rua_recusada
                if mais_que_a_rua and not repetida:  # I1: a mesma resposta de novo é aceita
                    if trecho is None:
                        self._rua_recusada = normalizar(valor)
                        return ["Me diga só o nome da rua (ex.: Rua das Flores).", pergunta]
                    self._campos["logradouro"] = trecho
                    return [f"Anotei a rua: {trecho}.", *self._proximo_campo_ou_fim()]
                # Sem IA ou nada útil (S3): o texto inteiro é a rua, como no C1.
            else:
                intencao = self._intencao_em_texto_livre(texto)
                if intencao == "VAZAMENTO":
                    return self._vazamento(texto)
                if campo != "estado" and (intencao == "PERGUNTA" or pergunta_ou_nao_sei(valor)):
                    return self._nao_e_um_dado(campo, pergunta)  # A1
        if opcional and normalizar(valor) == "pular":
            valor = None
        elif not valor:
            return [explicacao, pergunta]
        elif campo == "estado":
            valor = nome_do_estado(valor)
            if valor is None:
                return [explicacao, pergunta]
        elif campo == "numero":
            numero = numero_do_endereco(valor)
            if numero is None:
                if fala_de_apartamento(valor):  # A2: "ap409" não é o número do prédio
                    return ["O apartamento ou bloco vai no complemento.", "Qual é o número do prédio ou da casa? (ou S/N)"]
                return ["Envie o número da casa ou do prédio (ex.: 123) ou S/N.", pergunta]  # ticket 02
            valor = numero
        self._campos[campo] = valor
        return self._proximo_campo_ou_fim()

    def _nao_e_um_dado(self, campo: str, pergunta: str) -> list[str]:
        """A1: a resposta foi uma pergunta ou "não sei": nada é guardado."""
        rotulo, exemplo = ROTULOS_DOS_CAMPOS[campo]
        return [
            f"Não consigo descobrir o endereço sozinho. Me diga {rotulo} (ex.: {exemplo}). "
            "Se preferir, escreva atendente.",
            pergunta,
        ]

    def _campo_atual(self) -> tuple[str, str, bool, str]:
        """O primeiro campo do endereço que ainda não foi respondido."""
        return next(c for c in CAMPOS_DO_ENDERECO if c[0] not in self._campos)

    def _proximo_campo_ou_fim(self) -> list[str]:
        if any(c[0] not in self._campos for c in CAMPOS_DO_ENDERECO):
            return [self._pergunta_do_campo()]
        self._endereco = Endereco(**self._campos)
        if self._corrigindo:
            return self._voltar_ao_resumo()
        return self._seguir()

    def _passo_pagamento(self, texto: str) -> list[str]:
        indice = opcao(texto, [ROTULOS_DE_PAGAMENTO[t] for t in self._formas])
        if indice is None:
            return self._nao_entendeu(texto, [DA_LISTA, self._pergunta_pagamento()])
        self._pagamento = self._formas[indice]
        self._troco = None
        self._troco_respondido = False
        if self._pagamento == DINHEIRO:
            self._passo = "TROCO"
            return [PERGUNTA_DO_TROCO]
        return self._voltar_ao_resumo()

    def _passo_troco(self, texto: str) -> list[str]:
        if sim_ou_nao(texto) is False:
            self._troco = None
        else:
            valor = valor_em_reais(texto)
            if valor is None or valor < self._total():
                return self._nao_entendeu(texto, [
                    f"O troco precisa ser para pelo menos {reais(self._total())}, ou responda 'não'.",
                    PERGUNTA_DO_TROCO,
                ])
            self._troco = valor
        self._troco_respondido = True
        return self._voltar_ao_resumo()

    def _passo_resumo(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DO_RESUMO)
        if escolha is None:
            return self._nao_entendeu(texto, [DA_LISTA, self._resumo()])
        if escolha == 1:
            self._passo = "CORRIGIR"
            return [PERGUNTA_DO_QUE_CORRIGIR]
        if escolha == 2:
            self._passo = "ENCERRADA"
            return ["Pedido descartado. Nada foi enviado."]
        resultado = self._d.registrador.registrar(self._pedido())
        if isinstance(resultado, VendaCriada):
            self._passo = "POS_VENDA"  # problema 4: a conversa continua (cancelar, novo pedido)
            self._ultima_venda_id = resultado.id
            # Sem entregador e sem prazo (RN9; a meta de 20 min não é prometida).
            return [
                f"Pedido registrado! Número {resultado.id}, total {reais(resultado.total)}. "
                + ENSINA_O_POS_VENDA
            ]
        if isinstance(resultado, EmAndamento):
            return ["Seu pedido já está sendo registrado. Aguarde um instante."]
        if isinstance(resultado, PedidoInvalido):
            return self._corrigir_o_que_a_api_recusou(resultado)
        return self._transferir_venda(resultado)

    def _corrigir_o_que_a_api_recusou(self, invalido: PedidoInvalido) -> list[str]:
        """Leva o cliente ao passo do primeiro campo recusado; depois, volta ao resumo."""
        aviso = "Precisamos corrigir: " + "; ".join(p.mensagem for p in invalido.problemas) + "."
        campo = invalido.problemas[0].campo
        self._corrigindo = True
        if campo.startswith("endereco"):
            return [aviso, *self._comecar_endereco()[1:]]
        if campo == "nome":
            self._passo = "NOME"
            return [aviso, "Qual é o seu nome?"]
        if campo == "telefone":
            self._passo = "DIGITAR_NUMERO"
            return [aviso, PERGUNTA_DO_NUMERO]
        # Campo que o cliente não informou (itens, pagamento...): não é com ele.
        self._corrigindo = False
        return self._transferir(
            "Não consegui concluir seu pedido agora.",
            f"Pedido recusado na validação (campo {campo}).\n{self._resumo_do_pedido()}",
        )

    def _transferir_venda(self, transferir: TransferirAtendente) -> list[str]:
        """Registro não concluído: vai para a fila como VENDA. Nunca diz 'registrado' (RN8)."""
        self._passo = "ENCERRADA"
        try:
            protocolo = self._d.fila.abrir(
                "VENDA",
                self._telefone,
                f"Venda não concluída pelo chatbot: {transferir.motivo}\n{self._resumo_do_pedido()}",
                nome=self._nome or None,
                dados={
                    "pedido_id": self._pedido_id,
                    "venda_pode_existir": transferir.venda_pode_existir,
                    "marca": marca(self._pedido_id),
                },
            )
        except Exception:  # noqa: BLE001 - nunca inventar protocolo
            if not transferir.venda_pode_existir:
                _log.error("não foi possível registrar uma transferência de venda na fila")
                return [TENTE_DE_NOVO]
            # V2: a venda pode existir. Não mandar tentar de novo (duplicaria) e deixar
            # para a equipe um aviso só com o pedido e a marca (sem dado pessoal).
            _log.error(
                "EMERGÊNCIA: venda possivelmente criada sem registro na fila; pedido_id=%s marca=%s",
                self._pedido_id,
                marca(self._pedido_id),
            )
            return ["Não consegui concluir seu pedido agora.", NAO_REFACA_O_PEDIDO]
        mensagens = [
            "Não consegui concluir seu pedido agora. "
            f"Uma pessoa da nossa equipe vai continuar o atendimento. Protocolo {protocolo}."
        ]
        if transferir.venda_pode_existir:
            mensagens.append(NAO_REFACA_O_PEDIDO)  # I2: evita pedido duplicado
        return mensagens

    def _passo_corrigir(self, texto: str) -> list[str]:
        escolha = opcao(texto, O_QUE_CORRIGIR)
        if escolha is None:
            return self._nao_entendeu(texto, [DA_LISTA, PERGUNTA_DO_QUE_CORRIGIR])
        self._corrigindo = True
        if escolha == 0:
            return self._perguntar_produto()
        if escolha == 1:
            return self._comecar_endereco()
        if escolha == 2:
            self._passo = "PAGAMENTO"
            return [self._pergunta_pagamento()]
        self._passo = "NOME"
        return ["Qual é o seu nome?"]

    def _voltar_ao_resumo(self) -> list[str]:
        self._corrigindo = False
        self._passo = "RESUMO"
        return [self._resumo()]

    def _passo_pos_venda(self, texto: str) -> list[str]:
        """Depois do pedido registrado: um número solto não recomeça nada sem explicar."""
        if normalizar(texto) in ("oi", "ola", "novo pedido", "novo", "outro pedido"):
            self._novo_rascunho()
            return self.iniciar()
        # Ticket 08 (Q1): a IA pode abrir o cancelamento aqui (só essa intenção; atendente
        # e sair vindos da IA continuam ignorados, V3). Nada é registrado sem o "Confirma?".
        if self._d.devolucao is not None:
            interpretado = self._interpretar(texto)
            if interpretado is not None and interpretado.intencao == "CANCELAR_OU_DEVOLVER":
                tipo = "CANCELAMENTO" if "cancel" in normalizar(texto) else None
                return self._comecar_devolucao(PedidoDeDevolucao(tipo), texto)
        return [
            f"Seu pedido número {self._ultima_venda_id} já foi registrado. "
            f"{ENSINA_O_POS_VENDA} Para um novo pedido, escreva novo pedido."
        ]

    def _passo_encerrada(self, texto: str) -> list[str]:
        if normalizar(texto) in ("oi", "ola"):
            self._novo_rascunho()
            return self.iniciar()
        return ["Para um novo pedido, envie oi."]

    # --- devolução na conversa (fase C): o chatbot só lê o ERP e abre a solicitação ------

    def _devolucao_pedida(self, texto: str, pedido: str | None) -> list[str] | None:
        """Se a mensagem pede cancelamento, troca ou devolução de valor, começa a devolução."""
        if self._d.devolucao is None:
            return None
        if self._passo in PASSOS_DE_TEXTO_LIVRE and pedido != "CANCELAR_OU_DEVOLVER":
            return None  # V3: no texto livre, só a mensagem inteira ("cancelar") vale
        dev = pedido_de_devolucao(texto)
        if dev is None:
            if pedido != "CANCELAR_OU_DEVOLVER":
                return None
            dev = PedidoDeDevolucao(None)
        if self._passo in ("RESUMO", "CORRIGIR") and dev.tipo in ("CANCELAMENTO", None):
            return None  # "cancelar" no resumo continua descartando o rascunho (comando de sempre)
        if self._passo in PASSOS_DO_RASCUNHO or self._passo in ("RESUMO", "CORRIGIR"):
            # DC-2 + I2: com um pedido em andamento, nada é descartado sem o cliente escolher.
            self._devolucao_pendente = (dev, texto, self._passo)
            self._passo = "RASCUNHO_OU_PEDIDO"
            return [PERGUNTA_DO_RASCUNHO]
        return self._comecar_devolucao(dev, texto)

    def _passo_rascunho_ou_pedido(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DO_RASCUNHO)
        if escolha is None:
            return [DA_LISTA, PERGUNTA_DO_RASCUNHO]
        dev, original, passo_de_antes = self._devolucao_pendente
        if escolha == 0:  # continuar: o rascunho está intacto; repete a pergunta de antes
            self._passo = passo_de_antes
            return [self._pergunta_do_passo(passo_de_antes)]
        if escolha == 1:
            self._fim_da_devolucao()
            return ["Pedido descartado. Nada foi enviado."]
        return self._comecar_devolucao(dev, original, aviso=RASCUNHO_DESCARTADO)

    def _pergunta_do_passo(self, passo: str) -> str:
        perguntas = {
            "PRODUTO": self._pergunta_produto,
            "QUANTIDADE": lambda: "Quantos?",
            "ENDERECO_DO_CADASTRO": self._pergunta_endereco_do_cadastro,
            "CAMPO_DO_ENDERECO": self._pergunta_do_campo,
            "PAGAMENTO": self._pergunta_pagamento,
            "TROCO": lambda: PERGUNTA_DO_TROCO,
            "RESUMO": self._resumo,
            "CORRIGIR": lambda: PERGUNTA_DO_QUE_CORRIGIR,
        }
        return perguntas[passo]()

    def _comecar_devolucao(self, dev: PedidoDeDevolucao, texto: str, *, aviso: str | None = None) -> list[str]:
        self._dev: dict = {
            "tipo": dev.tipo,
            "motivo": dev.motivo,
            # Problema no produto: a frase do cliente é o texto (se já descreve o problema).
            "texto": " ".join(texto.split()) if dev.tipo == "PROBLEMA_PRODUTO" and dev.descreve else None,
            "preferencia": None,
            "encontrados": None,
            "escolha": None,
            "nota": None,
            "pergunta": "",
        }
        mensagens = [aviso] if aviso else []
        self._anotar_tentativa()
        if dev.tipo is None:
            self._passo = "DEV_MENU"
            return mensagens + [MENU_DA_DEVOLUCAO]
        return mensagens + self._dev_listar()

    def _anotar_tentativa(self) -> None:
        """Ticket 09: "Tentou cancelar o pedido 1001." vai junto se a pessoa pedir atendente."""
        acao = {"CANCELAMENTO": "cancelar", "ESTORNO": "pedir a devolução do valor de",
                "PROBLEMA_PRODUTO": "relatar um problema com"}.get(self._dev["tipo"])
        if acao is None:
            self._tentativa_de_devolucao = "Tentou abrir uma devolução."
            return
        pedido = self._pedido_escolhido()
        alvo = f"o pedido {pedido.id}" if pedido else "um pedido"
        self._tentativa_de_devolucao = f"Tentou {acao} {alvo}."

    def _passo_dev_menu(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DA_DEVOLUCAO)
        if escolha is None:
            return [DA_LISTA, MENU_DA_DEVOLUCAO]
        if escolha == 3:
            return self._comando("ATENDENTE", texto)
        self._dev["tipo"] = ("CANCELAMENTO", "PROBLEMA_PRODUTO", "ESTORNO")[escolha]
        self._anotar_tentativa()
        if self._dev["tipo"] == "PROBLEMA_PRODUTO":
            self._dev["motivo"] = "OUTRO"
        return self._dev_listar()

    def _pode_listar(self) -> bool:
        """V1: só mostra pedidos do número do canal (ou do cliente fixo, no --venda-real)."""
        assert self._d.devolucao is not None
        if self._d.devolucao.usa_cliente_fixo:
            return True
        return self._numero_confiavel and self._telefone == self._telefone_do_canal

    def _dev_listar(self) -> list[str]:
        assert self._d.devolucao is not None
        if not self._pode_listar():
            self._dev["nota"] = "Pedidos não listados: o número não é o do canal."
            return ["Por aqui não consigo ver seus pedidos. Vou registrar e a equipe confere.", *self._dev_motivo()]
        resultado = self._d.devolucao.pedidos_do_cliente(self._telefone)
        if isinstance(resultado, PedidosEncontrados):
            self._dev["encontrados"] = resultado
            self._passo = "DEV_PEDIDO"
            return [self._lista_de_pedidos()]
        if isinstance(resultado, NenhumPedido):
            self._dev["nota"] = "O cliente não tem pedidos nos últimos dias neste número."
            return [
                "Não encontrei pedidos recentes no seu número. Vou registrar mesmo assim e a equipe confere.",
                *self._dev_motivo(),
            ]
        self._dev["nota"] = f"Não foi possível listar os pedidos: {resultado.motivo}."
        return ["Não consegui ver seus pedidos agora. Vou registrar mesmo assim e a equipe confere.", *self._dev_motivo()]

    def _lista_de_pedidos(self) -> str:
        pedidos = self._dev["encontrados"].pedidos
        linhas = [f"{n}) {p.texto_para_cliente()}" for n, p in enumerate(pedidos, 1)]
        return "Qual pedido?\n" + "\n".join(linhas) + "\n0) não achei meu pedido"

    def _passo_dev_pedido(self, texto: str) -> list[str]:
        pedidos = self._dev["encontrados"].pedidos
        resposta = normalizar(texto).rstrip(")")
        if resposta == "0":
            return self._dev_motivo()  # sem pedido: a equipe confere
        if not resposta.isdigit() or not 1 <= int(resposta) <= len(pedidos):
            return ["Escolha um número da lista.", self._lista_de_pedidos()]
        self._dev["escolha"] = int(resposta)
        self._anotar_tentativa()
        if self._dev["tipo"] == "CANCELAMENTO" and pedidos[int(resposta) - 1].cancelado:
            if not self._perguntar_se_pagou():  # ticket 07: pago na entrega → nada a devolver
                self._fim_da_devolucao()
                return ["Esse pedido já está cancelado. Como o pagamento seria na entrega, não há valor a devolver."]
            self._dev["ja_cancelado"] = True
            self._passo = "DEV_PAGOU"
            return ["Esse pedido já está cancelado.", "Você já pagou esse pedido? (sim/não)"]
        return self._dev_motivo()

    def _perguntar_se_pagou(self) -> bool:
        """Ticket 07 (Q5): só no PIX (ou forma desconhecida). Dinheiro e cartão são pagos na entrega."""
        pedido = self._pedido_escolhido()
        formas = pedido.pagamentos if pedido else None
        return formas is None or "PIX" in formas

    def _motivos(self) -> list[tuple[str, str]]:
        return list(REGRAS[self._dev["tipo"]].motivos.items())

    def _pergunta_do_motivo(self) -> str:
        return "Qual o motivo?\n" + "\n".join(f"{n}) {rotulo}" for n, (_, rotulo) in enumerate(self._motivos(), 1))

    def _dev_motivo(self) -> list[str]:
        if self._dev["tipo"] == "PROBLEMA_PRODUTO":
            if self._dev["texto"]:
                return self._dev_confirmar()
            return self._pedir_texto("Conte em poucas palavras o que aconteceu com o produto.")
        if self._dev["motivo"] is None:
            self._passo = "DEV_MOTIVO"
            return [self._pergunta_do_motivo()]
        return self._dev_depois_do_motivo()

    def _passo_dev_motivo(self, texto: str) -> list[str]:
        escolha = opcao(texto, [rotulo for _, rotulo in self._motivos()])
        if escolha is None:
            return [DA_LISTA, self._pergunta_do_motivo()]
        self._dev["motivo"] = self._motivos()[escolha][0]
        if self._dev["motivo"] == "OUTRO":
            return self._pedir_texto("Conte em poucas palavras o motivo.")
        return self._dev_depois_do_motivo()

    def _pedir_texto(self, pergunta: str) -> list[str]:
        self._dev["pergunta"] = pergunta
        self._passo = "DEV_TEXTO"
        return [pergunta]

    def _passo_dev_texto(self, texto: str) -> list[str]:
        frase = " ".join(texto.split())
        if not frase:
            return ["Preciso de uma frase curta.", self._dev["pergunta"]]
        if len(frase) > 200:
            return ["Use no máximo 200 caracteres.", self._dev["pergunta"]]
        self._dev["texto"] = frase
        if self._dev["tipo"] == "PROBLEMA_PRODUTO":
            return self._dev_confirmar()
        return self._dev_depois_do_motivo()

    def _dev_depois_do_motivo(self) -> list[str]:
        if self._dev["tipo"] == "CANCELAMENTO":
            if not self._perguntar_se_pagou():
                return self._dev_confirmar()  # pago na entrega: sem preferência
            self._passo = "DEV_PAGOU"
            return ["Você já pagou esse pedido? (sim/não)"]
        return self._dev_preferencia()

    def _passo_dev_pagou(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, "Você já pagou esse pedido? (sim/não)"]
        if self._dev.get("ja_cancelado"):
            if not resposta:
                self._fim_da_devolucao()
                return ["Tudo bem: como o pedido já está cancelado e não foi pago, não há valor a devolver."]
            self._dev.update(tipo="ESTORNO", motivo="PEDIDO_CANCELADO_JA_PAGO")
            return self._dev_preferencia()
        return self._dev_preferencia() if resposta else self._dev_confirmar()

    def _dev_preferencia(self) -> list[str]:
        self._passo = "DEV_PREFERENCIA"
        return [PERGUNTA_DA_PREFERENCIA]

    def _passo_dev_preferencia(self, texto: str) -> list[str]:
        escolha = opcao(texto, list(PREFERENCIAS.values()))
        if escolha is None:
            return [DA_LISTA, PERGUNTA_DA_PREFERENCIA]
        self._dev["preferencia"] = list(PREFERENCIAS)[escolha]
        return self._dev_confirmar()

    def _pedido_escolhido(self) -> PedidoDoCliente | None:
        if self._dev["encontrados"] is None or self._dev["escolha"] is None:
            return None
        return self._dev["encontrados"].pedidos[self._dev["escolha"] - 1]

    def _resumo_da_devolucao(self) -> str:
        """I1 (revisão): o cliente confere antes de qualquer solicitação."""
        dev = self._dev
        pedido = self._pedido_escolhido()
        linhas = [
            f"Vou pedir à equipe: {ACAO_DA_DEVOLUCAO[dev['tipo']]}.",
            pedido.texto_para_cliente() if pedido else "Pedido: não escolhido (a equipe confere).",
        ]
        if dev["tipo"] == "PROBLEMA_PRODUTO":
            linhas.append(f"O que aconteceu: {dev['texto']}")
        else:
            rotulo = REGRAS[dev["tipo"]].motivos[dev["motivo"]]
            linhas.append(f"Motivo: {rotulo}" + (f" ({dev['texto']})" if dev["texto"] else ""))
        if dev["preferencia"]:
            linhas.append(f"Preferência: {PREFERENCIAS[dev['preferencia']]}")
        linhas.append("Confirma? 1) sim 2) não")
        return "\n".join(linhas)

    def _dev_confirmar(self) -> list[str]:
        self._passo = "DEV_CONFIRMAR"
        return [self._resumo_da_devolucao()]

    def _passo_dev_confirmar(self, texto: str) -> list[str]:
        escolha = opcao(texto, ["sim", "não"])
        if escolha is None:
            resposta = sim_ou_nao(texto)
            escolha = None if resposta is None else (0 if resposta else 1)
        if escolha is None:
            return [DA_LISTA, self._resumo_da_devolucao()]
        if escolha == 1:
            self._fim_da_devolucao()
            return ["Tudo bem, nada foi registrado."]
        return self._registrar_devolucao()

    def _registrar_devolucao(self) -> list[str]:
        assert self._d.devolucao is not None
        dev = self._dev
        self._fim_da_devolucao()
        try:
            resultado = self._d.devolucao.solicitar(
                dev["tipo"],
                self._telefone,
                encontrados=dev["encontrados"],
                escolha=dev["escolha"],
                motivo=dev["motivo"],
                texto=dev["texto"],
                preferencia=dev["preferencia"],
                nota=dev["nota"],
            )
        except Exception:  # noqa: BLE001 - §3.7: nunca inventar protocolo
            _log.error("não foi possível registrar uma solicitação de devolução na fila")
            return ["Não consegui registrar sua solicitação agora. Tente de novo em alguns minutos ou escreva atendente."]
        if isinstance(resultado, SolicitacaoAberta):
            return [resultado.mensagem_para_cliente + " Se precisar de comprovante, ela vai pedir."]
        return self._transferir(
            "Não consegui registrar sua solicitação.",
            "Solicitação de devolução recusada na validação: " + ", ".join(resultado.campos()),
        )

    def _fim_da_devolucao(self) -> None:
        """Volta ao estado de antes: depois de um pedido, POS_VENDA; senão, encerrada."""
        self._passo = "POS_VENDA" if self._ultima_venda_id is not None else "ENCERRADA"

    # --- IA (C2): só onde o roteiro não entendeu -----------------------------------

    def _interpretar(self, texto: str) -> PedidoInterpretado | None:
        """Consulta a IA e valida a resposta. Sem IA, erro ou nada útil → None.

        Vai ao provedor só o texto digitado agora (com números longos
        mascarados) + produtos, formas de pagamento e o passo (C2-4, C2-D1).
        """
        if self._d.interpretador is None:
            return None
        if self._d.ia_permitida is not None and not self._d.ia_permitida(self._telefone):
            return None  # H3: telefone atual fora da regra → nada vai à IA grátis
        contexto = Contexto(
            passo=self._passo,
            produtos=tuple(p.nome for _, p in self._produtos),
            pagamentos=tuple(ROTULOS_DE_PAGAMENTO[t] for t in self._formas),
        )
        try:
            interpretacao = self._d.interpretador.interpretar(mascarar(texto), contexto)
            if interpretacao is None:
                return None
            interpretado = validar_interpretacao(
                interpretacao,
                self._d.catalogo,
                {t: ROTULOS_DE_PAGAMENTO[t] for t in self._formas},
                texto=texto,  # I5: só vale parte do endereço que o cliente disse
            )
            if interpretado.campos_do_endereco:
                self._enderecos_ditos.update(interpretado.campos_do_endereco)  # para o "já informei"
            return interpretado
        except Exception:  # noqa: BLE001 - a IA nunca derruba a conversa (C2-3)
            return None

    def _nao_entendeu(self, texto: str, mensagens: list[str], *, extrair: bool = False) -> list[str]:
        """O roteiro não entendeu: a IA pode reconhecer a intenção (e, no produto, os dados)."""
        interpretado = self._interpretar(texto)
        if interpretado is None:
            return mensagens
        if interpretado.intencao == "VAZAMENTO":
            return self._vazamento(texto)
        if interpretado.intencao == "CANCELAR_OU_DEVOLVER" and self._d.devolucao is not None:
            return self._comecar_devolucao(PedidoDeDevolucao(None), texto)
        if interpretado.intencao in ("CANCELAR_OU_DEVOLVER", "ATENDENTE", "SAIR"):
            return self._comando(interpretado.intencao, texto)
        if extrair and interpretado.intencao == "PEDIR" and interpretado.product_id is not None:
            return self._preencher(interpretado)
        return mensagens

    def _intencao_em_texto_livre(self, texto: str) -> str | None:
        """Nos passos de texto livre, a IA só serve para achar VAZAMENTO (S1) ou
        PERGUNTA (A1). Atendente/cancelar/sair vindos da IA são ignorados (V3)."""
        interpretado = self._interpretar(texto)
        if interpretado is not None and interpretado.intencao in ("VAZAMENTO", "PERGUNTA"):
            return interpretado.intencao
        return None

    def _preencher(self, interpretado: PedidoInterpretado) -> list[str]:
        """Usa o que a IA entendeu e passou na validação; o resto é perguntado (3.1)."""
        self._produto_id = interpretado.product_id
        if interpretado.quantidade is not None:
            self._quantidade = interpretado.quantidade
        if interpretado.endereco is not None:
            self._endereco = interpretado.endereco  # o endereço dito substitui o do cadastro
        elif interpretado.campos_do_endereco:
            self._endereco_do_texto = dict(interpretado.campos_do_endereco)  # parcial: pergunta o resto
        if interpretado.pagamento is not None:
            self._pagamento = interpretado.pagamento
            self._troco = interpretado.troco
            self._troco_respondido = interpretado.troco is not None
        return self._seguir()

    def _seguir(self) -> list[str]:
        """Vai para o primeiro item do pedido que ainda falta (ou para o resumo)."""
        if not self._quantidade:
            self._passo = "QUANTIDADE"
            return ["Quantos?"]
        if self._endereco is None:
            if self._endereco_do_texto:
                ditos, self._endereco_do_texto = self._endereco_do_texto, None
                return self._comecar_endereco(ditos)  # o dito vence o do cadastro
            if self._endereco_do_cadastro is not None:
                self._passo = "ENDERECO_DO_CADASTRO"
                return [self._pergunta_endereco_do_cadastro()]
            return self._comecar_endereco()
        if not self._pagamento:
            self._passo = "PAGAMENTO"
            return [self._pergunta_pagamento()]
        if self._pagamento == DINHEIRO and self._troco is not None and self._troco < self._total():
            # O total mudou (ex.: correção do produto) e o troco informado não serve mais (I4).
            self._troco = None
            self._troco_respondido = False
        if self._pagamento == DINHEIRO and not self._troco_respondido:
            self._passo = "TROCO"
            return [PERGUNTA_DO_TROCO]
        return self._voltar_ao_resumo()

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

    def _vazamento(self, texto: str) -> list[str]:
        """Orientações PRIMEIRO; depois a solicitação urgente. A fila falhar não impede as orientações."""
        self._passo = "ENCERRADA"
        try:
            protocolo = self._d.fila.abrir(
                "VAZAMENTO",
                self._telefone,
                "Cliente relatou vazamento ou cheiro de gás.",
                texto_cliente=texto,
            )
        except Exception:  # noqa: BLE001 - qualquer falha: as orientações saem de todo jeito
            _log.error("não foi possível registrar um vazamento na fila")
            return [
                self._d.seguranca,
                "Não consegui registrar sua solicitação agora. "
                "Siga as orientações acima e ligue para os Bombeiros (193).",
            ]
        return [
            self._d.seguranca,
            f"Sua solicitação é urgente. Protocolo {protocolo}. "
            "Uma pessoa da nossa equipe vai entrar em contato.",
        ]

    def _transferir(
        self,
        aviso_ao_cliente: str,
        resumo: str,
        *,
        texto_cliente: str | None = None,
        dados: dict | None = None,
    ) -> list[str]:
        """Abre a solicitação ATENDIMENTO na fila e encerra a conversa."""
        self._passo = "ENCERRADA"
        try:
            protocolo = self._d.fila.abrir(
                "ATENDIMENTO", self._telefone, resumo, texto_cliente=texto_cliente, dados=dados
            )
        except Exception:  # noqa: BLE001 - nunca inventar protocolo (devolução §3.7)
            _log.error("não foi possível registrar uma transferência na fila")
            return [TENTE_DE_NOVO]
        aviso = f"{aviso_ao_cliente} " if aviso_ao_cliente else ""
        return [f"{aviso}Uma pessoa da nossa equipe vai continuar o atendimento. Protocolo {protocolo}."]

    # --- textos e montagem -------------------------------------------------------

    def _pergunta_do_campo(self) -> str:
        return self._campo_atual()[1]

    def _pergunta_numero_digitado(self) -> str:
        return f"É este: {telefone_legivel(self._telefone)}? (sim/não)"

    def _pergunta_numero(self) -> str:
        return f"Este número é seu: {telefone_legivel(self._telefone)}? (sim/não)"

    def _pergunta_endereco_do_cadastro(self) -> str:
        e = self._endereco_do_cadastro
        return f"Entregar em {e.logradouro}, {e.numero} — {e.bairro}, {e.cidade}? (sim/não)"

    def _perguntar_produto(self) -> list[str]:
        """Ticket 04 (Q3): com um produto só, pula a escolha e pergunta a quantidade."""
        if len(self._produtos) == 1:
            self._produto_id, produto = self._produtos[0]
            self._passo = "QUANTIDADE"
            return [f"{produto.nome[:1].upper()}{produto.nome[1:]} ({reais(produto.preco)}). Quantos?"]
        self._passo = "PRODUTO"
        return [self._pergunta_produto()]

    def _pergunta_produto(self) -> str:
        linhas = [f"{n}) {p.nome} — {reais(p.preco)}" for n, (_, p) in enumerate(self._produtos, 1)]
        return "O que você quer pedir?\n" + "\n".join(linhas)

    def _pergunta_pagamento(self) -> str:
        linhas = [f"{n}) {ROTULOS_DE_PAGAMENTO[t]}" for n, t in enumerate(self._formas, 1)]
        return "Como vai pagar?\n" + "\n".join(linhas)

    def _total(self) -> Decimal:
        return self._d.catalogo[self._produto_id].preco * self._quantidade

    def _resumo(self) -> str:
        return "\n".join(
            ["Confira seu pedido:", self._resumo_do_pedido(), "Responda: 1) confirmar  2) corrigir  3) cancelar"]
        )

    def _resumo_do_pedido(self) -> str:
        produto = self._d.catalogo[self._produto_id]
        e = self._endereco
        return "\n".join(
            [
                f"Nome: {self._nome}",
                f"Telefone: {telefone_legivel(self._telefone)}",
                f"{self._quantidade}x {produto.nome} — {reais(self._total())}",
                f"Entrega: {e.logradouro}, {e.numero} — {e.bairro}, {e.cidade}, {e.estado}",
                *([f"Complemento: {e.complemento}"] if e.complemento else []),  # A3
                *([f"Referência: {e.referencia}"] if e.referencia else []),
                f"Pagamento: {self._descricao_do_pagamento()}",
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

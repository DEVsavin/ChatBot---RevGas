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

from revgas_chatbot.api import TempoEsgotado
from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.entrada import (
    comando,
    normalizar,
    opcao,
    sim_ou_nao,
    telefone,
    telefone_legivel,
    valor_em_reais,
)
from revgas_chatbot.estados import nome_do_estado
from revgas_chatbot.fila import FilaDeAtendimento, mascarar
from revgas_chatbot.interpretacao import (
    Contexto,
    Interpretador,
    PedidoInterpretado,
    validar_interpretacao,
)
from revgas_chatbot.pedido import Endereco, Item, Pagamento, Pedido, ProdutoCatalogo
from revgas_chatbot.atendimento import TransferirAtendente
from revgas_chatbot.registro import EmAndamento, PedidoInvalido, RegistradorDeVendas, VendaCriada
from revgas_chatbot.venda import marca, reais

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
PASSOS_DE_TEXTO_LIVRE = frozenset({"NOME", "CAMPO_DO_ENDERECO"})

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


@dataclass(frozen=True)
class Dependencias:
    api: ApiCadenciada  # a MESMA instância usada pelo registrador
    registrador: RegistradorDeVendas
    fila: FilaDeAtendimento
    catalogo: dict[int, ProdutoCatalogo]
    pagamentos: set[str]
    seguranca: str  # orientações para vazamento (config/seguranca.txt)
    interpretador: Interpretador | None = None  # IA (C2); None = só o roteiro do C1


class Conversa:
    def __init__(self, telefone_do_canal: str, dependencias: Dependencias) -> None:
        self._telefone_do_canal = telefone_do_canal
        self._d = dependencias
        self._ultima_venda_id: int | None = None  # para "quero cancelar" depois da venda
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

    # --- interface -------------------------------------------------------------

    def iniciar(self) -> list[str]:
        return [
            "Olá! Sou o atendente virtual da revenda de gás. Vou anotar seu pedido.",
            self._pergunta_numero(),
        ]

    def responder(self, texto: str) -> list[str]:
        # Vazamento tem prioridade sempre, em qualquer passo (inclusive texto livre).
        pedido = comando(texto, livre=self._passo in PASSOS_DE_TEXTO_LIVRE)
        if pedido == "VAZAMENTO":
            return self._vazamento(texto)
        # Nos passos de opção, a resposta válida do passo vem antes do comando (V3):
        # no resumo, "cancelar" é a opção 3.
        resposta_do_passo = self._passo == "RESUMO" and opcao(texto, OPCOES_DO_RESUMO) is not None
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
            return self._nao_entendeu(
                texto, ["Não entendi o número. Envie com DDD, por exemplo (86) 99999-0000.", PERGUNTA_DO_NUMERO]
            )
        self._telefone = numero
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
        if self._vazamento_pela_ia(texto):
            return self._vazamento(texto)
        self._nome = nome
        if self._corrigindo:
            return self._voltar_ao_resumo()
        self._passo = "PRODUTO"
        return [f"Olá, {nome.split()[0]}!", self._pergunta_produto()]

    def _passo_produto(self, texto: str) -> list[str]:
        indice = opcao(texto, [p.nome for _, p in self._produtos])
        if indice is None:
            return self._nao_entendeu(texto, [DA_LISTA, self._pergunta_produto()], extrair=True)
        self._produto_id = self._produtos[indice][0]
        self._passo = "QUANTIDADE"
        return ["Quantos?"]

    def _passo_quantidade(self, texto: str) -> list[str]:
        quantidade = texto.strip()
        if not quantidade.isdigit() or not 1 <= int(quantidade) <= MAX_QUANTIDADE:
            return self._nao_entendeu(texto, [f"Envie um número de 1 a {MAX_QUANTIDADE}.", "Quantos?"])
        self._quantidade = int(quantidade)
        return self._seguir()

    def _passo_endereco_do_cadastro(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return self._nao_entendeu(texto, [SIM_OU_NAO, self._pergunta_endereco_do_cadastro()])
        if resposta is False:
            return self._comecar_endereco()
        self._endereco = self._endereco_do_cadastro
        return self._seguir()

    def _comecar_endereco(self) -> list[str]:
        self._campos: dict[str, str | None] = {}
        self._passo = "CAMPO_DO_ENDERECO"
        return ["Agora o endereço de entrega.", self._pergunta_do_campo()]

    def _passo_campo_do_endereco(self, texto: str) -> list[str]:
        campo, pergunta, opcional, explicacao = self._campo_atual()
        valor = " ".join(texto.split())
        if valor and normalizar(valor) != "pular":
            if campo == "logradouro":
                # Rua (3.2): UMA consulta à IA serve para vazamento e para o endereço numa frase.
                interpretado = self._interpretar(texto)
                if interpretado is not None and interpretado.intencao == "VAZAMENTO":
                    return self._vazamento(texto)
                if interpretado is not None and interpretado.campos_do_endereco:
                    self._campos.update(interpretado.campos_do_endereco)
                    return self._proximo_campo_ou_fim()
                # Sem IA ou nada útil (S3): o texto inteiro é a rua, como no C1.
            elif self._vazamento_pela_ia(texto):
                return self._vazamento(texto)
        if opcional and normalizar(valor) == "pular":
            valor = None
        elif not valor:
            return [explicacao, pergunta]
        elif campo == "estado":
            valor = nome_do_estado(valor)
            if valor is None:
                return [explicacao, pergunta]
        self._campos[campo] = valor
        return self._proximo_campo_ou_fim()

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
            self._passo = "ENCERRADA"
            self._ultima_venda_id = resultado.id
            # Sem entregador e sem prazo (RN9; a meta de 20 min não é prometida).
            return [f"Pedido registrado! Número {resultado.id}, total {reais(resultado.total)}."]
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
            self._passo = "PRODUTO"
            return [self._pergunta_produto()]
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

    def _passo_encerrada(self, texto: str) -> list[str]:
        if normalizar(texto) in ("oi", "ola"):
            self._novo_rascunho()
            return self.iniciar()
        return ["Para um novo pedido, envie oi."]

    # --- IA (C2): só onde o roteiro não entendeu -----------------------------------

    def _interpretar(self, texto: str) -> PedidoInterpretado | None:
        """Consulta a IA e valida a resposta. Sem IA, erro ou nada útil → None.

        Vai ao provedor só o texto digitado agora (com números longos
        mascarados) + produtos, formas de pagamento e o passo (C2-4, C2-D1).
        """
        if self._d.interpretador is None:
            return None
        contexto = Contexto(
            passo=self._passo,
            produtos=tuple(p.nome for _, p in self._produtos),
            pagamentos=tuple(ROTULOS_DE_PAGAMENTO[t] for t in self._formas),
        )
        try:
            interpretacao = self._d.interpretador.interpretar(mascarar(texto), contexto)
            if interpretacao is None:
                return None
            return validar_interpretacao(
                interpretacao,
                self._d.catalogo,
                {t: ROTULOS_DE_PAGAMENTO[t] for t in self._formas},
                texto=texto,  # I5: só vale parte do endereço que o cliente disse
            )
        except Exception:  # noqa: BLE001 - a IA nunca derruba a conversa (C2-3)
            return None

    def _nao_entendeu(self, texto: str, mensagens: list[str], *, extrair: bool = False) -> list[str]:
        """O roteiro não entendeu: a IA pode reconhecer a intenção (e, no produto, os dados)."""
        interpretado = self._interpretar(texto)
        if interpretado is None:
            return mensagens
        if interpretado.intencao == "VAZAMENTO":
            return self._vazamento(texto)
        if interpretado.intencao in ("CANCELAR_OU_DEVOLVER", "ATENDENTE", "SAIR"):
            return self._comando(interpretado.intencao, texto)
        if extrair and interpretado.intencao == "PEDIR" and interpretado.product_id is not None:
            return self._preencher(interpretado)
        return mensagens

    def _vazamento_pela_ia(self, texto: str) -> bool:
        """Nos passos de texto livre, a IA só serve para achar vazamento (S1)."""
        interpretado = self._interpretar(texto)
        return interpretado is not None and interpretado.intencao == "VAZAMENTO"

    def _preencher(self, interpretado: PedidoInterpretado) -> list[str]:
        """Usa o que a IA entendeu e passou na validação; o resto é perguntado (3.1)."""
        self._produto_id = interpretado.product_id
        if interpretado.quantidade is not None:
            self._quantidade = interpretado.quantidade
        if interpretado.endereco is not None:
            self._endereco = interpretado.endereco  # o endereço dito substitui o do cadastro
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

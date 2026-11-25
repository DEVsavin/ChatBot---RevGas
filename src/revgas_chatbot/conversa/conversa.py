"""Conversa de pedido: o motor do atendimento, sem canal e sem LLM.

`Conversa.iniciar()` e `Conversa.responder(texto)` devolvem a lista de
mensagens do chatbot. Quem lê o teclado ou o WhatsApp e mostra as
mensagens é o canal (ex.: `chat_terminal.py`), não este módulo.

Máquina de estados pura (regras e if/else determinísticos), fazendo uma
pergunta curta e individual por vez para não confundir o cliente.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from revgas_chatbot.api_revgas.api import TempoEsgotado
from revgas_chatbot.api_revgas.api_cadenciada import ApiCadenciada
from revgas_chatbot.atendimento.atendimento import TransferirAtendente, mascarar
from revgas_chatbot.conversa.entrada import (
    PedidoDeDevolucao,
    com_nono_digito,
    comando,
    extrair_pedido_livre,
    fala_de_apartamento,
    forma_de_pagamento,
    ja_informado,
    ler_modelo,
    normalizar,
    numero_do_endereco,
    opcao,
    parece_modelo,
    pedido_de_devolucao,
    pergunta_ou_nao_sei,
    quantidade_do_texto,
    quer_pular,
    rua_do_texto,
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
    SolicitacaoAberta,
    montar_corpo_atualizacao_obs,
)
from revgas_chatbot.vendas.historico import (
    HistoricoDeVendas,
    NenhumPedido,
    PedidoDoCliente,
    PedidosEncontrados,
)
from revgas_chatbot.vendas.pedido import (
    DINHEIRO,
    MAX_SERIAL_VALEGAS,
    VALEGAS,
    Endereco,
    Item,
    Pagamento,
    Pedido,
    ProdutoCatalogo,
)
from revgas_chatbot.vendas.registro import EmAndamento, PedidoInvalido, RegistradorDeVendas, VendaCriada
from revgas_chatbot.vendas.venda import marca, reais

MAX_QUANTIDADE = 10

_log = logging.getLogger(__name__)

# Rótulos em português, na ordem em que aparecem para o cliente.
ROTULOS_DE_PAGAMENTO = {
    "PIX": "PIX",
    "MONEY": "Dinheiro",
    "CARTAO_DE_DEBITO": "Cartão de débito",
    "CARTAO_DE_CREDITO": "Cartão de crédito",
    "VALEGAS": "Vale-gás",
}

OPCOES_DO_RESUMO = ["Confirmar", "Corrigir", "Cancelar"]

# Passos em que o cliente digita texto livre: comandos (exceto vazamento)
# só valem se forem a mensagem inteira.
PASSOS_DE_TEXTO_LIVRE = frozenset({"NOME", "CAMPO_DO_ENDERECO", "SERIAL_VALEGAS", "DEV_TEXTO"})

# Passos com um pedido em andamento (rascunho ainda não confirmado).
PASSOS_DO_RASCUNHO = frozenset(
    {
        "PRODUTO",
        "QUANTIDADE",
        "ENDERECO_DO_CADASTRO",
        "CAMPO_DO_ENDERECO",
        "PAGAMENTO",
        "MOMENTO_PIX",
        "TROCO",
        "SERIAL_VALEGAS",
    }
)
OPCOES_DO_RASCUNHO = ["Continuar este pedido", "Desistir deste pedido", "Falar de um pedido já feito"]
PERGUNTA_DO_RASCUNHO = (
    "Você está no meio de um pedido. Quer:\n1) Continuar este pedido\n"
    "2) Desistir deste pedido (nada foi enviado)\n3) Falar de um pedido já feito (cancelar, problema ou cobrança)"
)
RASCUNHO_DESCARTADO = "O pedido que você estava fazendo foi descartado (nada tinha sido enviado)."
OPCOES_DA_DEVOLUCAO = [
    "Cancelar um pedido",
    "Problema com o produto",
    "Cobrança ou devolução de dinheiro",
    "Falar com um atendente",
]
MENU_DA_DEVOLUCAO = "Posso ajudar com:\n" + "\n".join(f"{n}) {o}" for n, o in enumerate(OPCOES_DA_DEVOLUCAO, 1))
PERGUNTA_DA_PREFERENCIA = (
    "Se houver valor a devolver, você prefere:\n1) Crédito na próxima compra\n2) Dinheiro de volta"
)
AVISO_DO_ENTREGADOR = (
    "O entregador pode já estar a caminho. A equipe vai tentar avisá-lo agora; "
    "se ele chegar antes, você pode recusar o pedido na porta."
)
OPCOES_DO_ENTREGUE = ["Problema com o produto", "Devolução do valor", "Falar com um atendente"]
PERGUNTA_DO_ENTREGUE = "Esse pedido já foi entregue. Posso ajudar com:\n" + "\n".join(
    f"{n}) {o}" for n, o in enumerate(OPCOES_DO_ENTREGUE, 1)
)
ENSINA_O_POS_VENDA = (
    "Para acompanhar, escreva status. Para cancelar, escreva cancelar. Para outra coisa, escreva atendente."
)
ACAO_DA_DEVOLUCAO = {
    "CANCELAMENTO": "cancelar o pedido",
    "ESTORNO": "devolver o valor do pedido",
    "PROBLEMA_PRODUTO": "verificar um problema no produto",
}

SIM_OU_NAO = "Responda sim ou não."
PERGUNTA_DO_NUMERO = "Qual é o seu número com DDD?"
O_QUE_CORRIGIR = ["Produto", "Endereço", "Pagamento", "Nome"]
PERGUNTA_DO_QUE_CORRIGIR = "O que quer corrigir?\n" + "\n".join(
    f"{n}) {item}" for n, item in enumerate(O_QUE_CORRIGIR, 1)
)
OPCOES_MOMENTO_PIX = ["Pagar agora", "Pagar na entrega"]
PERGUNTA_MOMENTO_PIX = "O pagamento no PIX será:\n1) Pagar agora\n2) Pagar na entrega"
PERGUNTA_DO_TROCO = "Vai precisar de troco? Responda o valor (ex.: 200) ou 'não'."
PERGUNTA_DO_SERIAL_VALEGAS = "Qual é o código do vale-gás? (7 caracteres, ex.: 1234567)"
NAO_REFACA_O_PEDIDO = "Não faça um novo pedido: nossa equipe vai verificar se ele foi registrado."
TENTE_DE_NOVO = (
    "Não consegui registrar sua solicitação agora. "
    "Tente de novo em alguns minutos ou ligue para a revenda."
)
DA_LISTA = "Escolha um número da lista."

OPCAO_PULAR = "0) Não tem / Pular etapa"

# Endereço campo a campo (uma pergunta curta por vez):
# (campo do Endereco, pergunta, opcional?, explicação se não servir).
CAMPOS_DO_ENDERECO = [
    ("logradouro", "Qual é a rua?", False, "Preciso da rua para a entrega."),
    ("numero", f"Qual é o número?\n{OPCAO_PULAR}", False, f"Preciso do número ou escolha {OPCAO_PULAR}."),
    ("quadra", f"Qual é a quadra?\n{OPCAO_PULAR}", True, ""),
    ("bairro", "Qual é o bairro?", False, "Preciso do bairro para a entrega."),
    ("cidade", "Qual é a cidade?", False, "Preciso da cidade para a entrega."),
    (
        "estado",
        "Qual é o estado? (ex.: PI ou Piauí)",
        False,
        "Não reconheci o estado. Envie a sigla (PI) ou o nome (Piauí).",
    ),
    ("complemento", f"Complemento?\n{OPCAO_PULAR}", True, ""),
    ("referencia", f"Ponto de referência?\n{OPCAO_PULAR}", True, ""),
]

_OPCAO = "envie só o número da opção, como 1"
EXEMPLOS_DAS_PERGUNTAS = {
    "CONFIRMAR_NUMERO": "responda sim ou não",
    "CONFIRMAR_NUMERO_DIGITADO": "responda sim ou não",
    "DIGITAR_NUMERO": "(86) 99999-0000",
    "NOME": "Maria Silva",
    "REPETIR_ULTIMO": "1 para sim ou 2 para fazer outro pedido",
    "PRODUTO": _OPCAO,
    "QUANTIDADE": "2",
    "ENDERECO_DO_CADASTRO": "responda sim ou não",
    "PAGAMENTO": _OPCAO,
    "MOMENTO_PIX": _OPCAO,
    "TROCO": "200, ou não",
    "SERIAL_VALEGAS": "VG-123456",
    "RESUMO": _OPCAO,
    "CORRIGIR": _OPCAO,
    "RASCUNHO_OU_PEDIDO": _OPCAO,
    "ACOMPANHAR_ESCOLHER": _OPCAO,
    "DEV_MENU": _OPCAO,
    "DEV_PEDIDO": _OPCAO,
    "DEV_MOTIVO": _OPCAO,
    "DEV_TEXTO": "o botijão veio sem lacre",
    "DEV_PAGOU": "responda sim ou não",
    "DEV_PREFERENCIA": _OPCAO,
    "DEV_CONFIRMAR": "1 para confirmar ou 2 para não registrar",
    "DEV_ESTE_PEDIDO": "1 para sim ou 2 se for outro pedido",
    "DEV_UM_PEDIDO": "1 para sim ou 2 para não",
    "DEV_ENTREGUE": _OPCAO,
}
EXEMPLOS_DO_ENDERECO = {
    "logradouro": "Rua das Flores",
    "numero": "123, ou 0 para pular",
    "quadra": "Quadra 12, ou 0 para pular",
    "bairro": "Centro",
    "cidade": "Teresina",
    "estado": "PI",
    "complemento": "Apto 12, ou 0 para pular",
    "referencia": "perto da praça, ou 0 para pular",
}

ROTULOS_DOS_CAMPOS = {
    "logradouro": ("o nome da rua", "Rua das Flores"),
    "numero": ("o número", "120, ou 0 para pular"),
    "quadra": ("a quadra", "Quadra 12, ou 0 para pular"),
    "bairro": ("o bairro", "Centro"),
    "cidade": ("a cidade", "Teresina"),
    "complemento": ("o complemento", "Apto 12, ou 0 para pular"),
    "referencia": ("um ponto de referência", "perto da praça, ou 0 para pular"),
}


@dataclass(frozen=True)
class Dependencias:
    api: ApiCadenciada  # a MESMA instância usada pelo registrador
    registrador: RegistradorDeVendas
    catalogo: dict[int, ProdutoCatalogo]
    pagamentos: set[str]
    seguranca: str  # orientações para vazamento (config/seguranca.txt)
    devolucao: Devolucao | None = None
    historico: HistoricoDeVendas | None = None


class Conversa:
    def __init__(
        self,
        telefone_do_canal: str,
        dependencias: Dependencias,
        *,
        numero_confiavel: bool = True,
    ) -> None:
        self._telefone_do_canal = com_nono_digito(telefone_do_canal)
        self._numero_confiavel = numero_confiavel
        self._d = dependencias
        self._ultima_venda_id: int | None = None
        self._recusas_seguidas = 0
        self._rua_recusada: str | None = None
        self._tentativa_de_devolucao: str | None = None
        self._travou_em: str | None = None
        self._produtos = list(self._d.catalogo.items())
        self._formas = [t for t in ROTULOS_DE_PAGAMENTO if t in self._d.pagamentos]
        self._nome_lembrado: str = ""
        self._endereco_lembrado: Endereco | None = None
        self._ultima_repetivel: PedidoDoCliente | None = None
        self._acompanhar_pedidos: tuple[PedidoDoCliente, ...] = ()
        self._passo_anterior_acompanhar: str = "ENCERRADA"
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
        self._pagamento_pix: str | None = None
        self._troco: Decimal | None = None
        self._troco_respondido = False
        self._serial_valegas: str | None = None
        self._pedido_id = str(uuid.uuid4())
        self._enderecos_ditos: dict[str, str] = {}
        self._endereco_do_texto: dict[str, str] | None = None
        self._cartao_sem_tipo = False

    # --- interface -------------------------------------------------------------

    @property
    def encerrada(self) -> bool:
        return self._passo == "ENCERRADA"

    def iniciar(self) -> list[str]:
        saudacao = "Olá! Sou o atendente virtual da revenda de gás. Vou anotar seu pedido."
        self._telefone = self._telefone_do_canal
        if not self._numero_confiavel:
            if self._nome_lembrado:
                self._nome = self._nome_lembrado
            if self._d.historico is not None and self._d.historico.usa_cliente_fixo:
                ultima = self._d.historico.ultima_venda_repetivel(
                    self._telefone, pagamentos_permitidos=self._d.pagamentos
                )
                if ultima is not None:
                    self._ultima_repetivel = ultima
                    if ultima.endereco is not None:
                        self._endereco_lembrado = ultima.endereco
                    self._passo = "REPETIR_ULTIMO"
                    if self._nome:
                        return [f"Olá, {self._nome.split()[0]}!", self._pergunta_repetir(ultima)]
                    return [saudacao, self._pergunta_repetir(ultima)]
            if self._endereco_lembrado is not None:
                self._endereco = self._endereco_lembrado
            if self._nome:
                return [f"Olá, {self._nome.split()[0]}!", *self._perguntar_produto()]
            self._passo = "NOME"
            return [saudacao, "Qual é o seu nome?"]
        clientes = self._buscar_cliente_do_canal()
        if clientes is None or len(clientes) > 1:
            return self._transferir(
                "Não consegui identificar seu cadastro agora.",
                "Cliente não identificado com segurança pelo número do canal.",
            )
        if not clientes:
            self._passo = "NOME"
            return [saudacao, "Qual é o seu nome?"]
        [cliente] = clientes
        self._nome = cliente["name"]
        self._nome_lembrado = self._nome
        self._endereco_do_cadastro = _endereco_do_cadastro(cliente)
        if self._d.historico is not None:
            ultima = self._d.historico.ultima_venda_repetivel(
                self._telefone, pagamentos_permitidos=self._d.pagamentos
            )
            if ultima is not None:
                self._ultima_repetivel = ultima
                self._passo = "REPETIR_ULTIMO"
                return [f"Olá, {self._nome.split()[0]}!", self._pergunta_repetir(ultima)]
        return [f"Olá, {self._nome.split()[0]}!", *self._perguntar_produto()]

    def responder(self, texto: str) -> list[str]:
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
            if pedido in ("ATENDENTE", "SAIR"):
                return self._comando(pedido, texto)
            return getattr(self, f"_passo_{self._passo.lower()}")(texto)
        if self._passo == "ACOMPANHAR_ESCOLHER":
            if pedido in ("ATENDENTE", "SAIR"):
                return self._comando(pedido, texto)
            return self._passo_acompanhar_escolher(texto)

        # Nos passos de opção, a resposta válida do passo vem antes do comando:
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
        if pedido == "CONSULTAR_PRECO":
            return self._responder_faq()
        if pedido == "ACOMPANHAR_PEDIDO":
            return self._iniciar_acompanhamento()
        if pedido == "MEUS_PEDIDOS":
            return self._listar_meus_pedidos()
        if pedido == "REPETIR_PEDIDO":
            return self._iniciar_recompra_por_comando()
        if pedido == "CANCELAR_OU_DEVOLVER" and self._passo in ("RESUMO", "CORRIGIR"):
            self._passo = "ENCERRADA"
            return ["Pedido descartado. Nada foi enviado."]
        motivo = "Cliente quer cancelar ou devolver." if pedido == "CANCELAR_OU_DEVOLVER" else "Cliente pediu atendente."
        motivo = "\n".join([motivo, *self._contexto_para_a_equipe()])
        dados = {"venda_id": self._ultima_venda_id} if self._ultima_venda_id is not None else None
        return self._transferir("", motivo, texto_cliente=texto, dados=dados)

    # --- FAQ, Meus Pedidos, Acompanhamento e Recompra ----------------------------

    def _responder_faq(self) -> list[str]:
        produtos = ", ".join(f"{p.nome} ({reais(p.preco)})" for _, p in self._produtos)
        formas = ", ".join(ROTULOS_DE_PAGAMENTO[t] for t in self._formas)
        info = f"Produtos disponíveis: {produtos}.\nFormas de pagamento: {formas} (e outras com atendente)."
        if self._passo in PASSOS_DO_RASCUNHO or self._passo in ("RESUMO", "CORRIGIR", "CONFIRMAR_NUMERO", "NOME"):
            return [info, self._pergunta_atual_para_retomar()]
        return [info, "Para fazer um pedido, escreva novo pedido."]

    def _pergunta_atual_para_retomar(self) -> str:
        if self._passo == "CONFIRMAR_NUMERO":
            return self._pergunta_numero()
        if self._passo == "NOME":
            return "Qual é o seu nome?"
        if self._passo in PASSOS_DO_RASCUNHO or self._passo in ("RESUMO", "CORRIGIR"):
            return self._pergunta_do_passo(self._passo)
        return "Para um novo pedido, envie oi."

    def _historico_disponivel(self) -> HistoricoDeVendas | None:
        return self._d.historico

    def _pode_listar_historico(self) -> bool:
        historico = self._historico_disponivel()
        if historico is None:
            return False
        if historico.usa_cliente_fixo:
            return True
        return self._numero_confiavel and self._telefone == self._telefone_do_canal

    def _listar_meus_pedidos(self) -> list[str]:
        historico = self._historico_disponivel()
        if historico is None or not self._pode_listar_historico():
            return ["Por aqui não consigo listar seus pedidos. Escreva atendente para falar com a equipe."]
        resultado = historico.pedidos_do_cliente(self._telefone, prazo_dias=30, limite=5)
        if isinstance(resultado, PedidosEncontrados):
            linhas = [f"{n}) {p.texto_com_status()}" for n, p in enumerate(resultado.pedidos, 1)]
            return [
                "Seus últimos pedidos:\n" + "\n".join(linhas),
                "Para ver detalhes de um pedido, escreva status. Para repetir o último, escreva repetir.",
            ]
        if isinstance(resultado, NenhumPedido):
            return ["Não encontrei pedidos recentes no seu número. Para fazer um pedido, escreva novo pedido."]
        return ["Não consegui consultar seus pedidos agora. Escreva atendente para falar com a equipe."]

    def _iniciar_acompanhamento(self) -> list[str]:
        historico = self._historico_disponivel()
        if historico is None or not self._pode_listar_historico():
            return ["Por aqui não consigo consultar o andamento do pedido. Escreva atendente para falar com a equipe."]
        if self._ultima_venda_id is not None:
            detalhes = historico.detalhar_pedido(self._ultima_venda_id)
            if detalhes is not None:
                return [detalhes.texto_para_cliente()]
        resultado = historico.pedidos_do_cliente(self._telefone, prazo_dias=30, limite=5)
        if isinstance(resultado, PedidosEncontrados):
            if len(resultado.pedidos) == 1:
                detalhes = historico.detalhar_pedido(resultado.pedidos[0].id)
                if detalhes is not None:
                    return [detalhes.texto_para_cliente()]
                return [resultado.pedidos[0].texto_com_status()]
            self._passo_anterior_acompanhar = self._passo
            self._acompanhar_pedidos = resultado.pedidos
            self._passo = "ACOMPANHAR_ESCOLHER"
            return [self._lista_acompanhar()]
        if isinstance(resultado, NenhumPedido):
            return ["Não encontrei pedidos recentes no seu número. Para falar com a equipe, escreva atendente."]
        return ["Não consegui consultar seu pedido agora. Escreva atendente para falar com a equipe."]

    def _lista_acompanhar(self) -> str:
        linhas = [f"{n}) {p.texto_com_status()}" for n, p in enumerate(self._acompanhar_pedidos, 1)]
        return "Qual pedido você quer acompanhar?\n" + "\n".join(linhas)

    def _passo_acompanhar_escolher(self, texto: str) -> list[str]:
        resposta = normalizar(texto).rstrip(").- ")
        if not resposta.isdigit() or not 1 <= int(resposta) <= len(self._acompanhar_pedidos):
            return [DA_LISTA, self._lista_acompanhar()]
        escolhido = self._acompanhar_pedidos[int(resposta) - 1]
        self._passo = self._passo_anterior_acompanhar
        historico = self._historico_disponivel()
        detalhes = historico.detalhar_pedido(escolhido.id) if historico is not None else None
        if detalhes is not None:
            return [detalhes.texto_para_cliente()]
        return [escolhido.texto_com_status()]

    def _iniciar_recompra_por_comando(self) -> list[str]:
        historico = self._historico_disponivel()
        if historico is None or not self._pode_listar_historico():
            return ["Por aqui não consigo buscar seu último pedido. Vamos anotar um pedido novo.", *self._perguntar_produto()]
        ultima = historico.ultima_venda_repetivel(self._telefone, pagamentos_permitidos=self._d.pagamentos)
        if ultima is None:
            return ["Não encontrei um pedido anterior para repetir.", *self._perguntar_produto()]
        if not self._nome:
            clientes = self._buscar_cliente_do_canal()
            if clientes and len(clientes) == 1:
                self._nome = clientes[0].get("name") or ""
        self._ultima_repetivel = ultima
        self._passo = "REPETIR_ULTIMO"
        return [self._pergunta_repetir(ultima)]

    def _pergunta_repetir(self, pedido: PedidoDoCliente) -> str:
        pid, qtd = pedido.itens_ids[0]
        produto = self._d.catalogo[pid]
        total_atual = produto.preco * qtd
        forma = ROTULOS_DE_PAGAMENTO.get(pedido.pagamentos[0], pedido.pagamentos[0]) if pedido.pagamentos else ""
        e = pedido.endereco
        assert e is not None
        resumo = f"{qtd}x {produto.nome}, {forma}, {e.logradouro}, {e.numero} ({reais(total_atual)})"
        return f"Quer repetir seu último pedido?\n{resumo}\n1) Sim\n2) Não, fazer outro pedido"

    def _passo_repetir_ultimo(self, texto: str) -> list[str]:
        escolha = opcao(texto, ["Sim", "Não, fazer outro pedido"])
        if escolha is None and sim_ou_nao(texto) is not None:
            escolha = 0 if sim_ou_nao(texto) else 1
        if escolha is None:
            assert self._ultima_repetivel is not None
            return [DA_LISTA, self._pergunta_repetir(self._ultima_repetivel)]
        if escolha == 1 or self._ultima_repetivel is None:
            pedido_anterior = self._ultima_repetivel
            self._ultima_repetivel = None
            if pedido_anterior is not None and pedido_anterior.endereco is not None:
                self._endereco = pedido_anterior.endereco
                self._endereco_lembrado = pedido_anterior.endereco
            elif self._endereco_lembrado is not None:
                self._endereco = self._endereco_lembrado
            if not self._nome:
                self._passo = "NOME"
                return ["Qual é o seu nome?"]
            return self._perguntar_produto()
        pedido = self._ultima_repetivel
        self._ultima_repetivel = None
        pid, qtd = pedido.itens_ids[0]
        self._produto_id = pid
        self._quantidade = qtd
        self._endereco = pedido.endereco
        self._pagamento = pedido.pagamentos[0] if pedido.pagamentos else ""
        self._pagamento_pix = None
        self._troco = None
        self._troco_respondido = False
        self._serial_valegas = None
        if not self._nome:
            self._passo = "NOME"
            return ["Qual é o seu nome?"]
        return self._seguir()

    # --- passos do pedido -------------------------------------------------------

    def _passo_confirmar_numero(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_numero()]
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        if not self._numero_confiavel:
            if self._nome_lembrado:
                self._nome = self._nome_lembrado
            if self._d.historico is not None and self._d.historico.usa_cliente_fixo:
                ultima = self._d.historico.ultima_venda_repetivel(
                    self._telefone, pagamentos_permitidos=self._d.pagamentos
                )
                if ultima is not None:
                    self._ultima_repetivel = ultima
                    self._passo = "REPETIR_ULTIMO"
                    saudacao = [f"Olá, {self._nome.split()[0]}!"] if self._nome else []
                    return [*saudacao, self._pergunta_repetir(ultima)]
            if self._nome:
                return [f"Olá, {self._nome.split()[0]}!", *self._perguntar_produto()]
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
        # Recompra (3.2): se houver pedido anterior não cancelado, oferece repetir.
        if self._d.historico is not None:
            ultima = self._d.historico.ultima_venda_repetivel(
                self._telefone, pagamentos_permitidos=self._d.pagamentos
            )
            if ultima is not None:
                self._ultima_repetivel = ultima
                self._passo = "REPETIR_ULTIMO"
                return [f"Olá, {self._nome.split()[0]}!", self._pergunta_repetir(ultima)]
        return [f"Olá, {self._nome.split()[0]}!", *self._perguntar_produto()]

    def _passo_digitar_numero(self, texto: str) -> list[str]:
        numero = telefone(texto)
        if numero is None:
            return ["Não entendi o número. Envie com DDD, por exemplo (86) 99999-0000.", PERGUNTA_DO_NUMERO]
        self._telefone = com_nono_digito(numero)
        self._passo = "CONFIRMAR_NUMERO_DIGITADO"
        return [self._pergunta_numero_digitado()]

    def _passo_confirmar_numero_digitado(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_numero_digitado()]
        if resposta is False:
            self._passo = "DIGITAR_NUMERO"
            return [PERGUNTA_DO_NUMERO]
        if self._corrigindo:
            return self._voltar_ao_resumo()
        self._passo = "NOME"
        return ["Qual é o seu nome?"]

    def _passo_nome(self, texto: str) -> list[str]:
        nome = " ".join(texto.split())
        if not nome:
            return ["Preciso do seu nome para o pedido.", "Qual é o seu nome?"]
        if pergunta_ou_nao_sei(nome):
            return [
                "Não consigo descobrir o seu nome sozinho. Me diga como você se chama. "
                "Se preferir, escreva atendente.",
                "Qual é o seu nome?",
            ]
        self._nome = nome
        self._nome_lembrado = nome
        if self._corrigindo:
            return self._voltar_ao_resumo()
        if self._produto_id is not None and self._quantidade > 0:
            return [f"Olá, {nome.split()[0]}!", *self._seguir()]
        return [f"Olá, {nome.split()[0]}!", *self._perguntar_produto()]

    def _passo_produto(self, texto: str) -> list[str]:
        indice = opcao(texto, [p.nome for _, p in self._produtos])
        if indice is None:
            # Tenta casar pelo nome parcial do produto (ex.: "gás", "botijão", "água").
            msg = normalizar(texto)
            achados = [i for i, (_, p) in enumerate(self._produtos) if normalizar(p.nome) in msg]
            if len(achados) == 1:
                self._produto_id = self._produtos[achados[0]][0]
                livre = extrair_pedido_livre(texto)
                if livre is not None and livre.quantidade is not None and 1 <= livre.quantidade <= MAX_QUANTIDADE:
                    return self._ler_modelo(livre)
                self._passo = "QUANTIDADE"
                return ["Quantos?"]
            return [DA_LISTA, self._pergunta_produto()]
        self._produto_id = self._produtos[indice][0]
        self._passo = "QUANTIDADE"
        return ["Quantos?"]

    def _passo_quantidade(self, texto: str) -> list[str]:
        recusa = [f"Envie um número de 1 a {MAX_QUANTIDADE}.", "Quantos?"]
        if "," in texto and parece_modelo(texto):
            return self._ler_modelo(ler_modelo(texto))
        livre = extrair_pedido_livre(texto)
        if livre is not None and livre.quantidade is not None and 1 <= livre.quantidade <= MAX_QUANTIDADE:
            if livre.pagamento is not None or livre.troco is not None:
                return self._ler_modelo(livre)
            self._quantidade = livre.quantidade
            return self._seguir()
        quantidade = quantidade_do_texto(texto)
        if quantidade is None or not 1 <= quantidade <= MAX_QUANTIDADE:
            return recusa
        self._quantidade = quantidade
        return self._seguir()

    def _passo_endereco_do_cadastro(self, texto: str) -> list[str]:
        resposta = sim_ou_nao(texto)
        if resposta is None:
            return [SIM_OU_NAO, self._pergunta_endereco_do_cadastro()]
        if resposta is False:
            return self._comecar_endereco()
        self._endereco = self._endereco_do_cadastro
        return self._seguir()

    def _comecar_endereco(self, ditos: dict[str, str] | None = None) -> list[str]:
        self._campos: dict[str, str | None] = {}
        self._passo = "CAMPO_DO_ENDERECO"
        if ditos:
            self._usar_campos(ditos)
            self._completar_estado()
            if any(c[0] not in self._campos for c in CAMPOS_DO_ENDERECO):
                return ["Anotei o endereço que você mandou.", self._pergunta_do_campo()]
            return self._proximo_campo_ou_fim()
        return ["Agora o endereço de entrega.", self._pergunta_do_campo()]

    def _completar_estado(self) -> None:
        """Cidade e estado não são perguntados: Teresina, Piauí por padrão (Timon → Maranhão).
        Se o cliente já tiver informado cidade/estado (ex.: em frase livre), o valor dele vence."""
        if "bairro" in self._campos and "cidade" not in self._campos:
            self._campos["cidade"] = "Teresina"
        if "cidade" in self._campos and "estado" not in self._campos:
            cidade = self._campos["cidade"] or ""
            self._campos["estado"] = nome_do_estado("MA" if normalizar(cidade) == "timon" else "PI")

    def _ler_modelo(self, modelo) -> list[str]:
        if modelo is None:
            return self._seguir()
        if modelo.quantidade is not None and 1 <= modelo.quantidade <= MAX_QUANTIDADE:
            self._quantidade = modelo.quantidade
        campos = {
            nome: valor
            for nome, valor in (
                ("logradouro", modelo.logradouro),
                ("numero", modelo.numero),
                ("bairro", modelo.bairro),
                ("cidade", modelo.cidade),
                ("estado", modelo.estado),
                ("complemento", modelo.complemento),
            )
            if valor
        }
        if campos:
            self._endereco_do_texto = campos
        if modelo.pagamento == "CARTAO":
            self._cartao_sem_tipo = True
        elif modelo.pagamento == "OUTRA":
            return self._transferir(
                "Para outra forma de pagamento, vou te passar para um atendente.",
                "Cliente pediu outra forma de pagamento.",
            )
        elif modelo.pagamento in self._formas:
            self._pagamento = modelo.pagamento
            self._pagamento_pix = None
            self._troco = modelo.troco if modelo.pagamento == DINHEIRO else None
            self._troco_respondido = self._troco is not None
            self._serial_valegas = None
        return self._seguir()

    def _usar_campos(self, ditos: dict[str, str]) -> None:
        self._campos.update({k: v for k, v in ditos.items() if k not in self._campos})
        for nome, _, opcional, _ in CAMPOS_DO_ENDERECO:
            if opcional and nome not in self._campos:
                self._campos[nome] = None

    def _ja_informado(self, campo: str, pergunta: str) -> list[str]:
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
        if opcional and quer_pular(valor):
            self._campos[campo] = None
            return self._proximo_campo_ou_fim()
        if valor and campo != "estado" and pergunta_ou_nao_sei(valor):
            return self._nao_e_um_dado(campo, pergunta)
        if valor and campo == "logradouro":
            mais_que_a_rua, trecho = rua_do_texto(valor)
            repetida = "," not in valor and normalizar(valor) == self._rua_recusada
            if mais_que_a_rua and not repetida:
                if trecho is None:
                    self._rua_recusada = normalizar(valor)
                    return ["Me diga só o nome da rua (ex.: Rua das Flores).", pergunta]
                self._campos["logradouro"] = trecho
                return [f"Anotei a rua: {trecho}.", *self._proximo_campo_ou_fim()]
        if not valor:
            return [explicacao, pergunta]
        if campo == "estado":
            valor = nome_do_estado(valor)
            if valor is None:
                return [explicacao, pergunta]
        elif campo == "numero":
            numero = numero_do_endereco(valor)
            if numero is None:
                if fala_de_apartamento(valor):
                    return [
                        "O apartamento ou bloco vai no complemento.",
                        f"Qual é o número do prédio ou da casa?\n{OPCAO_PULAR}",
                    ]
                return [f"Envie o número da casa ou do prédio (ex.: 123) ou escolha {OPCAO_PULAR}.", pergunta]
            valor = numero
        self._campos[campo] = valor
        return self._proximo_campo_ou_fim()

    def _nao_e_um_dado(self, campo: str, pergunta: str) -> list[str]:
        rotulo, exemplo = ROTULOS_DOS_CAMPOS[campo]
        return [
            f"Não consigo descobrir o endereço sozinho. Me diga {rotulo} (ex.: {exemplo}). "
            "Se preferir, escreva atendente.",
            pergunta,
        ]

    def _campo_atual(self) -> tuple[str, str, bool, str]:
        return next(c for c in CAMPOS_DO_ENDERECO if c[0] not in self._campos)

    def _proximo_campo_ou_fim(self) -> list[str]:
        self._completar_estado()
        if any(c[0] not in self._campos for c in CAMPOS_DO_ENDERECO):
            return [self._pergunta_do_campo()]
        self._endereco = Endereco(**self._campos)
        if self._corrigindo:
            return self._voltar_ao_resumo()
        return self._seguir()

    def _passo_pagamento(self, texto: str) -> list[str]:
        opcoes = [*[ROTULOS_DE_PAGAMENTO[t] for t in self._formas], "Outra forma"]
        indice = opcao(texto, opcoes)
        escolhida: str | None = None
        if indice is not None:
            escolhida = "OUTRA" if indice == len(self._formas) else self._formas[indice]
        else:
            forma = forma_de_pagamento(texto)
            if forma == "CARTAO":
                return ["Cartão de crédito ou de débito?", self._pergunta_pagamento()]
            if forma == "OUTRA":
                escolhida = "OUTRA"
            elif forma in self._formas:
                escolhida = forma
        if escolhida is None:
            return [DA_LISTA, self._pergunta_pagamento()]
        if escolhida == "OUTRA":
            return self._transferir(
                "Para outra forma de pagamento, vou te passar para um atendente.",
                "Cliente escolheu outra forma de pagamento.",
            )
        self._pagamento = escolhida
        self._pagamento_pix = None
        self._troco = None
        self._troco_respondido = False
        self._serial_valegas = None
        if self._pagamento == "PIX":
            self._passo = "MOMENTO_PIX"
            return [PERGUNTA_MOMENTO_PIX]
        if self._pagamento == DINHEIRO:
            self._passo = "TROCO"
            return [PERGUNTA_DO_TROCO]
        if self._pagamento == VALEGAS:
            self._passo = "SERIAL_VALEGAS"
            return [PERGUNTA_DO_SERIAL_VALEGAS]
        return self._voltar_ao_resumo()

    def _passo_momento_pix(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_MOMENTO_PIX)
        if escolha is None:
            msg = normalizar(texto)
            if any(p in msg for p in ("agora", "logo", "ja", "antecipad", "adiantad")):
                escolha = 0
            elif any(p in msg for p in ("entrega", "chegar", "entregador", "depois")):
                escolha = 1
        if escolha is None:
            return [DA_LISTA, PERGUNTA_MOMENTO_PIX]
        self._pagamento_pix = "AGORA" if escolha == 0 else "ENTREGA"
        return self._voltar_ao_resumo()

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
        self._troco_respondido = True
        return self._voltar_ao_resumo()

    def _passo_serial_valegas(self, texto: str) -> list[str]:
        codigo = " ".join(texto.split())
        if not codigo or pergunta_ou_nao_sei(codigo):
            return ["Preciso do código impresso no vale-gás.", PERGUNTA_DO_SERIAL_VALEGAS]
        if len(codigo) != 7:
            return ["O código do vale-gás precisa ter exatamente 7 caracteres (ex.: 1234567).", PERGUNTA_DO_SERIAL_VALEGAS]
        self._serial_valegas = codigo
        return self._voltar_ao_resumo()

    def _passo_resumo(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DO_RESUMO)
        if escolha is None and sim_ou_nao(texto) is True:
            escolha = 0
        if escolha is None:
            return [DA_LISTA, self._resumo()]
        if escolha == 1:
            self._passo = "CORRIGIR"
            return [PERGUNTA_DO_QUE_CORRIGIR]
        if escolha == 2:
            self._passo = "ENCERRADA"
            return ["Pedido descartado. Nada foi enviado."]
        resultado = self._d.registrador.registrar(self._pedido())
        if isinstance(resultado, VendaCriada):
            self._passo = "POS_VENDA"
            self._ultima_venda_id = resultado.id
            if self._endereco is not None:
                self._endereco_lembrado = self._endereco
            mensagens = [
                f"Pedido registrado! Número {resultado.id}, total {reais(resultado.total)}. "
                + ENSINA_O_POS_VENDA
            ]
            if self._pagamento == "PIX" and self._pagamento_pix == "AGORA":
                mensagens.append("Assim que fizer o PIX, guarde o comprovante para apresentar se a equipe pedir.")
            return mensagens
        if isinstance(resultado, EmAndamento):
            return ["Seu pedido já está sendo registrado. Aguarde um instante."]
        if isinstance(resultado, PedidoInvalido):
            return self._corrigir_o_que_a_api_recusou(resultado)
        return self._transferir_venda(resultado)

    def _corrigir_o_que_a_api_recusou(self, invalido: PedidoInvalido) -> list[str]:
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
        if campo == "serial":
            self._passo = "SERIAL_VALEGAS"
            return [aviso, PERGUNTA_DO_SERIAL_VALEGAS]
        self._corrigindo = False
        return self._transferir(
            "Não consegui concluir seu pedido agora.",
            f"Pedido recusado na validação (campo {campo}).\n{self._resumo_do_pedido()}",
        )

    def _transferir_venda(self, transferir: TransferirAtendente) -> list[str]:
        self._passo = "ENCERRADA"
        if transferir.venda_pode_existir:
            _log.error(
                "Venda possivelmente criada com resultado incerto; pedido_id=%s marca=%s",
                self._pedido_id,
                marca(self._pedido_id),
            )
            return [
                "Não consegui concluir seu pedido agora. Uma pessoa da nossa equipe vai continuar o atendimento.",
                NAO_REFACA_O_PEDIDO,
            ]
        return [
            "Não consegui concluir seu pedido agora. "
            "Uma pessoa da nossa equipe vai continuar o atendimento."
        ]

    def _passo_corrigir(self, texto: str) -> list[str]:
        escolha = opcao(texto, O_QUE_CORRIGIR)
        if escolha is None:
            return [DA_LISTA, PERGUNTA_DO_QUE_CORRIGIR]
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
        if normalizar(texto) in ("oi", "ola", "novo pedido", "novo", "outro pedido"):
            self._novo_rascunho()
            return self.iniciar()
        return [
            f"Seu pedido número {self._ultima_venda_id} já foi registrado. "
            f"{ENSINA_O_POS_VENDA} Para um novo pedido, escreva novo pedido."
        ]

    def _passo_encerrada(self, texto: str) -> list[str]:
        if normalizar(texto) in ("oi", "ola", "novo pedido", "novo"):
            self._novo_rascunho()
            return self.iniciar()
        return ["Para um novo pedido, envie oi."]

    # --- devolução na conversa ---------------------------------------------------

    def _devolucao_pedida(self, texto: str, pedido: str | None) -> list[str] | None:
        if self._d.devolucao is None:
            return None
        if self._passo in PASSOS_DE_TEXTO_LIVRE and pedido != "CANCELAR_OU_DEVOLVER":
            return None
        dev = pedido_de_devolucao(texto)
        if dev is None:
            if pedido != "CANCELAR_OU_DEVOLVER":
                return None
            dev = PedidoDeDevolucao(None)
        if self._passo in ("RESUMO", "CORRIGIR") and dev.tipo in ("CANCELAMENTO", None):
            return None
        if (
            self._passo in PASSOS_DO_RASCUNHO and self._passo != "PRODUTO"
        ) or self._passo in ("RESUMO", "CORRIGIR"):
            self._devolucao_pendente = (dev, texto, self._passo)
            self._passo = "RASCUNHO_OU_PEDIDO"
            return [PERGUNTA_DO_RASCUNHO]
        return self._comecar_devolucao(dev, texto)

    def _passo_rascunho_ou_pedido(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DO_RASCUNHO)
        if escolha is None:
            return [DA_LISTA, PERGUNTA_DO_RASCUNHO]
        dev, original, passo_de_antes = self._devolucao_pendente
        if escolha == 0:
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
            "MOMENTO_PIX": lambda: PERGUNTA_MOMENTO_PIX,
            "TROCO": lambda: PERGUNTA_DO_TROCO,
            "SERIAL_VALEGAS": lambda: PERGUNTA_DO_SERIAL_VALEGAS,
            "RESUMO": self._resumo,
            "CORRIGIR": lambda: PERGUNTA_DO_QUE_CORRIGIR,
        }
        return perguntas[passo]()

    def _comecar_devolucao(self, dev: PedidoDeDevolucao, texto: str, *, aviso: str | None = None) -> list[str]:
        self._dev: dict = {
            "tipo": dev.tipo,
            "motivo": dev.motivo,
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
        if dev.tipo == "CANCELAMENTO" and self._ultima_venda_id is not None:
            self._passo = "DEV_ESTE_PEDIDO"
            return mensagens + [self._pergunta_deste_pedido()]
        return mensagens + self._dev_listar()

    def _pergunta_deste_pedido(self) -> str:
        return (
            f"Quer cancelar o pedido {self._ultima_venda_id}, que você acabou de fazer?\n"
            "1) Sim\n2) É outro pedido"
        )

    def _passo_dev_este_pedido(self, texto: str) -> list[str]:
        escolha = opcao(texto, ["sim", "é outro pedido"])
        if escolha is None and sim_ou_nao(texto) is not None:
            escolha = 0 if sim_ou_nao(texto) else 1
        if escolha is None:
            return [DA_LISTA, self._pergunta_deste_pedido()]
        if escolha == 1:
            return self._dev_listar()
        venda = self._ultima_venda_id
        self._tentativa_de_devolucao = f"Tentou cancelar o pedido {venda}."
        if self._pode_listar():
            resultado = self._d.devolucao.pedidos_do_cliente(self._telefone)
            if isinstance(resultado, PedidosEncontrados):
                for numero, pedido in enumerate(resultado.pedidos, 1):
                    if pedido.id == venda:
                        self._dev.update(encontrados=resultado, escolha=numero)
                        return self._apos_escolher_pedido(pedido)
        self._dev["nota"] = f"Pedido {venda}, feito nesta conversa (não apareceu na busca)."
        return self._dev_motivo()

    def _anotar_tentativa(self) -> None:
        acao = {
            "CANCELAMENTO": "cancelar",
            "ESTORNO": "pedir a devolução do valor de",
            "PROBLEMA_PRODUTO": "relatar um problema com",
        }.get(self._dev["tipo"])
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
            if len(resultado.pedidos) == 1:
                self._passo = "DEV_UM_PEDIDO"
                return [self._pergunta_do_pedido_unico()]
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

    def _pergunta_do_pedido_unico(self) -> str:
        pedido = self._dev["encontrados"].pedidos[0]
        return f"Encontrei seu pedido. {pedido.texto_para_cliente()}.\nÉ esse?\n1) Sim\n2) Não"

    def _passo_dev_um_pedido(self, texto: str) -> list[str]:
        escolha = opcao(texto, ["sim", "não"])
        if escolha is None and sim_ou_nao(texto) is not None:
            escolha = 0 if sim_ou_nao(texto) else 1
        if escolha is None:
            return [DA_LISTA, self._pergunta_do_pedido_unico()]
        if escolha == 1:
            return ["Tudo bem, vou registrar e a equipe confere qual é o pedido.", *self._dev_motivo()]
        self._dev["escolha"] = 1
        self._anotar_tentativa()
        return self._apos_escolher_pedido(self._dev["encontrados"].pedidos[0])

    def _apos_escolher_pedido(self, pedido: PedidoDoCliente) -> list[str]:
        if self._dev["tipo"] == "CANCELAMENTO" and pedido.cancelado:
            if not self._perguntar_se_pagou():
                self._fim_da_devolucao()
                return ["Esse pedido já está cancelado. Como o pagamento seria na entrega, não há valor a devolver."]
            self._dev["ja_cancelado"] = True
            self._passo = "DEV_PAGOU"
            return ["Esse pedido já está cancelado.", "Você já pagou esse pedido? (sim/não)"]
        if self._dev["tipo"] == "CANCELAMENTO" and pedido.entregue:
            self._passo = "DEV_ENTREGUE"
            return [PERGUNTA_DO_ENTREGUE]
        return self._dev_motivo()

    def _passo_dev_entregue(self, texto: str) -> list[str]:
        escolha = opcao(texto, OPCOES_DO_ENTREGUE)
        if escolha is None:
            return [DA_LISTA, PERGUNTA_DO_ENTREGUE]
        if escolha == 2:
            return self._comando("ATENDENTE", texto)
        if escolha == 0:
            self._dev.update(tipo="PROBLEMA_PRODUTO", motivo="OUTRO", texto=None)
        else:
            self._dev.update(tipo="ESTORNO", motivo=None)
        self._anotar_tentativa()
        return self._dev_motivo()

    def _lista_de_pedidos(self) -> str:
        pedidos = self._dev["encontrados"].pedidos
        linhas = [f"{n}) {p.texto_para_cliente()}" for n, p in enumerate(pedidos, 1)]
        return "Qual pedido?\n" + "\n".join(linhas) + "\n0) Não achei meu pedido"

    def _passo_dev_pedido(self, texto: str) -> list[str]:
        pedidos = self._dev["encontrados"].pedidos
        resposta = normalizar(texto).rstrip(")")
        if resposta == "0":
            return self._dev_motivo()
        if not resposta.isdigit() or not 1 <= int(resposta) <= len(pedidos):
            return ["Escolha um número da lista.", self._lista_de_pedidos()]
        self._dev["escolha"] = int(resposta)
        self._anotar_tentativa()
        return self._apos_escolher_pedido(pedidos[int(resposta) - 1])

    def _perguntar_se_pagou(self) -> bool:
        pedido = self._pedido_escolhido()
        formas = pedido.pagamentos if pedido else None
        return formas is None or "PIX" in formas

    def _motivos(self) -> list[tuple[str, str]]:
        return list(REGRAS[self._dev["tipo"]].motivos.items())

    def _pergunta_do_motivo(self) -> str:
        return "Qual o motivo?\n" + "\n".join(
            f"{n}) {rotulo[:1].upper()}{rotulo[1:]}" for n, (_, rotulo) in enumerate(self._motivos(), 1)
        )

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
                return self._dev_confirmar()
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
        linhas.append("Confirma?\n1) Sim\n2) Não")
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
        except Exception:  # noqa: BLE001
            _log.error("não foi possível registrar uma solicitação de devolução na fila")
            return ["Não consegui registrar sua solicitação agora. Tente de novo em alguns minutos ou escreva atendente."]
        if isinstance(resultado, SolicitacaoAberta):
            final = resultado.mensagem_para_cliente + " Se precisar de comprovante, ela vai pedir."
            return [AVISO_DO_ENTREGADOR, final] if resultado.entregador_pode_ter_saido else [final]
        return self._transferir(
            "Não consegui registrar sua solicitação.",
            "Solicitação de devolução recusada na validação: " + ", ".join(resultado.campos()),
        )

    def _fim_da_devolucao(self) -> None:
        self._passo = "POS_VENDA" if self._ultima_venda_id is not None else "ENCERRADA"

    # --- progressão da máquina de estados ----------------------------------------

    def _seguir(self) -> list[str]:
        """Vai para o primeiro item do pedido que ainda falta (ou para o resumo)."""
        if not self._quantidade:
            self._passo = "QUANTIDADE"
            return ["Quantos?"]
        if self._endereco is None:
            if self._endereco_do_texto:
                ditos, self._endereco_do_texto = self._endereco_do_texto, None
                return self._comecar_endereco(ditos)
            if self._endereco_do_cadastro is not None:
                self._passo = "ENDERECO_DO_CADASTRO"
                return [self._pergunta_endereco_do_cadastro()]
            return self._comecar_endereco()
        if not self._pagamento:
            self._passo = "PAGAMENTO"
            if self._cartao_sem_tipo:
                self._cartao_sem_tipo = False
                return ["Cartão de crédito ou de débito?", self._pergunta_pagamento()]
            return [self._pergunta_pagamento()]
        if self._pagamento == "PIX" and not self._pagamento_pix:
            self._passo = "MOMENTO_PIX"
            return [PERGUNTA_MOMENTO_PIX]
        if self._pagamento == DINHEIRO and self._troco is not None and self._troco < self._total():
            self._troco = None
            self._troco_respondido = False
        if self._pagamento == DINHEIRO and not self._troco_respondido:
            self._passo = "TROCO"
            return [PERGUNTA_DO_TROCO]
        if self._pagamento == VALEGAS and not self._serial_valegas:
            self._passo = "SERIAL_VALEGAS"
            return [PERGUNTA_DO_SERIAL_VALEGAS]
        return self._voltar_ao_resumo()

    # --- cliente e transferência ---------------------------------------------------

    def _buscar_cliente_do_canal(self) -> list[dict] | None:
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
        self._passo = "ENCERRADA"
        _ = texto
        return [
            self._d.seguranca,
            "Situação urgente: siga as orientações acima e ligue imediatamente para os Bombeiros (193) ou para a revenda.",
        ]

    def _transferir(
        self,
        aviso_ao_cliente: str,
        resumo: str,
        *,
        texto_cliente: str | None = None,
        dados: dict | None = None,
    ) -> list[str]:
        self._passo = "ENCERRADA"
        aviso = f"{aviso_ao_cliente} " if aviso_ao_cliente else ""
        venda_id = dados.get("venda_id") if isinstance(dados, dict) else None
        if isinstance(venda_id, int) and self._anotar_atendimento_na_venda(venda_id, resumo, texto_cliente):
            return [
                f"{aviso}Registrei sua solicitação no pedido {venda_id} (protocolo ERP-{venda_id}). "
                "Uma pessoa da nossa equipe vai continuar o atendimento."
            ]
        return [f"{aviso}Uma pessoa da nossa equipe vai continuar o atendimento."]

    def _anotar_atendimento_na_venda(self, venda_id: int, resumo: str, texto_cliente: str | None) -> bool:
        try:
            atual = self._d.api.buscar_venda(venda_id)
        except TempoEsgotado:
            return False
        if atual.status != 200 or not isinstance(atual.dados, dict):
            return False
        obs_atual = str(atual.dados.get("observation") or "").strip()
        partes = ["[CHATBOT][ATENDIMENTO]", " ".join(resumo.split())]
        if texto_cliente and texto_cliente.strip():
            partes.append(f"Relato: {mascarar(' '.join(texto_cliente.split()))[:200]}")
        anotacao = " ".join(partes)
        nova_obs = f"{obs_atual} | {anotacao}" if obs_atual else anotacao
        corpo = montar_corpo_atualizacao_obs(atual.dados, nova_obs)
        try:
            resposta = self._d.api.atualizar_venda(venda_id, corpo)
        except TempoEsgotado:
            return False
        return 200 <= resposta.status < 300

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
        if len(self._produtos) == 1:
            self._produto_id, produto = self._produtos[0]
            self._passo = "QUANTIDADE"
            return [f"{produto.nome[:1].upper()}{produto.nome[1:]} ({reais(produto.preco)}). Quantos?"]
        self._passo = "PRODUTO"
        return [self._pergunta_produto()]

    def _pergunta_produto(self) -> str:
        linhas = [
            f"{n}) {p.nome[:1].upper()}{p.nome[1:]} — {reais(p.preco)}" for n, (_, p) in enumerate(self._produtos, 1)
        ]
        return "O que você quer pedir?\n" + "\n".join(linhas)

    def _pergunta_pagamento(self) -> str:
        linhas = [f"{n}) {ROTULOS_DE_PAGAMENTO[t]}" for n, t in enumerate(self._formas, 1)]
        linhas.append(f"{len(self._formas) + 1}) Outra forma")
        return "Como vai pagar?\n" + "\n".join(linhas)

    def _total(self) -> Decimal:
        return self._d.catalogo[self._produto_id].preco * self._quantidade

    def _resumo(self) -> str:
        return "\n".join(
            ["Confira seu pedido:", self._resumo_do_pedido(), "Responda: 1) Confirmar  2) Corrigir  3) Cancelar"]
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
                *([f"Quadra: {e.quadra}"] if e.quadra else []),
                *([f"Complemento: {e.complemento}"] if e.complemento else []),
                *([f"Referência: {e.referencia}"] if e.referencia else []),
                f"Pagamento: {self._descricao_do_pagamento()}",
            ]
        )

    def _descricao_do_pagamento(self) -> str:
        rotulo = ROTULOS_DE_PAGAMENTO[self._pagamento]
        if self._pagamento == "PIX":
            if self._pagamento_pix == "AGORA":
                return f"{rotulo} (pagar agora)"
            if self._pagamento_pix == "ENTREGA":
                return f"{rotulo} (pagar na entrega)"
            return rotulo
        if self._pagamento == DINHEIRO:
            return f"{rotulo}, troco para {reais(self._troco)}" if self._troco else f"{rotulo}, sem troco"
        if self._pagamento == VALEGAS and self._serial_valegas:
            return f"{rotulo} (código {self._serial_valegas})"
        return rotulo

    def _pedido(self) -> Pedido:
        return Pedido(
            pedido_id=self._pedido_id,
            telefone=self._telefone,
            nome=self._nome,
            itens=(Item(product_id=self._produto_id, quantidade=self._quantidade),),
            endereco=self._endereco,
            pagamentos=(
                Pagamento(
                    tipo=self._pagamento,
                    valor=self._total(),
                    serial=self._serial_valegas if self._pagamento == VALEGAS else None,
                ),
            ),
            troco_para=self._troco,
            pagamento_pix=self._pagamento_pix if self._pagamento == "PIX" else None,
        )


_CAMPOS_OBRIGATORIOS_DO_CADASTRO = ("location", "number", "neighborhood", "city", "state_federation")


def _endereco_do_cadastro(cliente: dict) -> Endereco | None:
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

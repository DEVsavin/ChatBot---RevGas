"""Regras de entrada da conversa: entender o que o cliente digitou.

Funções puras: recebem texto e devolvem o valor entendido, ou None quando
não dá para entender (a conversa explica e repete a pergunta).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
import unicodedata
from decimal import Decimal

from revgas_chatbot.conversa.estados import nome_do_estado


def _tem_digito_nao_ascii(texto: str) -> bool:
    """Dígitos de outros alfabetos ("１", "٨") contam como dígito em Python, mas não para a API (I1)."""
    return any(c.isdigit() and not c.isascii() for c in texto)


def telefone(texto: str) -> str | None:
    """Telefone no formato da API: 55 + DDD + número (12 ou 13 dígitos).

    Aceita formatação comum: "(86) 99999-0000", "+55 86 99999-0000".
    Sem o 55, prefixa (10 ou 11 dígitos). Outro tamanho → None.
    """
    if _tem_digito_nao_ascii(texto):
        return None
    digitos = "".join(c for c in texto if c.isdigit())
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        return digitos
    return None


DDD_FICTICIO = "00"  # não existe no Brasil: números de demonstração


def numero_ficticio(numero: str) -> bool:
    """Número de demonstração (DDD 00), no formato da API ("5500...")."""
    return numero[2:4] == DDD_FICTICIO


def com_nono_digito(numero: str) -> str:
    """Celular brasileiro que chegou sem o 9 ganha o 9: "558681941234" -> "5586981941234".

    12 dígitos (55 + DDD + 8) com o número começando em 6, 7, 8 ou 9 é celular.
    Fixo (2 a 5) e o fictício (DDD 00, número começando em 0) não mudam.
    """
    if len(numero) == 12 and numero.startswith("55") and numero[4] in "6789":
        return numero[:4] + "9" + numero[4:]
    return numero


def telefone_legivel(numero: str) -> str:
    """"5586999990000" -> "(86) 99999-0000"; "558633330000" -> "(86) 3333-0000"."""
    ddd, resto = numero[2:4], numero[4:]
    return f"({ddd}) {resto[:-4]}-{resto[-4:]}"


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, espaços simples e sem pontuação nas pontas."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.casefold().split()).strip(" .,!?;:")


_SIM = frozenset({"sim", "s", "isso", "pode", "confirmo"})
_NAO = frozenset({"nao", "n"})


def sim_ou_nao(texto: str) -> bool | None:
    """True para sim, False para não, None se não for nem um nem outro."""
    resposta = normalizar(texto)
    if resposta in _SIM:
        return True
    if resposta in _NAO:
        return False
    return None


def opcao(texto: str, opcoes: list[str]) -> int | None:
    """Índice (a partir de 0) da opção escolhida pelo número ("2", "2)") ou pelo texto."""
    resposta = normalizar(texto).rstrip(")")
    if resposta.isdigit():
        numero = int(resposta)
        return numero - 1 if 1 <= numero <= len(opcoes) else None
    for indice, rotulo in enumerate(opcoes):
        if resposta == normalizar(rotulo):
            return indice
    return None


_SO_DECIMAL = re.compile(r"\d+([.,]\d{1,2})?", re.ASCII)  # 200 · 200,50 · 200.50
_COM_MILHAR = re.compile(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", re.ASCII)  # 1.200 · 1.200,00


def valor_em_reais(texto: str) -> Decimal | None:
    """Valor em reais > 0 com até 2 casas: "200", "200,00", "R$ 1.200,50"."""
    if _tem_digito_nao_ascii(texto):
        return None  # normalizar trocaria ou apagaria esses dígitos em silêncio
    limpo = normalizar(texto).replace("r$", "").replace(" ", "")
    if _COM_MILHAR.fullmatch(limpo):
        limpo = limpo.replace(".", "").replace(",", ".")
    elif _SO_DECIMAL.fullmatch(limpo):
        limpo = limpo.replace(",", ".")
    else:
        return None
    valor = Decimal(limpo)
    return valor if valor > 0 else None


# Comandos que valem em qualquer passo (lista inicial; será substituída no C2).
# Casam só como palavra ou frase INTEIRA, já normalizadas (sem acento).
_VAZAMENTO = ("vazamento", "vazando", "cheiro de gas", "cheirando gas")
_COMANDOS = {
    "CANCELAR_OU_DEVOLVER": ("cancelar", "cancela", "devolver", "devolucao", "estorno", "reembolso"),
    "ATENDENTE": ("atendente", "humano", "pessoa"),
    "SAIR": ("sair", "parar", "tchau"),
}


def _contem(texto: str, frases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(frase)}\b", texto) for frase in frases)


def comando(texto: str, *, livre: bool) -> str | None:
    """Comando global na mensagem, ou None.

    - Vazamento vale sempre, em qualquer lugar da mensagem (prioridade).
    - Nos passos de texto livre (`livre=True`: nome, endereço), os outros
      comandos só valem se forem a mensagem inteira ("Rua do Atendente, 10"
      é endereço). Nos demais passos, valem como palavra inteira.
    """
    mensagem = normalizar(texto)
    if _contem(mensagem, _VAZAMENTO):
        return "VAZAMENTO"
    for nome, palavras in _COMANDOS.items():
        if livre:
            if mensagem in palavras:
                return nome
        elif _contem(mensagem, palavras):
            return nome
    return None


# A1: palavras que, no começo da mensagem, indicam pergunta ou dúvida.
_DUVIDA = ("qual", "quais", "como", "onde", "voce", "vc", "sera", "nao sei", "sei la", "nao lembro")


def pergunta_ou_nao_sei(texto: str) -> bool:
    """A1: "qual é a rua?", "não sei", "você consegue pegar" não são um dado.

    Regra simples: termina com "?" ou COMEÇA com uma palavra de dúvida
    (palavra no meio, como em "Rua Onde Canta o Sabiá", não conta).
    """
    if texto.strip().endswith("?"):
        return True
    mensagem = normalizar(texto)
    return any(mensagem == p or mensagem.startswith(p + " ") for p in _DUVIDA)


_SEM_NUMERO = frozenset({"s/n", "sn", "s n", "sem numero", "n", "s.n", "s.n.", "s n.", "sem n"})  # ticket 02


def fala_de_apartamento(texto: str) -> bool:
    """"ap409", "apto 12", "bloco b": o apartamento ou bloco vai no complemento."""
    mensagem = normalizar(texto)
    return bool(re.match(r"(ap|apt|apto|apartamento|bloco|bl|bloc)\b|(ap|apt|apto)\d", mensagem))
_NUMERO = re.compile(r"(\d{1,6}) ?([a-z])?", re.ASCII)


def numero_do_endereco(texto: str) -> str | None:
    """A2: dígitos com uma letra opcional ("10", "10A") ou "S/N"; o resto → None.

    "ap409", "apto 12", "bloco b" não são o número do prédio (vão no complemento).
    """
    if _tem_digito_nao_ascii(texto):
        return None
    mensagem = normalizar(texto)
    if mensagem in _SEM_NUMERO:
        return "S/N"
    encontrado = _NUMERO.fullmatch(mensagem)
    if encontrado is None:
        return None
    return encontrado.group(1) + (encontrado.group(2) or "").upper()


# Problema 1 (conversas de 15/11): "já informei" não é um dado do endereço.
_JA_INFORMADO = (
    "ja informei", "ja falei", "ja disse", "ja mandei", "ja passei", "ja enviei", "ja coloquei",
    "ali em cima", "la em cima", "como eu disse", "como eu falei", "como falei", "como disse",
)


def ja_informado(texto: str) -> bool:
    """A pessoa diz que já mandou o dado ("já informei", "tá lá em cima")."""
    mensagem = normalizar(texto)
    return _contem(mensagem, _JA_INFORMADO)


# Devolução na conversa (fase C): qual dos 3 cenários a frase pede.
# Ordem: problema no produto, cobrança/estorno, cancelamento, genérico.
# DC-4 (usuário, 18/11): "demorou"/"tá demorando" NÃO abrem cancelamento.
_PROBLEMA_NO_PRODUTO = (
    "lacre violado", "lacre rompido", "sem lacre", "lacre", "peso errado", "veio errado", "produto errado",
    "botijao errado", "veio vazio", "amassado", "com defeito", "defeito", "problema com o produto",
    "problema no produto", "problema com o botijao", "problema no botijao",
)
_GENERICOS_DO_PRODUTO = ("problema com o produto", "problema no produto", "problema com o botijao",
                         "problema no botijao")
_COBRANCA_ERRADA = ("paguei duas vezes", "paguei 2 vezes", "cobraram a mais", "cobrado a mais",
                    "cobraram duas vezes", "cobranca errada", "cobraram errado", "valor errado")
# I2 (revisão): "meu dinheiro" e "devolve" saíram (frases comuns no meio de um pedido).
_ESTORNO = ("estorno", "estornar", "reembolso", "dinheiro de volta", "devolver o dinheiro", "devolucao do dinheiro")
_CANCELAMENTO = {
    "desisti": "DESISTI", "desistir": "DESISTI", "nao quero mais": "DESISTI",
    "pedi errado": "PEDI_ERRADO",
    "cancelar": None, "cancela": None, "cancelamento": None, "cancele": None,
}
_DEVOLUCAO_GENERICA = ("devolver", "devolucao")


@dataclass(frozen=True)
class PedidoDeDevolucao:
    tipo: str | None  # CANCELAMENTO, PROBLEMA_PRODUTO, ESTORNO; None = perguntar qual
    motivo: str | None = None  # já dito na frase (ex.: DESISTI, COBRANCA_ERRADA)
    descreve: bool = False  # problema no produto: a frase já descreve o que aconteceu


def pedido_de_devolucao(texto: str) -> PedidoDeDevolucao | None:
    mensagem = normalizar(texto)
    if _contem(mensagem, _PROBLEMA_NO_PRODUTO):
        return PedidoDeDevolucao("PROBLEMA_PRODUTO", "OUTRO", descreve=not _contem(mensagem, _GENERICOS_DO_PRODUTO))
    if _contem(mensagem, _COBRANCA_ERRADA):
        return PedidoDeDevolucao("ESTORNO", "COBRANCA_ERRADA")
    if _contem(mensagem, _ESTORNO):
        return PedidoDeDevolucao("ESTORNO")
    for frase, motivo in _CANCELAMENTO.items():
        if _contem(mensagem, (frase,)):
            return PedidoDeDevolucao("CANCELAMENTO", motivo)
    if _contem(mensagem, _DEVOLUCAO_GENERICA):
        return PedidoDeDevolucao(None)
    return None


# Ticket 01 (achados de 18/11): sem IA, a resposta à rua às vezes é o endereço inteiro.
# I1 da revisão: sem vírgula, só palavras de pagamento marcam o endereço inteiro;
# "bairro"/"cidade"/"cep" só contam junto com um número ("Rua Cidade Nova" é rua).
_PAGAMENTO_NO_TEXTO = ("pagar", "pagamento", "pago", "pix", "credito", "debito", "cartao", "dinheiro")
_LUGAR_NO_TEXTO = ("bairro", "cidade", "cep")
_PREPOSICOES_DA_RUA = ("fica na ", "fica no ", "e na ", "e no ", "na ", "no ", "em ")


def rua_do_texto(texto: str) -> tuple[bool, str | None]:
    """(a resposta parece mais que a rua?, o trecho que dá para usar como rua com segurança).

    Parece mais que a rua: tem vírgula, palavras de pagamento ("pagar", "pix"...) ou
    "bairro"/"cidade" junto com um número.
    Trecho seguro: o que vem antes da 1ª vírgula, sem "na"/"no", sem essas
    palavras e sem terminar em número (senão, None: o chatbot pede só a rua).
    """
    mensagem = normalizar(texto)
    lugar_com_numero = _contem(mensagem, _LUGAR_NO_TEXTO) and bool(re.search(r"\d", mensagem))
    if "," not in texto and not _contem(mensagem, _PAGAMENTO_NO_TEXTO) and not lugar_com_numero:
        return False, None
    trecho = " ".join(texto.split(",")[0].split())
    for preposicao in _PREPOSICOES_DA_RUA:
        if normalizar(trecho).startswith(preposicao.strip() + " "):
            trecho = trecho[len(preposicao):].strip()
            break
    if (
        len(trecho) < 3
        or _contem(normalizar(trecho), _PAGAMENTO_NO_TEXTO)
        or re.match(r"(bairro|cidade|cep)\b", normalizar(trecho))
        or (_contem(normalizar(trecho), _LUGAR_NO_TEXTO) and re.search(r"\d", trecho))
        or re.search(r"\d\s*$", trecho)
        or _tem_digito_nao_ascii(trecho)
    ):
        return True, None
    return True, trecho


# Ticket 03 (achados de 18/11): "4 botijoes na rua teste…" → 4.
_POR_EXTENSO = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10,
}


def quantidade_do_texto(texto: str) -> int | None:
    """O número de unidades que a frase pede (sem conferir o limite), ou None.

    Ordem: a mensagem só com o número; o número no começo da frase; "N botijões";
    um número por extenso; o único número da frase.
    """
    if _tem_digito_nao_ascii(texto):
        return None
    mensagem = normalizar(texto)
    if mensagem.isdigit():
        return int(mensagem)
    for padrao in (r"(\d{1,3})\b", r".*?\b(\d{1,3})\s*botij"):
        encontrado = re.match(padrao, mensagem)
        if encontrado:
            return int(encontrado.group(1))
    extenso = [_POR_EXTENSO[p] for p in re.findall(r"[a-z]+", mensagem) if p in _POR_EXTENSO]
    if len(extenso) == 1:
        return extenso[0]
    numeros = re.findall(r"\d+", mensagem)
    return int(numeros[0]) if len(numeros) == 1 else None


# Pedido "agilizar" (18/11): o modelo "Quantidade, endereço, bairro, cidade, pagamento"
# lido por vírgulas, sem IA. O que não der para ler com segurança fica vazio (é perguntado depois).


@dataclass(frozen=True)
class Modelo:
    quantidade: int | None = None
    logradouro: str | None = None
    numero: str | None = None
    bairro: str | None = None
    cidade: str | None = None
    estado: str | None = None  # nome por extenso, se o cliente disse
    pagamento: str | None = None  # tipo da API, ou "CARTAO" (sem dizer qual)
    troco: Decimal | None = None
    complemento: str | None = None  # I2: "apto 301", "casa 2"... depois do número


# I1 (revisão): a 2ª parte só é endereço se tiver número ou começar como nome de rua.
_TIPOS_DE_RUA = ("rua", "r", "av", "avenida", "travessa", "tv", "trav", "alameda", "al", "quadra", "qd",
                 "conjunto", "conj", "rodovia", "estrada", "praca", "beco", "vila", "residencial", "largo")
# I2 (revisão): o que vem depois do número da casa é complemento.
_COMPLEMENTOS = ("apto", "apt", "ap", "apartamento", "casa", "bloco", "bl", "fundos", "lote", "lt", "sala",
                 "andar", "cond", "condominio", "torre", "box", "loja")


def parece_modelo(texto: str) -> bool:
    """3+ partes separadas por vírgula e a 2ª parece endereço ("quero 2, por favor" não é)."""
    partes = [" ".join(p.split()) for p in texto.split(",")]
    if len(partes) < 3:
        return False
    segunda = normalizar(partes[1])
    palavras = segunda.split()
    if palavras and palavras[0] in ("na", "no", "em"):
        palavras = palavras[1:]
    return bool(re.search(r"\d", segunda)) or bool(palavras and palavras[0].rstrip(".") in _TIPOS_DE_RUA)


_PALAVRA_BAIRRO = ("bairro", "bairo", "barrio", "bairr")
_TROCO = re.compile(r"troco\s*(?:para|pra|de|p/)?\s*(?:r\$)?\s*([\d.,]+)")


def forma_de_pagamento(texto: str) -> str | None:
    """"pix" → PIX; "crédito" → CARTAO_DE_CREDITO; "débito" → CARTAO_DE_DEBITO;
    "dinheiro" → MONEY; "cartão" sem dizer qual → "CARTAO"; outro → None."""
    mensagem = normalizar(texto)
    if _contem(mensagem, ("pix",)):
        return "PIX"
    if _contem(mensagem, ("credito",)):
        return "CARTAO_DE_CREDITO"
    if _contem(mensagem, ("debito",)):
        return "CARTAO_DE_DEBITO"
    if _contem(mensagem, ("dinheiro", "especie")):
        return "MONEY"
    if _contem(mensagem, ("cartao",)):
        return "CARTAO"
    return None


def _rua_e_numero(parte: str) -> tuple[str | None, str | None, str | None]:
    """(rua, número, complemento). I2 (revisão): o número é o 1º número da casa depois do nome
    da rua (no fim, ou seguido de "apto", "casa", "bloco"...); o que vem depois é complemento.
    "Rua 7 de Setembro 100" → ("Rua 7 de Setembro", "100", None)."""
    texto = " ".join(parte.split())
    for preposicao in _PREPOSICOES_DA_RUA:
        if normalizar(texto).startswith(preposicao.strip() + " "):
            texto = texto[len(preposicao):].strip()
            break
    palavras = texto.split()
    rua, numero, complemento = texto, None, None
    for i in range(1, len(palavras)):
        # "S/N" pode vir junto ("S/N") ou separado ("S N").
        for tamanho in (2, 1):
            candidato = " ".join(palavras[i:i + tamanho])
            depois = palavras[i + tamanho:]
            if len(palavras[i:i + tamanho]) < tamanho or numero_do_endereco(candidato) is None:
                continue
            if depois and normalizar(depois[0]) not in _COMPLEMENTOS:
                continue  # "7" de "Rua 7 de Setembro": faz parte do nome
            rua, numero = " ".join(palavras[:i]), numero_do_endereco(candidato)
            complemento = " ".join(depois) or None
            break
        if numero:
            break
    mais_que_a_rua, trecho = rua_do_texto(rua)
    if mais_que_a_rua:
        rua = trecho
    if not rua or len(rua) < 3 or numero_do_endereco(rua) or normalizar(rua.split()[0]) in _COMPLEMENTOS:
        return None, None, None  # sem um nome de rua seguro, nada é usado (é perguntado depois)
    return rua, numero, complemento


def ler_modelo(texto: str) -> Modelo | None:
    """Lê "2, Rua das Flores 123, Centro, Teresina, Pix" por posição. Sem vírgula → None."""
    if "," not in texto or _tem_digito_nao_ascii(texto):
        return None
    partes = [" ".join(p.split()) for p in texto.split(",")]
    while len(partes) < 5:
        partes.append("")
    campos: dict = {}
    if partes[0]:
        campos["quantidade"] = quantidade_do_texto(partes[0])
    if partes[1]:
        campos["logradouro"], campos["numero"], campos["complemento"] = _rua_e_numero(partes[1])
    if partes[2]:
        palavras = partes[2].split()
        if palavras and normalizar(palavras[0]) in _PALAVRA_BAIRRO:
            palavras = palavras[1:]
        campos["bairro"] = " ".join(palavras) or None
    if partes[3]:
        campos["cidade"] = partes[3]
    for parte in partes[4:]:  # pagamento, troco e (se o cliente disse) o estado, em qualquer ordem
        if not parte:
            continue
        mensagem = normalizar(parte)
        troco = _TROCO.search(mensagem)
        if troco:
            campos["troco"] = valor_em_reais(troco.group(1))
        forma = forma_de_pagamento(parte)
        if forma and "pagamento" not in campos:
            campos["pagamento"] = forma
        elif not troco and not forma and nome_do_estado(parte):
            campos["estado"] = nome_do_estado(parte)
    return Modelo(**campos)

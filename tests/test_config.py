"""Testes da leitura dos arquivos de configuração (catálogo e formas de pagamento)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from revgas_chatbot.config import carregar_catalogo, carregar_pagamentos
from revgas_chatbot.pedido import ProdutoCatalogo

CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_catalogo_real_tem_o_botijao_de_gas_a_110():
    catalogo = carregar_catalogo(CONFIG / "catalogo.json")
    assert catalogo == {26717: ProdutoCatalogo(nome="botijão de gás", preco=Decimal("110.00"))}


def test_pagamentos_reais_sao_os_quatro_escolhidos():
    assert carregar_pagamentos(CONFIG / "pagamentos.json") == {
        "PIX",
        "MONEY",
        "CARTAO_DE_DEBITO",
        "CARTAO_DE_CREDITO",
    }


def test_preco_e_lido_como_decimal_exato(tmp_path):
    arquivo = tmp_path / "catalogo.json"
    arquivo.write_text(json.dumps([{"product_id": 7, "nome": "x", "preco": "0.10"}]))
    assert carregar_catalogo(arquivo)[7].preco == Decimal("0.10")


@pytest.mark.parametrize("preco", ["0", "-1", "NaN", "10.005", 110.0])
def test_preco_invalido_no_catalogo_e_recusado(tmp_path, preco):
    # Preço como número JSON (110.0) também é recusado: escreva como texto "110.00".
    arquivo = tmp_path / "catalogo.json"
    arquivo.write_text(json.dumps([{"product_id": 7, "nome": "x", "preco": preco}]))
    with pytest.raises(ValueError):
        carregar_catalogo(arquivo)


def test_product_id_repetido_no_catalogo_e_recusado(tmp_path):
    arquivo = tmp_path / "catalogo.json"
    item = {"product_id": 7, "nome": "x", "preco": "1.00"}
    arquivo.write_text(json.dumps([item, item]))
    with pytest.raises(ValueError):
        carregar_catalogo(arquivo)


def test_forma_de_pagamento_desconhecida_da_api_e_recusada(tmp_path):
    arquivo = tmp_path / "pagamentos.json"
    arquivo.write_text(json.dumps(["PIX", "FIADO"]))
    with pytest.raises(ValueError):
        carregar_pagamentos(arquivo)


@pytest.mark.parametrize("product_id", [26717.0, True, "26717", None])
def test_product_id_que_nao_e_inteiro_e_recusado(tmp_path, product_id):
    arquivo = tmp_path / "catalogo.json"
    arquivo.write_text(json.dumps([{"product_id": product_id, "nome": "x", "preco": "1.00"}]))
    with pytest.raises(ValueError):
        carregar_catalogo(arquivo)

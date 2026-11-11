"""Teste do construtor da ApiCadenciada (seam combinado: só o construtor).

O resto do comportamento (1 req/s, 409, 429) é testado por quem a usa:
`registrar()` e a devolução.
"""

import pytest

from revgas_chatbot.api_cadenciada import ApiCadenciada
from revgas_chatbot.api_falsa import ApiRevGasFalsa


def test_nao_aceita_embrulhar_outra_api_cadenciada():
    # Embrulhar duas vezes multiplicaria as novas tentativas (429: 3 -> 9).
    api = ApiRevGasFalsa()
    cadenciada = ApiCadenciada(api, api.relogio)

    with pytest.raises(TypeError):
        ApiCadenciada(cadenciada, api.relogio)

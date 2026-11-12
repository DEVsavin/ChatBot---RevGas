"""ApiCadenciada: a disciplina de chamadas à API da RevGás, num lugar só.

Envolve uma `ApiRevGas` (real ou falsa) e oferece as mesmas operações,
acrescentando as regras que valem para TODA chamada:

- no máximo 1 requisição por segundo (limite global da API);
- 409 de lock ("cannot modify object due to lock"): 1 nova tentativa;
- 429 (limite excedido): nova tentativa depois de >= 1 s, até 3 no total.

Nos dois casos de nova tentativa o servidor recusou, então nada foi criado
e repetir é seguro. Qualquer outro status volta para quem chamou, que é
quem decide o que ele significa. `TempoEsgotado` também volta: o resultado
é incerto e nunca é repetido aqui.

Para o limite de 1 req/s valer entre fluxos (venda e devolução), todos
devem usar a MESMA instância de ApiCadenciada.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from revgas_chatbot.api_revgas.api import ApiRevGas, Relogio, Resposta

# Limite global da API: 1 requisição por segundo.
INTERVALO_MINIMO = 1.0

# Novas tentativas além da primeira.
REPETICOES_409 = 1  # só para 409 de lock
REPETICOES_429 = 2  # 3 tentativas no total


def _travado(resposta: Resposta) -> bool:
    """409 de lock no banco da API (o único 409 que vale repetir)."""
    if resposta.status != 409 or not isinstance(resposta.dados, dict):
        return False
    return "lock" in str(resposta.dados.get("message", "")).lower()


class ApiCadenciada:
    """Uma `ApiRevGas` com ritmo controlado e novas tentativas seguras.

    Oferece as mesmas 4 operações da porta. Não embrulha outra
    `ApiCadenciada` (as novas tentativas se multiplicariam).
    """

    def __init__(self, api: ApiRevGas, relogio: Relogio) -> None:
        if isinstance(api, ApiCadenciada):
            raise TypeError("ApiCadenciada não pode embrulhar outra ApiCadenciada; use a mesma instância")
        self._api = api
        self._relogio = relogio
        self._ultima_requisicao: datetime | None = None

    def buscar_clientes(self, telefone: str) -> Resposta:
        return self._chamar(lambda: self._api.buscar_clientes(telefone))

    def cadastrar_cliente(self, dados: dict[str, Any]) -> Resposta:
        return self._chamar(lambda: self._api.cadastrar_cliente(dados))

    def criar_venda(self, corpo: dict[str, Any]) -> Resposta:
        return self._chamar(lambda: self._api.criar_venda(corpo))

    def buscar_vendas(
        self, *, customer_id: int, channel: str | None, de: date, ate: date, pagina: int
    ) -> Resposta:
        return self._chamar(
            lambda: self._api.buscar_vendas(
                customer_id=customer_id, channel=channel, de=de, ate=ate, pagina=pagina
            )
        )

    def _chamar(self, requisicao: Callable[[], Resposta]) -> Resposta:
        repeticoes_409 = repeticoes_429 = 0
        while True:
            self._aguardar_vez()
            try:
                resposta = requisicao()
            finally:
                self._ultima_requisicao = self._relogio.agora()
            if _travado(resposta) and repeticoes_409 < REPETICOES_409:
                repeticoes_409 += 1
            elif resposta.status == 429 and repeticoes_429 < REPETICOES_429:
                repeticoes_429 += 1
            else:
                return resposta
            # A próxima tentativa espera >= 1 s por causa de _aguardar_vez.

    def _aguardar_vez(self) -> None:
        if self._ultima_requisicao is None:
            return
        passou = (self._relogio.agora() - self._ultima_requisicao).total_seconds()
        if passou < INTERVALO_MINIMO:
            self._relogio.dormir(INTERVALO_MINIMO - passou)

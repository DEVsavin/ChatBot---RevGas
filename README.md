<div align="center">

# 🔥 Chatbot RevGás

**Inteligência artificial no atendimento do mercado de gás de cozinha (GLP).**

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Projeto de Extensão](https://img.shields.io/badge/Projeto%20de%20Extens%C3%A3o%20VI-2026.2-blueviolet)

</div>

---

## 📌 Sobre

O **Chatbot RevGás** automatiza o atendimento aos clientes das revendas de GLP. O cliente faz o pedido numa conversa pelo WhatsApp, e o chatbot coleta os dados e registra a venda direto no **ERP RevGás**. Quando necessário, o atendimento passa para uma pessoa.

> Projeto desenvolvido na disciplina **Projeto de Extensão VI (Transformação Digital, 2026.2)**.

## 🎯 Objetivos

- Reduzir o atendimento manual nas revendas
- Agilizar o registro de pedidos
- Melhorar a experiência do cliente
- Manter o atendimento humano disponível quando necessário

## 💬 Como funciona

```mermaid
flowchart LR
    A([Cliente]) --> B[WhatsApp]
    B --> C[Chatbot]
    C --> D[Central de pedidos]
    D --> E[(ERP RevGás)]
    E --> F[App do entregador]
    F --> G([Entregador])
```

O chatbot identifica o cliente e coleta **nome, produto, telefone, endereço e forma de pagamento**. Depois mostra um resumo para o cliente confirmar e só então registra o pedido.

## 🚀 Como rodar

Requisitos: Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/DEVsavin/ChatBot---RevGas.git
cd ChatBot---RevGas
uv sync
uv run pytest
```

Os testes usam uma API falsa e não acessam a internet.

### Arquivo `.env`

O chat de teste funciona sem configuração nenhuma. As chaves só são necessárias para a IA, o WhatsApp e a venda real. Crie o arquivo a partir do exemplo e preencha o que for usar:

```bash
cp .env.example .env
```

| Variável | Para quê |
|---|---|
| `GEMINI_API_KEY` | IA que entende texto livre (opcional) |
| `REVGAS_API_KEY`, `REVGAS_API_URL`, `REVGAS_CLIENTE_DEMO` | Venda real no ERP (só com `--venda-real`) |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_DOMINIO` | Canal WhatsApp |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | Só referência da conta na Meta; o programa não usa |
| `WHATSAPP_NUMEROS_EQUIPE` | Até 5 celulares da equipe, separados por vírgula: só eles (e os números fictícios) usam a IA |

O `.env` está no `.gitignore` e **nunca** deve ser enviado ao repositório.

## 🗨️ Chat de teste (terminal)

```bash
uv run python -m revgas_chatbot.canais.chat_terminal --telefone 5500900000001
```

- `--telefone` simula o número que o WhatsApp entregaria. Sem ele, o chat pergunta qual usar.
- Por padrão o chat usa uma **API de teste** (nenhuma venda vai para o ERP de verdade) com clientes fictícios de DDD `00`. Para vendas reais, veja a seção **Venda real no ERP**, mais abaixo.
  - `5500900000001`: cliente cadastrado (Maria Exemplo, com endereço);
  - `5500900000002`: cliente sem cadastro.
- As solicitações para a equipe ficam em `dados/fila.html` (abra no navegador e recarregue com F5). A pasta `dados/` não vai para o git.
- `Ctrl+D` sai do chat.

### Com IA (texto livre)

Com a chave do Gemini (cota grátis do [Google AI Studio](https://aistudio.google.com/)) no arquivo `.env` (`GEMINI_API_KEY=...`), o chat entende frases como *"manda 2 botijão pra Rua Nova 10, Centro, Teresina-PI, pago no pix"*:

```bash
uv run --env-file .env python -m revgas_chatbot.canais.chat_terminal --telefone 5500900000001
```

- A IA só **entende** a mensagem; o chatbot confere tudo (preço do catálogo, endereço que o cliente disse) e o resumo continua obrigatório.
- Sem a chave, sem internet ou se a IA não responder em 8 segundos, o chat segue o roteiro normal.
- Na cota grátis o Google pode usar as mensagens: por isso a IA só atende os números fictícios (DDD `00`) e os da equipe (`WHATSAPP_NUMEROS_EQUIPE`). Para clientes reais será preciso um plano pago.

### Chat no navegador

```bash
uv run python -m revgas_chatbot.canais.canal_web                      # sem IA
uv run --env-file .env python -m revgas_chatbot.canais.canal_web      # com IA
```

Abra **http://127.0.0.1:8000**, digite um telefone de demonstração e converse. A fila da equipe fica em **http://127.0.0.1:8000/fila** (recarregue para atualizar).

- Funciona **só nesta máquina** (`127.0.0.1`); outros computadores não acessam.
- Números fictícios (DDD `00`) são tratados como o número do cliente: a cliente cadastrada é reconhecida e a IA pode ser usada.
- Qualquer outro número é tratado como digitado: o chatbot pede nome e endereço, não mostra nenhum cadastro e só usa a IA se for um número da equipe.
- `--porta 8080` muda a porta, se a 8000 estiver ocupada.

### Roteiro de demonstração

1. **Pedido completo (cliente cadastrado):** `--telefone 5500900000001` → `sim` → `1` (botijão) → `2` (quantidade) → `sim` (endereço do cadastro) → `1` (PIX) → `1` (confirmar).
2. **Cliente novo com correção:** `--telefone 5500900000002` → `sim` → nome → `1` → `1` → rua, número, bairro, cidade, estado, `pular`, `pular` → `2` (dinheiro) → troco `200` → no resumo, `2` (corrigir) → `2` (endereço) → endereço novo → `1` (confirmar).
3. **Transferência para a equipe:** em qualquer momento, envie `quero falar com um atendente` ou `estou sentindo cheiro de gás` e veja a solicitação em `dados/fila.html` (o vazamento aparece como URGENTE, no topo).

> As orientações de segurança para vazamento (`config/seguranca.txt`) são provisórias e ainda serão validadas com a revenda.

## 📱 WhatsApp

O chatbot atende pelo WhatsApp usando a Cloud API da Meta, com o número de teste dela. O programa roda nesta máquina (`127.0.0.1:8001`) e o [ngrok](https://ngrok.com/) leva até ele as mensagens que a Meta envia. Só a rota `/webhook` passa pelo túnel: a página do chat e a fila da equipe continuam acessíveis apenas localmente.

Antes de começar:

- um app na Meta com o produto WhatsApp, e os valores de `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` e `WHATSAPP_APP_SECRET` no `.env`;
- os celulares da equipe cadastrados como destinatários de teste no painel da Meta (o número de teste só conversa com eles) e listados em `WHATSAPP_NUMEROS_EQUIPE`;
- o ngrok instalado, com um domínio fixo gratuito salvo em `WHATSAPP_DOMINIO` (só o domínio, sem `https://`);
- `WHATSAPP_VERIFY_TOKEN` com pelo menos 32 caracteres. Para gerar um:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Ligue o programa e o túnel, cada um num terminal:

```bash
uv run --env-file .env python -m revgas_chatbot.canais.canal_whatsapp
```

```bash
ngrok http 8001 --url=https://<seu-dominio>
```

No painel da Meta, em **WhatsApp → Configuração → Webhook**, use `https://<seu-dominio>/webhook` como URL de retorno, o valor de `WHATSAPP_VERIFY_TOKEN` como token de verificação, e assine o campo **messages**. Depois, mande "oi" de um celular da equipe para o número de teste.

- O número que chega pelo WhatsApp é tratado como o número do cliente.
- Antes de ler qualquer mensagem, o programa confere a assinatura da Meta; mensagens sem assinatura válida são recusadas.
- O token temporário da Meta vence em 24 horas. Se aparecer `token do WhatsApp recusado (401)`, gere outro no painel e troque no `.env`.
- Sem `--venda-real`, os pedidos vão para a API de teste.

## 🧾 Venda real no ERP

Por padrão, todos os canais usam a API de teste. Com a opção `--venda-real`, cada pedido confirmado pelo cliente cria uma venda de verdade no ERP RevGás (estabelecimento 2580):

```bash
uv run --env-file .env python -m revgas_chatbot.canais.chat_terminal --venda-real
uv run --env-file .env python -m revgas_chatbot.canais.canal_web --venda-real
uv run --env-file .env python -m revgas_chatbot.canais.canal_whatsapp --venda-real
```

- Precisa de `REVGAS_API_KEY`, `REVGAS_API_URL` e `REVGAS_CLIENTE_DEMO` no `.env`; sem eles, o programa não inicia.
- O programa mostra o aviso `VENDA REAL LIGADA` ao iniciar (e a página web também avisa).
- Toda venda usa o cliente de demonstração de `REVGAS_CLIENTE_DEMO`, porque a chave atual ainda não tem acesso à busca de clientes da API.
- As vendas são criadas **sem avisar os entregadores**.
- A API limita a 1 requisição por segundo; o chatbot respeita esse limite.

Para cancelar as vendas abertas da demonstração depois de um teste (o script pede confirmação a cada uma):

```bash
uv run --env-file .env python -m revgas_chatbot.vendas.venda_de_teste --cancelar-demo
```

## 👥 Equipe

- Sávio Macêdo
- Bruno Moraes
- Matheus Farias

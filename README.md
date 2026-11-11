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

## 👥 Equipe

- Sávio Macêdo
- Bruno Moraes
- Matheus Farias

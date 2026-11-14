#!/usr/bin/env bash
# Liga o WhatsApp pelo chip (WAHA) com um comando:
#   ./scripts/whatsapp-waha.sh              (venda na API de teste)
#   ./scripts/whatsapp-waha.sh --venda-real (venda de verdade; só o usuário liga)
#
# 1. sobe o WAHA no Docker (só em 127.0.0.1:3000);
# 2. liga o chatbot (só em 127.0.0.1:8002);
# 3. confere se o WAHA alcança o chatbot (senão as mensagens somem sem aviso);
# 4. fica rodando até Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.waha.yml --env-file .env)

if ! docker info >/dev/null 2>&1; then
  echo "O Docker não está ligado. Abra o Docker Desktop, espere ele ficar pronto e rode de novo."
  exit 1
fi
if [ ! -f .env ]; then
  echo "Falta o arquivo .env (veja o .env.example)."
  exit 1
fi

echo "1/3 Subindo o WAHA no Docker..."
mkdir -p dados/waha-sessoes
"${COMPOSE[@]}" up -d

echo "2/3 Ligando o chatbot..."
uv run --env-file .env python -m revgas_chatbot.canais.canal_waha "$@" &
CHATBOT=$!
trap 'kill "$CHATBOT" 2>/dev/null || true' EXIT INT TERM
for _ in $(seq 1 40); do
  if curl -s -o /dev/null http://127.0.0.1:8002/ 2>/dev/null; then break; fi
  if ! kill -0 "$CHATBOT" 2>/dev/null; then echo "O chatbot não iniciou (veja a mensagem acima)."; exit 1; fi
  sleep 0.5
done

echo "3/3 Conferindo se o WAHA alcança o chatbot..."
# Qualquer resposta HTTP (mesmo "403: sem assinatura") prova que o caminho funciona.
if "${COMPOSE[@]}" exec -T waha node -e "fetch('http://host.docker.internal:8002/webhook',{method:'POST'}).then(r=>{console.log('HTTP',r.status)}).catch(()=>process.exit(1))" >/dev/null 2>&1; then
  echo "OK: o WAHA alcança o chatbot."
else
  echo "ATENÇÃO: o WAHA NÃO alcança o chatbot. As mensagens do WhatsApp não vão chegar."
  echo "Veja 'Se o WAHA não alcançar o chatbot' no README (plano B)."
fi

echo
echo "Agora: abra http://127.0.0.1:3000/dashboard (usuário e senha: WAHA_DASHBOARD_USERNAME e"
echo "WAHA_DASHBOARD_PASSWORD no .env), leia o QR code com o celular do chip e mande 'oi'"
echo "de um celular da equipe. Para parar: Ctrl+C (o WAHA continua no Docker; veja o README)."
wait "$CHATBOT"

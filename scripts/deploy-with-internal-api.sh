#!/usr/bin/env bash
# Деплой бота + включение INTERNAL_API_TOKEN (тот же, что MAX_BOT_INTERNAL_TOKEN на admspgo).
#   export DEPLOY_PASSWORD='...'
#   bash scripts/deploy-with-internal-api.sh
#
# Токен с VPS приложения:
#   export INTERNAL_API_TOKEN="$(ssh root@201.51.8.202 cat /root/.max_bot_internal_token)"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${DEPLOY_PASSWORD:-}" ]]; then
  echo "Задайте DEPLOY_PASSWORD (root на 201.51.6.233)." >&2
  exit 1
fi

if [[ -z "${INTERNAL_API_TOKEN:-}" ]]; then
  echo "INTERNAL_API_TOKEN не задан — читаю с VPS приложения..." >&2
  INTERNAL_API_TOKEN="$(ssh -o BatchMode=yes -o ConnectTimeout=10 root@201.51.8.202 'cat /root/.max_bot_internal_token' 2>/dev/null)" || true
  if [[ -z "${INTERNAL_API_TOKEN:-}" ]]; then
    echo "Сначала: bash scripts/vps/configure-max-bot-env.sh (в sergiev_posad) или задайте INTERNAL_API_TOKEN." >&2
    exit 1
  fi
  export INTERNAL_API_TOKEN
fi

echo "==> Деплой кода на bot VPS..."
DEPLOY_PASSWORD="$DEPLOY_PASSWORD" python3 deploy/remote_deploy.py

echo "==> INTERNAL_API_TOKEN в .env на bot VPS..."
export DEPLOY_HOST="${DEPLOY_HOST:-201.51.6.233}"
export DEPLOY_USER="${DEPLOY_USER:-root}"
python3 deploy/set_internal_api_token.py

echo "=== Bot internal API ready ==="

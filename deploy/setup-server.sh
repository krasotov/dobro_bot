#!/bin/bash
set -euo pipefail

# Запускать на VPS от root после копирования проекта в /opt/max_bot

DOMAIN_PUNY="bot.xn--80agc6alen.xn--p1ai"
PROJECT_DIR="/opt/max_bot"

echo "=== Установка Docker ==="
if ! command -v docker &>/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
fi

echo "=== Подготовка certbot ==="
apt-get install -y certbot || true
mkdir -p /var/www/certbot

if [ ! -f "/etc/letsencrypt/live/${DOMAIN_PUNY}/fullchain.pem" ]; then
  echo "Получение SSL-сертификата для ${DOMAIN_PUNY}"
  echo "Убедитесь, что DNS A-запись bot.адмспго.рф указывает на этот сервер!"
  certbot certonly --standalone -d "${DOMAIN_PUNY}" --non-interactive --agree-tos -m admin@admspgo.ru || true
fi

cd "$PROJECT_DIR"
docker compose up -d --build

echo "=== Готово ==="
echo "Проверка: curl https://${DOMAIN_PUNY}/health"
echo "Админка: https://${DOMAIN_PUNY}/admin/"
echo "Webhook setup: curl -X POST https://${DOMAIN_PUNY}/setup-webhook -H 'Content-Type: application/json' -d '{\"password\":\"ВАШ_ПАРОЛЬ\"}'"

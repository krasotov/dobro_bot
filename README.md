# Доброчат — бот MAX

Сервис бота в мессенджере MAX: регистрация жителей, рассылки, скриншоты, встроенная **веб-админка** (`/admin`), опционально — транспорт, служебные записки, internal API для интеграций.

Репозиторий **автономный**: не требует admspgo.ru и мобильного приложения. Интеграция с внешним API включается только через переменные `ADMSPGO_*` в `.env` (можно не задавать).

## Возможности

- Регистрация (согласие на ПДн, ФИО, адрес через DaData, телефон)
- Админ-панель: пользователи, CSV, рассылки по районам, скриншоты, операторы, автоновости из канала
- Webhook MAX + резервный long polling `/updates`
- Docker Compose (PostgreSQL + приложение + nginx)

Дополнительно (в коде, без внешнего API работает частично): заказ транспорта, служебные записки ИТ, internal API (`INTERNAL_API_TOKEN`).

Функции **справочника, тем подписок, опросов, очереди обращений на портале** — только при настройке `ADMSPGO_API_URL` и `ADMSPGO_INTERNAL_TOKEN`.

## Быстрый старт (локально)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните MAX_BOT_TOKEN, пароли, при необходимости DaData
uvicorn app.main:app --reload
```

Админка: http://127.0.0.1:8000/admin/

## Деплой на VPS

1. A-запись DNS на IP сервера → домен для бота (например `bot.example.ru`).
2. Скопируйте проект на сервер (git clone или rsync).
3. Создайте `.env` из `.env.example` (токен MAX, `WEBHOOK_URL`, `ADMIN_*`, `POSTGRES_PASSWORD`, `SECRET_KEY`).
4. Подставьте домен в `deploy/nginx.conf` (server_name и пути сертификатов) или используйте свой reverse proxy.
5. Запуск:

```bash
docker compose up -d --build
chmod +x deploy/setup-server.sh   # при первом SSL через certbot
./deploy/setup-server.sh          # при необходимости
```

6. Подключите webhook:

```bash
curl -X POST "https://ВАШ_ДОМЕН/setup-webhook" \
  -H "Content-Type: application/json" \
  -d '{"password":"ВАШ_ADMIN_PASSWORD"}'
```

7. Операторы: **https://ВАШ_ДОМЕН/admin/**

## Переменные окружения

| Переменная | Назначение |
|------------|------------|
| `MAX_BOT_TOKEN` | Токен бота (business.max.ru) |
| `WEBHOOK_URL` | `https://домен/webhook` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Суперадмин веб-панели |
| `SECRET_KEY` | Сессии админки |
| `DADATA_*` | Нормализация адресов (рекомендуется) |
| `INTERNAL_API_TOKEN` | Доступ к `/internal/v1/*` (опционально) |
| `ADMSPGO_*` | Внешний API — **не нужен** для standalone |

Подробнее: `.env.example`, `docs/STANDALONE.md`.

## Безопасность

- Не коммитьте `.env` и токены.
- После передачи проекта смените пароли и при необходимости перевыпустите токен бота в MAX.

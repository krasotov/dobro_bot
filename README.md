# Доброчат — бот MAX для Администрации Сергиево-Посадского ГО

Бот собирает регистрационные данные жителей (ФИО, адрес, телефон), проверяет дубли, позволяет делать рассылки и принимать скриншоты.

## Что уже есть

- Регистрация по шагам с согласием на обработку ПДн
- Проверка дублей по `user_id` и телефону
- Админ-панель: пользователи, CSV-выгрузка, рассылки, скриншоты
- Webhook для production

## Быстрый старт (локально)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните .env
uvicorn app.main:app --reload
```

## Деплой на VPS 201.51.6.233

### 1. Настройте DNS у регистратора домена адмспго.рф

Добавьте A-запись:

| Имя | Тип | Значение |
|-----|-----|----------|
| `bot` | A | `201.51.6.233` |

Проверка: `dig bot.xn--80agc6alen.xn--p1ai` должен вернуть IP сервера.

### 2. Скопируйте проект на сервер

```bash
scp -r . root@201.51.6.233:/opt/max_bot
```

### 3. Создайте `.env` на сервере

```bash
ssh root@201.51.6.233
cd /opt/max_bot
cp .env.example .env
nano .env
```

Обязательные переменные:

```env
MAX_BOT_TOKEN=ваш_токен
MAX_WEBHOOK_SECRET=случайная_строка_5_256_символов
WEBHOOK_URL=https://bot.xn--80agc6alen.xn--p1ai/webhook
ADMIN_USERNAME=admin
ADMIN_PASSWORD=надёжный_пароль
SECRET_KEY=случайная_строка
POSTGRES_PASSWORD=надёжный_пароль_для_бд
MAX_API_VERIFY_SSL=false
```

> `MAX_API_VERIFY_SSL=false` — временно, пока на сервере не установлен сертификат Минцифры для API MAX.

### 4. Запустите

```bash
chmod +x deploy/setup-server.sh
./deploy/setup-server.sh
```

### 5. Подключите webhook к MAX

```bash
curl -X POST https://bot.xn--80agc6alen.xn--p1ai/setup-webhook \
  -H "Content-Type: application/json" \
  -d '{"password":"ВАШ_ADMIN_PASSWORD"}'
```

### 6. Админ-панель

https://bot.xn--80agc6alen.xn--p1ai/admin/

## Безопасность

- **Не публикуйте токен бота и пароли** в чатах и репозиториях
- После настройки **обновите токен** на business.max.ru, если он был передан в открытом виде
- Смените пароль root на VPS

## Структура регистрации

1. Пользователь нажимает «Начать» в MAX
2. Бот показывает согласие на обработку ПДн
3. ФИО → адрес → кнопка «Поделиться контактом»
4. Проверка дублей → подтверждение регистрации

## Рассылки

Рассылка идёт только зарегистрированным пользователям, которые нажали «Начать». Лимит API — 30 сообщений/сек.

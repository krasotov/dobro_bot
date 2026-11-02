from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    max_bot_token: str = ""
    max_api_base: str = "https://platform-api2.max.ru"
    max_webhook_secret: str = ""

    database_url: str = "postgresql+asyncpg://bot:botpassword@localhost:5432/maxbot"

    admin_username: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "change-me"

    webhook_url: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    max_api_verify_ssl: bool = True

    news_source_chat_id: int | None = None
    outage_source_chat_id: int | None = None

    dadata_api_key: str = ""
    dadata_secret_key: str = ""

    # Сервисный доступ для admspgo.ru (read-only overview). Пусто — эндпоинты /internal/v1 отключены.
    internal_api_token: str = ""

    # Опционально: внешний API (обращения, справочник, опросы). Пусто — только локальные функции бота.
    admspgo_api_url: str = ""
    admspgo_internal_token: str = ""

    # Если MAX не шлёт webhook по dialog (известный баг у части аккаунтов), подхватываем GET /messages.
    dialog_inbox_poll_enabled: bool = True
    dialog_inbox_poll_interval_sec: float = 2.0
    dialog_inbox_poll_chat_ids: str = ""

    # Long polling /updates параллельно webhook (callback-кнопки у части аккаунтов).
    max_updates_poll_enabled: bool = True
    max_updates_poll_timeout_sec: int = 20


settings = Settings()

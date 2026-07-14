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

    dadata_api_key: str = ""
    dadata_secret_key: str = ""


settings = Settings()

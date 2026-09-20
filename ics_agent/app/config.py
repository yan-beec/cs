from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 3001
    log_level: str = "INFO"

    chatwoot_base_url: str = "http://127.0.0.1:3000"
    chatwoot_api_token: str = ""
    chatwoot_account_id: str = ""
    chatwoot_webhook_secret: str = ""

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "cs"
    mysql_user: str = "cs"
    mysql_password: str = "cs_password"

    redis_url: str = "redis://127.0.0.1:6380/0"
    redis_key_prefix: str = "ics_agent:"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

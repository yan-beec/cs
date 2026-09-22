# 模块职责：集中定义应用运行环境、基础设施连接、认证和外部服务配置。

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppSettings(BaseSettings):
    """Application and infrastructure settings.

    FAQ and RAG behavior defaults live in their own module configuration
    objects. This class only owns application, infrastructure, authentication,
    and external-service settings.
    """

    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 3001
    log_level: str = "INFO"

    # Browser/admin service binding and upload/log initialization.
    admin_web_host: str = "127.0.0.1"
    admin_web_port: int = Field(default=3001, ge=1, le=65535)
    admin_log_buffer_size: int = Field(default=2000, ge=100, le=10000)
    admin_file_max_size_mb: int = Field(default=100, ge=1, le=1024)

    # Chatwoot and other external service connection settings.
    chatwoot_base_url: str = "http://127.0.0.1:3000"
    chatwoot_api_token: str = ""
    chatwoot_account_id: str = ""
    chatwoot_webhook_secret: str = ""

    # Singleton administrator initialization and authentication.
    admin_username: str = "Denpans"
    admin_initial_password: str = ""
    admin_jwt_secret: str = ""
    admin_access_token_expire_seconds: int = Field(default=3600, ge=300)

    # PostgreSQL connection and pool settings.
    postgres_dsn: str = ""
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_database: str = "cs"
    postgres_user: str = "cs"
    postgres_password: str = "cs_password"
    postgres_sslmode: str = "disable"
    postgres_pool_name: str = "ics_agent_postgres"
    postgres_pool_size: int = Field(default=10, ge=1, le=20)
    postgres_connection_timeout: int = Field(default=5, ge=1)
    postgres_pool_acquire_timeout: float = Field(default=0.05, gt=0)

    # Redis connection and failure-degradation settings.
    redis_url: str = "redis://127.0.0.1:6380/0"
    redis_key_prefix: str = "ics_agent:"
    redis_connect_timeout: float = Field(default=0.5, gt=0)
    redis_socket_timeout: float = Field(default=0.5, gt=0)
    redis_failure_cooldown_seconds: float = Field(default=30.0, gt=0)

    # Cloud LLM provider connection and runtime limits.
    llm_model: str = "deepseek-flash"
    llm_model_provider: str = "openai"
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1024, ge=1)
    llm_timeout: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_max_concurrency: int = Field(default=5, ge=1)
    llm_concurrency_wait_timeout: float = Field(default=5.0, gt=0)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_structured_method: str = "function_calling"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def postgres_connection_string(self) -> str:
        """Return a PostgreSQL URI reusable by future checkpoint adapters."""
        if self.postgres_dsn:
            return self.postgres_dsn
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.postgres_database, safe="")
        return (
            f"postgresql://{user}:{password}@{self.postgres_host}:"
            f"{self.postgres_port}/{database}"
        )


Settings = AppSettings


@lru_cache
def get_settings() -> AppSettings:
    """Return the process-level application settings singleton."""
    return AppSettings()

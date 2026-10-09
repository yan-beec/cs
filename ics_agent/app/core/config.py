# 模块职责：集中定义应用运行环境、基础设施连接、认证和外部服务配置。

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AppSettings(BaseSettings):
    """应用和基础设施设置。

    FAQ and RAG behavior defaults live in their own module configuration
    objects. This class only owns application, infrastructure, authentication,
    and external-service settings.
    """

    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 3001
    log_level: str = "INFO"

    # 浏览器/管理后台服务绑定，以及上传和日志初始化。
    admin_web_host: str = "127.0.0.1"
    admin_web_port: int = Field(default=3001, ge=1, le=65535)
    admin_log_buffer_size: int = Field(default=2000, ge=100, le=10000)
    admin_file_max_size_mb: int = Field(default=100, ge=1, le=1024)
    uvicorn_log_directory: str = "log"
    uvicorn_log_max_bytes: int = Field(default=30 * 1024 * 1024, ge=1024)
    uvicorn_log_backup_count: int = Field(default=5, ge=1, le=100)
    uvicorn_stdout_log_filename: str = "uvicorn-admin.stdout.log"
    uvicorn_stderr_log_filename: str = "uvicorn-admin.stderr.log"

    # Chatwoot 和其他外部服务连接配置。
    chatwoot_base_url: str = "http://127.0.0.1:3000"
    # 浏览器访问 Website Widget 时使用的公网地址；不应填写容器内地址。
    chatwoot_public_base_url: str = ""
    chatwoot_api_token: str = ""
    # 坐席查询、会话状态/分配和联系人 blocked 更新不能使用 AgentBot Token。
    chatwoot_contact_api_token: str = ""
    chatwoot_account_id: str = ""
    chatwoot_hmac_token: str = ""
    chatwoot_profile_allowed_origins: str = (
        "null,http://localhost:8090,http://127.0.0.1:8090"
    )
    chatwoot_profile_allow_query_identifiers: bool = False
    # Account Webhook 和 AgentBot Webhook 的签名密钥可能不同。
    chatwoot_webhook_secret: str = ""
    chatwoot_agent_bot_secret: str = ""
    # 配置 AgentBot 自己的 Token 后，回复会以 AgentBot 身份写回 Chatwoot。
    chatwoot_agent_bot_access_token: str = ""
    chatwoot_agent_bot_id: str = ""
    chatwoot_webhook_tolerance_seconds: int = Field(
        default=300,
        ge=1,
        le=3600,
    )
    chatwoot_auto_reply_enabled: bool = False
    # 仅用户新消息刷新此窗口；resolved 会立即结束当前咨询。
    chatwoot_session_idle_timeout_seconds: int = Field(default=21600, gt=0)
    chatwoot_handoff_enabled: bool = True
    chatwoot_handoff_label: str = "handoff_required"
    chatwoot_handoff_notice_enabled: bool = True
    chatwoot_handoff_team_id: str = ""
    chatwoot_handoff_assignee_id: str = ""
    # 每位在线坐席最多同时承接的 open 人工会话数。
    chatwoot_handoff_max_active_conversations: int = Field(
        default=5,
        ge=1,
        le=100,
    )
    chatwoot_block_duration_seconds: int = Field(
        default=24 * 60 * 60,
        ge=60,
    )
    chatwoot_block_reaper_interval_seconds: int = Field(
        default=60,
        ge=5,
    )
    chatwoot_block_reaper_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
    )
    chatwoot_api_timeout: float = Field(default=15.0, gt=0)

    # 单例管理员初始化和身份认证。
    admin_username: str = "Denpans"
    admin_initial_password: str = ""
    admin_jwt_secret: str = ""
    admin_access_token_expire_seconds: int = Field(default=3600, ge=300)

    # PostgreSQL 连接和连接池配置。
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

    # 云端 LLM 提供商连接和运行时限制。
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
        """返回可供后续检查点适配器复用的 PostgreSQL URI。"""
        if self.postgres_dsn:
            return self.postgres_dsn
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password, safe="")
        database = quote(self.postgres_database, safe="")
        return (
            f"postgresql://{user}:{password}@{self.postgres_host}:"
            f"{self.postgres_port}/{database}?sslmode="
            f"{quote(self.postgres_sslmode, safe='')}"
        )


Settings = AppSettings


@lru_cache
def get_settings() -> AppSettings:
    """返回进程级应用设置单例。"""
    return AppSettings()

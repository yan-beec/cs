"""Optional presence writer configuration, separate from all existing pools."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PresenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )

    presence_enabled: bool = False
    source_key: str = "chatwoot-main"
    postgres_host: str = "archive-postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "ics_quality"
    postgres_sslmode: str = "prefer"
    presence_user: str = "archive_presence"
    presence_password: SecretStr = SecretStr("")
    presence_max_agents: int = Field(default=100, ge=1, le=200)

    @property
    def connection_kwargs(self) -> dict:
        password = self.presence_password.get_secret_value()
        if not password or password.startswith(("REPLACE_", "change-me")):
            raise ValueError("Configure the restricted archive presence credential")
        if not self.presence_user or self.presence_user != self.presence_user.strip():
            raise ValueError("Configure the archive presence database user")
        if not self.source_key or self.source_key != self.source_key.strip() or len(self.source_key) > 128:
            raise ValueError("Configure the stable archive source key")
        return {
            "host": self.postgres_host, "port": self.postgres_port,
            "dbname": self.postgres_db, "user": self.presence_user, "password": password,
            "sslmode": self.postgres_sslmode, "connect_timeout": 2,
            "application_name": "ics-quality-presence",
            "options": "-c statement_timeout=3000 -c lock_timeout=500",
        }

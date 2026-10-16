"""Optional reader configuration, separate from all customer-service pools."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class QuerySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )

    query_enabled: bool = False
    source_key: str = "chatwoot-main"
    postgres_host: str = "archive-postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "ics_quality"
    postgres_sslmode: str = "prefer"
    reader_user: str = "archive_reader"
    reader_password: SecretStr = SecretStr("")
    query_pool_size: int = Field(default=2, ge=1, le=5)
    export_max_entities: int = Field(default=20000, ge=1, le=100000)
    export_max_messages: int = Field(default=100000, ge=1, le=1000000)
    export_max_bytes: int = Field(default=134217728, ge=1024, le=1073741824)
    export_timeout_seconds: int = Field(default=120, ge=10, le=600)

    @property
    def connection_kwargs(self) -> dict:
        password = self.reader_password.get_secret_value()
        if not password or password.startswith(("REPLACE_", "change-me")):
            raise ValueError("Configure the archive reader credential")
        return {
            "host": self.postgres_host, "port": self.postgres_port,
            "dbname": self.postgres_db, "user": self.reader_user, "password": password,
            "sslmode": self.postgres_sslmode, "connect_timeout": 2,
            "application_name": "ics-quality-query",
            "options": "-c statement_timeout=3000 -c lock_timeout=500 -c default_transaction_read_only=on",
        }

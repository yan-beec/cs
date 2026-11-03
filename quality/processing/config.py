"""Worker-only resources, independent from the customer service application."""

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHIVE_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    postgres_host: str = "archive-postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "ics_quality"
    postgres_sslmode: str = "prefer"
    processor_pool_size: int = Field(default=2, ge=1, le=10)
    ingest_user: str = "archive_ingest"
    ingest_password: SecretStr
    processor_lease_seconds: int = Field(default=120, ge=30, le=3600)
    processor_max_attempts: int = Field(default=8, ge=1, le=100)
    processor_poll_seconds: float = Field(default=2, ge=0.1, le=30)
    processor_recent_rounds: int = Field(default=50, ge=1, le=200)
    processor_index_batch_size: int = Field(default=500, ge=1, le=2000)
    processor_index_batch_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    processor_max_conversation_events: int = Field(default=20000, ge=100, le=500000)
    processor_max_conversation_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024, le=1024 * 1024 * 1024)
    idle_timeout_seconds: int = Field(default=21600, gt=0, validation_alias=AliasChoices(
        "CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS", "idle_timeout_seconds"))

    @field_validator("ingest_password")
    @classmethod
    def credential_is_configured(cls, value):
        if not value.get_secret_value() or value.get_secret_value().startswith(("REPLACE_", "change-me")):
            raise ValueError("Configure the actual archive ingestion credential")
        return value

    @property
    def connection_kwargs(self):
        return {"host": self.postgres_host, "port": self.postgres_port, "dbname": self.postgres_db,
                "user": self.ingest_user, "password": self.ingest_password.get_secret_value(),
                "sslmode": self.postgres_sslmode, "connect_timeout": 3,
                "application_name": "ics-quality-processor",
                "options": "-c statement_timeout=30000 -c lock_timeout=2000"}

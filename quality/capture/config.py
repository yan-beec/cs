"""Independent archive connection and receiver configuration from the shared .env."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CaptureSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_", env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True,
    )

    source_key: str = Field(min_length=1, max_length=128)
    allowed_account_ids: str
    webhook_secret: SecretStr
    webhook_tolerance_seconds: int = Field(default=300, ge=1, le=3600)
    receiver_max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    postgres_host: str = "archive-postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "ics_quality"
    postgres_sslmode: str = "prefer"
    postgres_pool_size: int = Field(default=5, ge=1, le=20)
    ingest_user: str = "archive_ingest"
    ingest_password: SecretStr

    @field_validator("source_key")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("Set a stable, nonempty archive source key without surrounding whitespace")
        return value

    @field_validator("webhook_secret", "ingest_password")
    @classmethod
    def validate_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or secret.startswith(("REPLACE_", "change-me")):
            raise ValueError("Configure the actual server credential")
        return value

    @field_validator("webhook_secret")
    @classmethod
    def validate_signing_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 16:
            raise ValueError("Use the signing secret from the Chatwoot account webhook")
        return value

    @field_validator("allowed_account_ids")
    @classmethod
    def validate_accounts(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",")]
        if not parts or any(not part.isascii() or not part.isdecimal() or not 0 < int(part) <= 2**63 - 1 for part in parts):
            raise ValueError("Allowed account IDs must be comma-separated positive PostgreSQL bigint values")
        return ",".join(str(int(part)) for part in parts)

    @property
    def account_ids(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.allowed_account_ids.split(","))

    @property
    def connection_kwargs(self) -> dict:
        return {
            "host": self.postgres_host, "port": self.postgres_port,
            "dbname": self.postgres_db, "user": self.ingest_user,
            "password": self.ingest_password.get_secret_value(),
            "sslmode": self.postgres_sslmode, "connect_timeout": 2,
            "application_name": "ics-quality-receiver",
            "options": "-c statement_timeout=2000 -c lock_timeout=1000",
        }

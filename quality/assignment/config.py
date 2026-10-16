"""Assignment synchronization uses its own restricted destination credential."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AssignmentSyncSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )

    assignment_sync_enabled: bool = False
    source_key: str = ""
    allowed_account_ids: str = ""
    postgres_host: str = "archive-postgres"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "ics_quality"
    postgres_sslmode: str = "prefer"
    assignment_user: str = "archive_assignment"
    assignment_password: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_enabled_configuration(self):
        if not self.assignment_sync_enabled:
            return self
        if not self.source_key or self.source_key != self.source_key.strip() or len(self.source_key) > 128:
            raise ValueError("Configure the stable archive source key")
        parts = [part.strip() for part in self.allowed_account_ids.split(",")]
        if not parts or any(
            not part.isascii() or not part.isdecimal() or not 0 < int(part) <= 2**63-1
            for part in parts
        ):
            raise ValueError("Configure explicit positive allowed archive account IDs")
        self.allowed_account_ids = ",".join(str(int(part)) for part in parts)
        credential = self.assignment_password.get_secret_value()
        if not credential or credential.startswith(("REPLACE_", "change-me")):
            raise ValueError("Configure the restricted archive assignment credential")
        if not self.assignment_user or self.assignment_user != self.assignment_user.strip():
            raise ValueError("Configure the archive assignment database user")
        return self

    @property
    def account_ids(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.allowed_account_ids.split(","))

    @property
    def connection_kwargs(self) -> dict:
        return {
            "host": self.postgres_host, "port": self.postgres_port,
            "dbname": self.postgres_db, "user": self.assignment_user,
            "password": self.assignment_password.get_secret_value(),
            "sslmode": self.postgres_sslmode, "connect_timeout": 3,
            "application_name": "ics-quality-assignment-target",
            "options": "-c statement_timeout=2000 -c lock_timeout=1000",
        }

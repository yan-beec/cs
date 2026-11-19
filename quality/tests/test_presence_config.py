"""Presence configuration preserves opt-in, credential and resource boundaries."""

from pydantic import SecretStr, ValidationError
import pytest

from quality.presence.config import PresenceSettings


def test_presence_is_disabled_without_any_writer_credential(monkeypatch):
    for name in ("ARCHIVE_PRESENCE_ENABLED", "ARCHIVE_PRESENCE_PASSWORD", "ARCHIVE_PRESENCE_USER", "ARCHIVE_PRESENCE_MAX_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    settings = PresenceSettings(_env_file=None)
    assert settings.presence_enabled is False
    assert settings.presence_user == "archive_presence" and settings.presence_max_agents == 100
    assert settings.presence_password.get_secret_value() == ""


def test_presence_reads_explicit_archive_environment_and_uses_only_its_writer(monkeypatch):
    values = {
        "ARCHIVE_PRESENCE_ENABLED": "true", "ARCHIVE_PRESENCE_USER": "archive_presence",
        "ARCHIVE_PRESENCE_PASSWORD": "presence-secret", "ARCHIVE_PRESENCE_MAX_AGENTS": "200",
        "ARCHIVE_SOURCE_KEY": "test-source", "ARCHIVE_POSTGRES_HOST": "isolated-archive",
        "ARCHIVE_POSTGRES_PORT": "5544", "ARCHIVE_POSTGRES_DB": "test_archive",
        "ARCHIVE_POSTGRES_SSLMODE": "require", "ARCHIVE_READER_PASSWORD": "reader-secret",
        "ARCHIVE_ASSIGNMENT_PASSWORD": "assignment-secret", "ARCHIVE_INGEST_PASSWORD": "ingest-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = PresenceSettings(_env_file=None)
    assert settings.presence_enabled and settings.source_key == "test-source"
    assert settings.presence_max_agents == 200
    kwargs = settings.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["dbname"], kwargs["sslmode"]) == (
        "isolated-archive", 5544, "test_archive", "require",
    )
    assert kwargs["user"] == "archive_presence" and kwargs["password"] == "presence-secret"
    assert kwargs["connect_timeout"] == 2
    assert kwargs["options"] == "-c statement_timeout=3000 -c lock_timeout=500"
    assert isinstance(settings.presence_password, SecretStr)
    assert "presence-secret" not in repr(settings) and "presence-secret" not in settings.model_dump_json()


def test_presence_reads_shared_dotenv_without_requiring_query_credentials(tmp_path, monkeypatch):
    for name in ("ARCHIVE_PRESENCE_ENABLED", "ARCHIVE_PRESENCE_PASSWORD", "ARCHIVE_SOURCE_KEY", "ARCHIVE_POSTGRES_HOST"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / "presence.env"
    env_file.write_text("ARCHIVE_PRESENCE_ENABLED=true\nARCHIVE_PRESENCE_PASSWORD=dotenv-presence-secret\n"
                        "ARCHIVE_SOURCE_KEY=dotenv-source\nARCHIVE_POSTGRES_HOST=dotenv-archive\n"
                        "ARCHIVE_QUERY_ENABLED=false\nARCHIVE_READER_PASSWORD=\n", encoding="utf-8")
    settings = PresenceSettings(_env_file=env_file)
    assert settings.presence_enabled and settings.source_key == "dotenv-source"
    assert settings.connection_kwargs["host"] == "dotenv-archive"
    assert settings.connection_kwargs["password"] == "dotenv-presence-secret"


@pytest.mark.parametrize("password", ["", "REPLACE_WITH_RANDOM_PRESENCE_PASSWORD", "change-me-presence"])
def test_presence_never_falls_back_to_another_archive_role(password):
    settings = PresenceSettings(_env_file=None, presence_enabled=True, presence_password=password,
                                reader_password="reader-secret", assignment_password="assignment-secret",
                                ingest_password="ingest-secret")
    with pytest.raises(ValueError, match="presence credential") as error:
        settings.connection_kwargs
    assert all(secret not in str(error.value) for secret in ("reader-secret", "assignment-secret", "ingest-secret"))


@pytest.mark.parametrize("values", [
    {"presence_max_agents": 0}, {"presence_max_agents": 201},
    {"postgres_port": 0}, {"postgres_port": 65536},
])
def test_presence_rejects_configurations_outside_fixed_bounds_without_printing_secrets(values):
    with pytest.raises(ValidationError) as error:
        PresenceSettings(_env_file=None, presence_password="hidden-secret", **values)
    assert "hidden-secret" not in str(error.value)


@pytest.mark.parametrize("values", [
    {"source_key": ""}, {"source_key": " other-source "}, {"source_key": "s"*129},
    {"presence_user": ""}, {"presence_user": " archive_presence "},
])
def test_presence_validates_source_and_role_before_connecting(values):
    settings = PresenceSettings(_env_file=None, presence_password="hidden-secret", **values)
    with pytest.raises(ValueError) as error:
        settings.connection_kwargs
    assert "hidden-secret" not in str(error.value)

"""验证插件身份与管理员身份隔离、密码配置和登录限流。"""

from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import InvalidAdminCredentials, _decode_admin_token, hash_password
from ics_agent.app.core.config import get_settings
from reminder.backend import auth, routes


def settings():
    return get_settings().model_copy(update={
        "reminder_enabled": True, "chatwoot_account_id": "1",
        "reminder_plugin_username": "reminder",
        "reminder_plugin_password": "plugin-password",
        "reminder_plugin_password_hash": "",
        "reminder_plugin_secret": "test-plugin-secret-" * 3,
    })


def test_plugin_jwt_cannot_be_used_as_admin_even_with_the_same_signing_secret():
    config = settings()
    with patch.object(auth, "get_settings", return_value=config):
        token = auth.login("reminder", "plugin-password")["access_token"]
    admin_config = config.model_copy(update={"admin_jwt_secret": config.reminder_plugin_secret})
    with pytest.raises(InvalidAdminCredentials):
        _decode_admin_token(token, settings=admin_config)


def test_configured_password_hash_takes_precedence():
    config = settings().model_copy(update={"reminder_plugin_password_hash": hash_password("hashed-only-password")})
    with patch.object(auth, "get_settings", return_value=config):
        with pytest.raises(HTTPException) as failed:
            auth.login("reminder", "plugin-password")
        assert failed.value.status_code == 401
        assert auth.login("reminder", "hashed-only-password")["access_token"]


def test_unconfigured_plugin_does_not_fall_back_to_admin_password():
    config = settings().model_copy(update={"reminder_plugin_password": "", "reminder_plugin_password_hash": ""})
    with patch.object(auth, "get_settings", return_value=config):
        with pytest.raises(HTTPException) as failed:
            auth.login(config.admin_username, config.admin_initial_password)
        assert failed.value.status_code == 503


def test_wrong_password_requests_are_limited_before_more_credentials_are_checked():
    config = settings()
    app = FastAPI()
    app.include_router(routes.router)
    with (patch.object(auth, "get_settings", return_value=config),
          patch.object(routes, "get_settings", return_value=config),
          patch.dict(routes._login_attempts, {}, clear=True)):
        client = TestClient(app)
        for _ in range(20):
            assert client.post("/reminder/plugin/login", json={"username": "reminder", "password": "wrong"}).status_code == 401
        response = client.post("/reminder/plugin/login", json={"username": "reminder", "password": "plugin-password"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"

"""提醒插件独立认证；令牌只能操作提醒接口。"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ics_agent.app.core.auth import verify_password
from ics_agent.app.core.config import get_settings

bearer = HTTPBearer(auto_error=False)
ISSUER = "ics-reminder"
AUDIENCE = "ics-reminder-plugin"


def _secret(settings) -> str:
    value = settings.reminder_plugin_secret.strip()
    if not value:
        raise HTTPException(503, "请先配置 REMINDER_PLUGIN_SECRET。")
    return value


def credentials_version(settings) -> str:
    """修改插件密码或授权席位后，使已签发的插件登录会话失效。"""
    value = "\0".join((settings.reminder_plugin_username,
                        settings.reminder_plugin_password_hash,
                        settings.reminder_plugin_password,
                        settings.reminder_plugin_allowed_agent_ids,
                        settings.chatwoot_account_id))
    return hmac.new(_secret(settings).encode(), value.encode(), hashlib.sha256).hexdigest()


def allowed_agents(settings=None) -> set[str] | None:
    settings = settings or get_settings()
    value = settings.reminder_plugin_allowed_agent_ids.strip()
    return {part.strip() for part in value.split(",") if part.strip()} if value else None


def validate_plugin_device(device: dict, settings=None) -> None:
    settings = settings or get_settings()
    if (not (settings.reminder_plugin_password_hash or settings.reminder_plugin_password)
            or not settings.reminder_plugin_secret.strip()
            or device.get("auth_version") != credentials_version(settings)
            or device.get("account_id") != settings.chatwoot_account_id.strip()):
        raise HTTPException(401, "插件密码或授权配置已变化，请重新登录插件。")
    allowed = allowed_agents(settings)
    if allowed is not None and device["agent_id"] not in allowed:
        raise HTTPException(403, "当前席位已不在插件授权范围内。")


def login(username: str, password: str) -> dict[str, Any]:
    settings = get_settings()
    encoded = settings.reminder_plugin_password_hash
    configured = bool(encoded or settings.reminder_plugin_password)
    if not configured:
        raise HTTPException(503, "请先配置提醒插件的独立登录密码。")
    username_ok = hmac.compare_digest(username.encode(), settings.reminder_plugin_username.encode())
    password_ok = (verify_password(password, encoded) if encoded
                   else hmac.compare_digest(password.encode(), settings.reminder_plugin_password.encode()))
    if not username_ok or not password_ok:
        raise HTTPException(401, "插件账号或密码错误。")
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": username,
                        "scope": "reminder_plugin", "iat": now,
                        "exp": now + settings.reminder_plugin_session_expire_seconds,
                        "credentials_version": credentials_version(settings)},
                       _secret(settings), algorithm="HS256")
    return {"access_token": token, "token_type": "bearer",
            "expires_in": settings.reminder_plugin_session_expire_seconds}


async def require_plugin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict[str, Any]:
    settings = get_settings()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "请先登录提醒插件。")
    try:
        claims = jwt.decode(credentials.credentials, _secret(settings), algorithms=["HS256"],
                            issuer=ISSUER, audience=AUDIENCE,
                            options={"require": ["exp", "iat", "sub", "scope", "credentials_version"]})
        if (claims.get("scope") != "reminder_plugin"
                or claims.get("sub") != settings.reminder_plugin_username
                or claims.get("credentials_version") != credentials_version(settings)):
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "插件登录已过期，请重新登录。") from exc
    return claims

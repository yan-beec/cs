# 模块职责：创建单管理员用户、校验密码、签发 JWT 并提供后台接口权限依赖。

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ics_agent.app.config import Settings, get_settings
from ics_agent.app.logging_config import log_event
from ics_agent.faq.repositories import PostgreSQLConnectionPool


logger = logging.getLogger(__name__)

ADMIN_USER_TABLE = "system_users"
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "ics_agent"
JWT_AUDIENCE = "ics_agent_admin"


class InvalidAdminCredentials(RuntimeError):
    """Raised when the administrator username or password is invalid."""


class AdminAuthConfigurationError(RuntimeError):
    """Raised when required administrator authentication settings are missing."""


@dataclass(frozen=True)
class AdminPrincipal:
    """Authenticated administrator identity attached to a request."""

    username: str
    role: str = "system_admin"


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")


def hash_password(password: str) -> str:
    """Hash a password with a salted PBKDF2-SHA256 record."""
    if not password:
        raise ValueError("Administrator password cannot be empty.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        _password_bytes(password),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a PBKDF2 password record without exposing timing differences."""
    try:
        scheme, iterations_text, salt_text, digest_text = encoded_hash.split(
            "$",
            3,
        )
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(
            digest_text.encode("ascii")
        )
    except (TypeError, ValueError):
        return False

    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        _password_bytes(password),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def _build_schema_sql() -> str:
    return f"""
        CREATE TABLE IF NOT EXISTS {ADMIN_USER_TABLE} (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            username VARCHAR(64) NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """.strip()


def ensure_admin_user(
    settings: Settings | None = None,
    *,
    connections: PostgreSQLConnectionPool | None = None,
) -> None:
    """Create the singleton administrator table and seed it once.

    The configured initial password is used only when the singleton row does
    not exist. Existing passwords are never overwritten on application start.
    """
    runtime_settings = settings or get_settings()
    username = runtime_settings.admin_username.strip()
    initial_password = runtime_settings.admin_initial_password
    if not username:
        raise AdminAuthConfigurationError("ADMIN_USERNAME cannot be empty.")

    pool = connections or PostgreSQLConnectionPool()
    connection = pool.acquire()
    try:
        cursor = connection.cursor()
        try:
            with connection.transaction():
                cursor.execute(_build_schema_sql())
                cursor.execute(
                    f"""
                    SELECT username
                    FROM {ADMIN_USER_TABLE}
                    WHERE id = 1
                    FOR UPDATE
                    """
                )
                existing_row = cursor.fetchone()
                if existing_row is None:
                    if not initial_password:
                        raise AdminAuthConfigurationError(
                            "ADMIN_INITIAL_PASSWORD is required to create "
                            "the first system administrator."
                        )
                    cursor.execute(
                        f"""
                        INSERT INTO {ADMIN_USER_TABLE}
                            (id, username, password_hash)
                        VALUES (1, %s, %s)
                        """,
                        (username, hash_password(initial_password)),
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "auth.admin_user.created",
                        username=username,
                    )
                elif str(existing_row[0]) != username:
                    log_event(
                        logger,
                        logging.WARNING,
                        "auth.admin_user.config_username_differs",
                        configured_username=username,
                        database_username=str(existing_row[0]),
                    )
        finally:
            cursor.close()
    finally:
        connection.close()


def _load_active_admin(
    username: str,
    *,
    connections: PostgreSQLConnectionPool | None = None,
) -> tuple[str, str] | None:
    pool = connections or PostgreSQLConnectionPool()
    connection = pool.acquire()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"""
                SELECT username, password_hash
                FROM {ADMIN_USER_TABLE}
                WHERE username = %s AND is_active = TRUE
                LIMIT 1
                """,
                (username,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return str(row[0]), str(row[1])
        finally:
            cursor.close()
    finally:
        connection.close()


def _is_active_admin(
    username: str,
    *,
    connections: PostgreSQLConnectionPool | None = None,
) -> bool:
    return _load_active_admin(username, connections=connections) is not None


def _jwt_secret(settings: Settings) -> str:
    secret = settings.admin_jwt_secret.strip()
    if not secret:
        raise AdminAuthConfigurationError(
            "ADMIN_JWT_SECRET must be configured."
        )
    if (
        settings.app_env.lower() == "production"
        and secret.startswith("change-me")
    ):
        raise AdminAuthConfigurationError(
            "Replace the default ADMIN_JWT_SECRET before production use."
        )
    return secret


def authenticate_admin(
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
    connections: PostgreSQLConnectionPool | None = None,
) -> str:
    """Validate credentials and return a signed administrator JWT."""
    runtime_settings = settings or get_settings()
    if not username.strip() or not password:
        raise InvalidAdminCredentials("Invalid administrator credentials.")

    ensure_admin_user(runtime_settings, connections=connections)
    record = _load_active_admin(
        username.strip(),
        connections=connections,
    )
    if record is None or not verify_password(password, record[1]):
        raise InvalidAdminCredentials("Invalid administrator credentials.")

    issued_at = int(time.time())
    payload = {
        "sub": record[0],
        "role": "system_admin",
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + runtime_settings.admin_access_token_expire_seconds,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(
        payload,
        _jwt_secret(runtime_settings),
        algorithm=JWT_ALGORITHM,
    )
    log_event(
        logger,
        logging.INFO,
        "auth.admin_login.success",
        username=record[0],
    )
    return token


def _decode_admin_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> AdminPrincipal:
    runtime_settings = settings or get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _jwt_secret(runtime_settings),
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["sub", "role", "exp", "iat"]},
        )
    except (jwt.InvalidTokenError, AdminAuthConfigurationError) as exc:
        raise InvalidAdminCredentials("Invalid or expired administrator token.") from exc

    username = str(payload.get("sub", "")).strip()
    role = str(payload.get("role", "")).strip()
    if not username or role != "system_admin":
        raise InvalidAdminCredentials("Invalid administrator token claims.")
    return AdminPrincipal(username=username, role=role)


async def authenticate_token(token: str) -> AdminPrincipal:
    """Decode the token and verify the singleton admin is still active."""
    principal = _decode_admin_token(token)
    active = await asyncio.to_thread(_is_active_admin, principal.username)
    if not active:
        raise InvalidAdminCredentials("Administrator is inactive.")
    return principal


bearer_scheme = HTTPBearer(auto_error=False)


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
) -> AdminPrincipal:
    """FastAPI dependency for protected administrator operations."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Administrator authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await authenticate_token(credentials.credentials)
    except (InvalidAdminCredentials, AdminAuthConfigurationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired administrator credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

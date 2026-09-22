-- Singleton administrator table used by the FastAPI management endpoints.
-- The application seeds the first row from ADMIN_USERNAME and
-- ADMIN_INITIAL_PASSWORD, storing only the PBKDF2 password hash.

CREATE TABLE IF NOT EXISTS system_users (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

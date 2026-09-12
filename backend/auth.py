from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status

ROOT = Path(__file__).resolve().parents[1]
_database_path = Path(os.getenv("DATABASE_PATH", "data/sif.sqlite3"))
DB_PATH = _database_path if _database_path.is_absolute() else ROOT / _database_path
SESSION_DAYS = max(1, int(os.getenv("SESSION_EXPIRE_DAYS", "7")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'disabled')),
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                asins_json TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON analysis_jobs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_logs_created ON request_logs(created_at DESC);
            """
        )
        admin_count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if not admin_count:
            username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
            password = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
            db.execute(
                "INSERT INTO users (username, password_hash, role, status, created_at) VALUES (?, ?, 'admin', 'active', ?)",
                (username, hash_password(password), now_iso()),
            )


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_text, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_text))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "username", "role", "status", "created_at", "last_login_at")}


def find_user_by_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with db_connect() as db:
        row = db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
            (token_digest(token), now_iso()),
        ).fetchone()
    if not row or row["status"] != "active":
        return None
    return public_user(row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    with db_connect() as db:
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token_digest(token), user_id, (created + timedelta(days=SESSION_DAYS)).isoformat(), created.isoformat()),
        )
    return token


def revoke_session(token: str | None) -> None:
    if not token:
        return
    with db_connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(token),))


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    user = find_user_by_token(bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def raw_token_from_request(request: Request) -> str | None:
    return bearer_token(request.headers.get("Authorization"))


def audit_request(request: Request, response_status: int) -> None:
    try:
        user = find_user_by_token(raw_token_from_request(request))
        with db_connect() as db:
            db.execute(
                "INSERT INTO request_logs (user_id, username, method, path, status_code, ip_address, user_agent, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user["id"] if user else None,
                    user["username"] if user else None,
                    request.method,
                    request.url.path,
                    response_status,
                    request.client.host if request.client else None,
                    request.headers.get("user-agent", "")[:500],
                    now_iso(),
                ),
            )
    except Exception:
        # 审计失败不应阻断分析业务。
        pass


init_db()

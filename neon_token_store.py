"""Neon/Postgres storage for the active VSS token."""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg2
from psycopg2.extras import RealDictCursor

import operation_log

log = logging.getLogger("neon_token_store")


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


def configured() -> bool:
    return bool(_env("NEON_DB_URL"))


def table_name() -> str:
    name = _env("NEON_VSS_TOKEN_TABLE", "vss_tokens") or "vss_tokens"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("NEON_VSS_TOKEN_TABLE must be a simple table name")
    return name


def row_id() -> str:
    return _env("NEON_VSS_TOKEN_ID", "active") or "active"


@contextmanager
def _connect() -> Iterator[Any]:
    conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    """Create the single-row VSS token table if it does not exist."""
    if not configured():
        raise RuntimeError("NEON_DB_URL is not configured")
    table = table_name()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                create table if not exists {table} (
                    id text primary key default 'active',
                    token text not null,
                    pid text not null default '',
                    issued_at timestamptz not null,
                    base_url text,
                    profile text,
                    updated_at timestamptz not null default now()
                )
                """
            )


def load_vss_token() -> dict[str, Any] | None:
    """Return the active VSS token row from Neon, or None when no row exists."""
    if not configured():
        return None
    ensure_schema()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                select token, pid, issued_at, base_url, profile
                from {table_name()}
                where id = %s
                limit 1
                """,
                (row_id(),),
            )
            row = cur.fetchone()
    if not row:
        return None
    token = str(row.get("token") or "").strip()
    if not token:
        return None
    issued_at = row.get("issued_at")
    if isinstance(issued_at, datetime) and issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    pid = str(row.get("pid") or "").strip()
    profile = str(row.get("profile") or "").strip()
    return {
        "token": token,
        "pid": pid,
        "issued_at": issued_at,
        "base_url": str(row.get("base_url") or "").strip(),
        "profile": profile,
    }


def save_vss_token(
    token: str,
    pid: str,
    *,
    issued_at: datetime,
    base_url: str | None = None,
    profile: str | None = None,
) -> None:
    """Upsert the active VSS token row in Neon."""
    if not configured():
        raise RuntimeError("NEON_DB_URL is not configured")
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {table_name()} (id, token, pid, issued_at, base_url, profile, updated_at)
                values (%s, %s, %s, %s, %s, %s, now())
                on conflict (id) do update set
                    token = excluded.token,
                    pid = excluded.pid,
                    issued_at = excluded.issued_at,
                    base_url = excluded.base_url,
                    profile = excluded.profile,
                    updated_at = now()
                """,
                (row_id(), token, pid or "", issued_at, base_url or "", profile or ""),
            )
    pid_display = (pid or "")[:12]
    if pid and len(pid) > 12:
        pid_display += "…"
    log.info(
        "VSS token saved to Neon table=%s profile=%s pid=%s",
        table_name(),
        profile or "?",
        pid_display or "—",
    )
    operation_log.log_event(
        "vss_token",
        "save_neon",
        "ok",
        f"VSS token and pid saved to Neon ({table_name()}, profile={profile or '?'}, pid={pid_display or '—'})",
        detail={
            "table": table_name(),
            "profile": profile or "",
            "pid_prefix": (pid or "")[:12],
            "token_prefix": token[:8] if token else "",
            "base_url": base_url or "",
        },
    )

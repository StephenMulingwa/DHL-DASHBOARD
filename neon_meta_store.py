"""Neon key-value metadata (last refresh time, etc.)."""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg2
from psycopg2.extras import Json, RealDictCursor

log = logging.getLogger("neon_meta_store")

LAST_REFRESH_KEY = "last_refresh"


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


def configured() -> bool:
    return bool(_env("NEON_DB_URL"))


def table_name() -> str:
    name = _env("NEON_META_TABLE", "dashboard_meta") or "dashboard_meta"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("NEON_META_TABLE must be a simple table name")
    return name


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
    if not configured():
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                create table if not exists {table_name()} (
                    key text primary key,
                    value jsonb not null,
                    updated_at timestamptz not null default now()
                )
                """
            )


def set_meta(key: str, value: dict[str, Any]) -> None:
    if not configured():
        return
    ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {table_name()} (key, value, updated_at)
                values (%s, %s, now())
                on conflict (key) do update set
                    value = excluded.value,
                    updated_at = now()
                """,
                (key, Json(value)),
            )


def get_meta(key: str) -> dict[str, Any] | None:
    if not configured():
        return None
    ensure_schema()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"select value, updated_at from {table_name()} where key = %s limit 1",
                (key,),
            )
            row = cur.fetchone()
    if not row:
        return None
    val = row.get("value")
    if not isinstance(val, dict):
        return None
    updated_at = row.get("updated_at")
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        val = {**val, "_updated_at": updated_at.isoformat()}
    return val


def record_last_refresh(*, counts: dict[str, int], username: str = "", session_id: str = "") -> None:
    now = datetime.now(timezone.utc)
    set_meta(
        LAST_REFRESH_KEY,
        {
            "at": now.isoformat(),
            "counts": counts,
            "username": username,
            "session_id": session_id,
        },
    )
    log.info("last_refresh recorded at %s counts=%s", now.isoformat(), counts)


def last_refresh_display() -> dict[str, Any] | None:
    """Return last refresh metadata for UI banners."""
    meta = get_meta(LAST_REFRESH_KEY)
    if not meta:
        return None
    at_raw = meta.get("at") or meta.get("_updated_at")
    if not at_raw:
        return None
    try:
        if isinstance(at_raw, str):
            at_dt = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
        else:
            at_dt = at_raw
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    try:
        offset_h = float(os.environ.get("MIX_DISPLAY_TZ_OFFSET_HOURS", "3") or "3")
    except ValueError:
        offset_h = 3.0
    from datetime import timedelta

    local = at_dt + timedelta(hours=offset_h)
    return {
        "at_iso": at_dt.isoformat(),
        "at_display": local.strftime("%a, %b %d, %Y %H:%M:%S"),
        "counts": meta.get("counts") or {},
        "username": meta.get("username") or "",
    }

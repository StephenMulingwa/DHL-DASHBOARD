"""Neon/Postgres snapshots for fast dashboard rendering on Vercel."""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pandas as pd
import psycopg2
from psycopg2.extras import Json, RealDictCursor

import operation_log

log = logging.getLogger("neon_snapshot_store")


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


def configured() -> bool:
    return bool(_env("NEON_DB_URL"))


def table_name() -> str:
    name = _env("NEON_SNAPSHOT_TABLE", "dashboard_snapshots") or "dashboard_snapshots"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("NEON_SNAPSHOT_TABLE must be a simple table name")
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
                    payload jsonb not null,
                    row_count integer not null default 0,
                    updated_at timestamptz not null default now()
                )
                """
            )


def save_frame(key: str, df: pd.DataFrame) -> None:
    if not configured() or df is None:
        return
    ensure_schema()
    payload = json.loads(df.to_json(orient="split", date_format="iso", default_handler=str))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {table_name()} (key, payload, row_count, updated_at)
                values (%s, %s, %s, now())
                on conflict (key) do update set
                    payload = excluded.payload,
                    row_count = excluded.row_count,
                    updated_at = now()
                """,
                (key, Json(payload), int(len(df))),
            )
    row_count = int(len(df))
    category = "mix_data" if key.startswith("mix") else "vss_data"
    log.info("Neon snapshot saved key=%s rows=%s table=%s", key, row_count, table_name())
    operation_log.log_event(
        category,
        "save_neon",
        "ok",
        f"{key}: {row_count} rows saved to Neon ({table_name()})",
        detail={"key": key, "row_count": row_count, "table": table_name()},
    )


def load_frame(key: str) -> tuple[pd.DataFrame, datetime] | None:
    if not configured():
        return None
    ensure_schema()
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                select payload, updated_at
                from {table_name()}
                where key = %s
                limit 1
                """,
                (key,),
            )
            row = cur.fetchone()
    if not row:
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    df = pd.DataFrame(data=payload.get("data") or [], columns=payload.get("columns") or [])
    updated_at = row.get("updated_at") or datetime.now(timezone.utc)
    if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return df, updated_at

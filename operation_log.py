"""In-app operation log — memory ring buffer + Neon persistence, grouped by session."""

from __future__ import annotations

import contextvars
import logging
import os
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor

log = logging.getLogger("operation_log")

_RING_MAX = 100
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_MAX)
_ring_lock = __import__("threading").Lock()
_next_mem_id = 0
_schema_ready = False
_schema_lock = __import__("threading").Lock()

_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "operation_log_session", default=None
)


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


def configured() -> bool:
    return bool(_env("NEON_DB_URL"))


def table_name() -> str:
    name = _env("NEON_OPERATION_LOG_TABLE", "operation_logs") or "operation_logs"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("NEON_OPERATION_LOG_TABLE must be a simple table name")
    return name


def sessions_table_name() -> str:
    name = _env("NEON_OPERATION_SESSIONS_TABLE", "operation_sessions") or "operation_sessions"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("NEON_OPERATION_SESSIONS_TABLE must be a simple table name")
    return name


def ensure_schema() -> None:
    global _schema_ready
    if not configured():
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    create table if not exists {sessions_table_name()} (
                        id text primary key,
                        trigger text not null,
                        username text,
                        started_at timestamptz not null default now(),
                        ended_at timestamptz,
                        status text not null default 'running'
                    )
                    """
                )
                cur.execute(
                    f"""
                    create table if not exists {table_name()} (
                        id bigserial primary key,
                        session_id text,
                        ts timestamptz not null default now(),
                        category text not null,
                        step text not null,
                        status text not null,
                        message text not null,
                        detail jsonb
                    )
                    """
                )
                cur.execute(
                    f"""
                    alter table {table_name()}
                    add column if not exists session_id text
                    """
                )
                cur.execute(
                    f"""
                    create index if not exists operation_logs_session_idx
                    on {table_name()} (session_id, id)
                    """
                )
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def start_session(trigger: str, *, username: str = "") -> str:
    """Begin a new log session (login or refresh). Returns session id."""
    sid = str(uuid.uuid4())
    _current_session.set(sid)
    now = datetime.now(timezone.utc)
    if configured():
        try:
            ensure_schema()
            conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        insert into {sessions_table_name()} (id, trigger, username, started_at, status)
                        values (%s, %s, %s, %s, 'running')
                        """,
                        (sid, trigger, username or "", now),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("operation session start skipped: %s", exc)
    label = "Login" if trigger == "login" else "Refresh data"
    log_event(
        "system",
        "session_start",
        "running",
        f"{label} session started",
        session_id=sid,
        detail={"trigger": trigger, "username": username},
    )
    return sid


def end_session(session_id: str, status: str, *, message: str = "") -> None:
    """Mark session complete."""
    now = datetime.now(timezone.utc)
    final_status = "ok" if status in ("ok", "running") else "error"
    if configured():
        try:
            ensure_schema()
            conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        update {sessions_table_name()}
                        set ended_at = %s, status = %s
                        where id = %s
                        """,
                        (now, final_status, session_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("operation session end skipped: %s", exc)
    log_event(
        "system",
        "session_end",
        final_status,
        message or f"Session {'completed' if final_status == 'ok' else 'failed'}",
        session_id=session_id,
    )
    if _current_session.get() == session_id:
        _current_session.set(None)


def set_current_session(session_id: str | None) -> None:
    _current_session.set(session_id)


def log_event(
    category: str,
    step: str,
    status: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record a pipeline step to memory and Neon (best-effort)."""
    global _next_mem_id
    sid = session_id or _current_session.get()
    now = datetime.now(timezone.utc)
    with _ring_lock:
        _next_mem_id += 1
        entry: dict[str, Any] = {
            "id": _next_mem_id,
            "session_id": sid,
            "ts": now.isoformat(),
            "category": category,
            "step": step,
            "status": status,
            "message": message,
            "detail": detail or {},
        }
        _ring.append(entry)

    if configured():
        try:
            ensure_schema()
            conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        insert into {table_name()}
                            (session_id, ts, category, step, status, message, detail)
                        values (%s, %s, %s, %s, %s, %s, %s)
                        returning id, ts
                        """,
                        (sid, now, category, step, status, message, Json(detail or {})),
                    )
                    row = cur.fetchone()
                    if row:
                        entry["id"] = int(row["id"])
                        ts = row["ts"]
                        if isinstance(ts, datetime):
                            entry["ts"] = ts.astimezone(timezone.utc).isoformat()
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("operation_log Neon write skipped: %s", exc)

    log.info("[%s/%s/%s] %s", category, step, status, message)
    return entry


def _format_ts(ts: Any) -> str:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).isoformat()
    return str(ts)


def fetch_events(*, since_id: int = 0, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
    """Return recent events from Neon (preferred) or memory ring."""
    limit = max(1, min(int(limit), 500))
    if configured():
        try:
            ensure_schema()
            conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if session_id:
                        cur.execute(
                            f"""
                            select id, session_id, ts, category, step, status, message, detail
                            from {table_name()}
                            where session_id = %s and id > %s
                            order by id asc
                            limit %s
                            """,
                            (session_id, since_id, limit),
                        )
                    else:
                        cur.execute(
                            f"""
                            select id, session_id, ts, category, step, status, message, detail
                            from {table_name()}
                            where id > %s
                            order by id desc
                            limit %s
                            """,
                            (since_id, limit),
                        )
                    rows = cur.fetchall()
            finally:
                conn.close()
            out: list[dict[str, Any]] = []
            for row in reversed(rows) if not session_id else rows:
                out.append(
                    {
                        "id": int(row["id"]),
                        "session_id": row.get("session_id"),
                        "ts": _format_ts(row.get("ts")),
                        "category": row["category"],
                        "step": row["step"],
                        "status": row["status"],
                        "message": row["message"],
                        "detail": row.get("detail") or {},
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            log.debug("operation_log Neon read failed: %s", exc)

    with _ring_lock:
        items = [e for e in _ring if int(e.get("id") or 0) > since_id]
    return items[-limit:]


def fetch_sessions(*, limit: int = 15) -> list[dict[str, Any]]:
    """Return recent sessions with nested events for structured logs UI."""
    limit = max(1, min(int(limit), 50))
    if not configured():
        return []
    try:
        ensure_schema()
        conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    select id, trigger, username, started_at, ended_at, status
                    from {sessions_table_name()}
                    order by started_at desc
                    limit %s
                    """,
                    (limit,),
                )
                sessions = cur.fetchall()
            result: list[dict[str, Any]] = []
            try:
                offset_h = float(os.environ.get("MIX_DISPLAY_TZ_OFFSET_HOURS", "3") or "3")
            except ValueError:
                offset_h = 3.0
            from datetime import timedelta

            for sess in sessions:
                sid = sess["id"]
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        select id, session_id, ts, category, step, status, message, detail
                        from {table_name()}
                        where session_id = %s
                        order by id asc
                        """,
                        (sid,),
                    )
                    events = cur.fetchall()
                started = sess.get("started_at")
                if isinstance(started, datetime):
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    started_display = (started + timedelta(hours=offset_h)).strftime(
                        "%a, %b %d %H:%M:%S"
                    )
                else:
                    started_display = str(started)
                trigger = sess.get("trigger") or "unknown"
                title = "Login" if trigger == "login" else "Refresh data"
                result.append(
                    {
                        "id": sid,
                        "trigger": trigger,
                        "title": title,
                        "username": sess.get("username") or "",
                        "started_at": _format_ts(started),
                        "started_display": started_display,
                        "ended_at": _format_ts(sess.get("ended_at")) if sess.get("ended_at") else None,
                        "status": sess.get("status") or "running",
                        "events": [
                            {
                                "id": int(ev["id"]),
                                "session_id": ev.get("session_id"),
                                "ts": _format_ts(ev.get("ts")),
                                "category": ev["category"],
                                "step": ev["step"],
                                "status": ev["status"],
                                "message": ev["message"],
                                "detail": ev.get("detail") or {},
                            }
                            for ev in events
                        ],
                    }
                )
            return result
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_sessions failed: %s", exc)
        return []


def latest_event_id() -> int:
    if configured():
        try:
            ensure_schema()
            conn = psycopg2.connect(_env("NEON_DB_URL"), connect_timeout=12)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"select coalesce(max(id), 0) from {table_name()}")
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
    with _ring_lock:
        if not _ring:
            return 0
        return max(int(e.get("id") or 0) for e in _ring)

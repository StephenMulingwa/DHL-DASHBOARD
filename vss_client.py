"""VSS API client (lifted from positions.ipynb cell 0).

Centralizes:
- Login + token caching (handles 10082 "login too frequently")
- POST helpers with retry/backoff (10129 "too frequent" handling)
- Endpoints used by the dashboard: fleets, devices, realtime status, alarm list, lang dict.

Credentials are read from environment variables; see .env.example / README.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


BASE_URL: str = _env("VSS_BASE_URL", "http://40.76.130.233:9966")
USERNAME: str = _env("VSS_USERNAME", "mawa@controltech-ea.com")
PASSWORD_PLAINTEXT: str = _env("VSS_PASSWORD", "Kenya+123")

_TOKEN_FILE = Path(__file__).resolve().parent / ".vss_token.txt"
_TOKEN_JSON_FILE = Path(__file__).resolve().parent / ".vss_token.json"


@dataclass(frozen=True)
class VssProfile:
    name: str
    base_url: str
    username: str
    password: str


@dataclass(frozen=True)
class _TokenRecord:
    token: str
    pid: str
    issued_at: datetime | None = None
    base_url: str | None = None
    profile: str | None = None


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


_active_base_url: str = _normalize_base_url(BASE_URL) or BASE_URL
_active_profile_name: str | None = None
_LAST_VSS_ERROR: str | None = None


def _credential_profiles() -> list[VssProfile]:
    profiles: list[VssProfile] = []
    primary_url = _normalize_base_url(_env("VSS_BASE_URL"))
    primary_user = _env("VSS_USERNAME")
    primary_pass = _env("VSS_PASSWORD")
    if primary_url and primary_user and primary_pass:
        profiles.append(VssProfile("primary", primary_url, primary_user, primary_pass))

    secondary_url = _normalize_base_url(_env("VSS_BASE_URL_N"))
    secondary_user = _env("VSS_USERNAME_N")
    secondary_pass = _env("VSS_PASSWORD_N")
    if secondary_url and secondary_user and secondary_pass:
        profiles.append(VssProfile("secondary", secondary_url, secondary_user, secondary_pass))

    prefer = _env("VSS_PREFERRED_PROFILE")
    if prefer and len(profiles) > 1:
        profiles.sort(key=lambda p: (0 if p.name == prefer else 1, p.name))

    if profiles:
        return profiles

    # Legacy module defaults when env is empty.
    if USERNAME and PASSWORD_PLAINTEXT:
        profiles.append(
            VssProfile(
                "primary",
                _normalize_base_url(BASE_URL) or BASE_URL,
                USERNAME,
                PASSWORD_PLAINTEXT,
            )
        )
    return profiles


def active_base_url() -> str:
    return _active_base_url or _normalize_base_url(BASE_URL) or BASE_URL


def last_vss_profile() -> str | None:
    return _active_profile_name


def last_vss_error() -> str | None:
    return _LAST_VSS_ERROR


def _set_last_vss_error(msg: str | None) -> None:
    global _LAST_VSS_ERROR
    _LAST_VSS_ERROR = (msg or "").strip() or None


def _set_active_profile(profile: VssProfile) -> None:
    global _active_base_url, _active_profile_name
    _active_base_url = profile.base_url
    _active_profile_name = profile.name


def _ssl_verify_for_url(base_url: str) -> bool:
    explicit = _env("VSS_SSL_VERIFY")
    if explicit:
        return _env_truthy("VSS_SSL_VERIFY")
    if base_url.startswith("https://") and "controltech" in base_url.lower():
        return False
    return True


def _token_ttl_hours() -> float:
    try:
        return max(1.0, float(_env("VSS_TOKEN_TTL_HOURS", "23") or "23"))
    except ValueError:
        return 23.0


def _parse_issued_at(raw: str | int | float | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _token_is_expired(issued_at: datetime | None) -> bool:
    if issued_at is None:
        return bool(_credential_profiles())
    ttl = timedelta(hours=_token_ttl_hours())
    now = datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    return now - issued_at >= ttl


def _token_file_mtime() -> float | None:
    for path in (_TOKEN_JSON_FILE, _TOKEN_FILE):
        if path.is_file():
            try:
                return path.stat().st_mtime
            except OSError:
                continue
    return None

_vss_log = logging.getLogger("vss_client")

_POOL_MAXSIZE: int = int(_env("VSS_POOL_MAXSIZE", "50") or "50")

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})
_session.mount("http://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))
_session.mount("https://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))

_lock = threading.Lock()
_vss_api_lock = threading.Lock()
_discover_lock = threading.Lock()
_VSS_TOKEN: str | None = None
_VSS_PID: str | None = None
_VSS_TOKEN_AT: datetime | None = None
# Set inside ``ensure_token`` for debugging: memory / env / file / login.
_LAST_TOKEN_SOURCE: str | None = None
# Mtime of ``.vss_token.txt`` when the in-memory token was last aligned with that file
# (used to pick up hand-edited tokens without restarting the process).
_FILE_TOKEN_MTIME: float | None = None
_LAST_10082_AT: float | None = None


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _reset_session() -> None:
    global _session
    try:
        _session.close()
    except Exception:
        pass
    _session = requests.Session()
    _session.headers.update({"Content-Type": "application/json"})
    _session.mount("http://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))
    _session.mount("https://", HTTPAdapter(pool_connections=_POOL_MAXSIZE, pool_maxsize=_POOL_MAXSIZE))


def vss_post_raw(path: str, payload: dict, timeout: int = 25, max_attempts: int = 5) -> dict:
    base = active_base_url()
    url = f"{base}{path}"
    verify = _ssl_verify_for_url(base)
    delay_s = 1.0
    last_exc: Exception | None = None

    with _vss_api_lock:
        for attempt in range(1, max_attempts + 1):
            try:
                r = _session.post(url, json=payload, timeout=timeout, verify=verify)
                r.raise_for_status()
                return r.json()
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt == max_attempts:
                    raise
                time.sleep(delay_s)
                delay_s = min(delay_s * 1.8, 10.0)
                _reset_session()

    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")


def vss_post(path: str, payload: dict, timeout: int = 25, max_wait_seconds: int = 300) -> dict:
    started = time.time()
    delay_s = 1.5

    while True:
        j = vss_post_raw(path, payload, timeout=timeout)
        status = j.get("status")
        if status == 10000:
            return j
        if status == 10129:
            if time.time() - started + delay_s > max_wait_seconds:
                raise RuntimeError(f"VSS rate-limited (10129) after {int(time.time() - started)}s full={j}")
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.7, 30.0)
            continue
        raise RuntimeError(f"VSS error status={status} msg={j.get('msg')} full={j}")


def _load_token_from_file() -> tuple[str, str] | None:
    rec = _load_token_record()
    return (rec.token, rec.pid) if rec else None


def _load_token_record() -> _TokenRecord | None:
    if _TOKEN_JSON_FILE.is_file():
        try:
            data = json.loads(_TOKEN_JSON_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tok = str(data.get("token") or "").strip()
                pid = str(data.get("pid") or "").strip()
                issued_at = _parse_issued_at(data.get("issued_at"))
                base_url = _normalize_base_url(str(data.get("base_url") or "")) or None
                profile = str(data.get("profile") or "").strip() or None
                if tok:
                    return _TokenRecord(tok, pid, issued_at, base_url, profile)
        except Exception:
            pass

    if not _TOKEN_FILE.is_file():
        return None
    try:
        lines = [ln.strip() for ln in _TOKEN_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return None
        parts = lines[0].split()
        tok = parts[0].strip()
        pid = parts[1].strip() if len(parts) > 1 else ""
        if not pid and len(lines) > 1:
            pid = lines[1].strip()
        if not tok:
            return None
        return _TokenRecord(tok, pid, None, None, None)
    except Exception:
        return None


def _save_token_to_file(
    token: str,
    pid: str,
    *,
    issued_at: datetime | None = None,
    base_url: str | None = None,
    profile: str | None = None,
) -> None:
    when = issued_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    payload: dict[str, str] = {
        "token": token,
        "pid": pid,
        "issued_at": when.isoformat(),
    }
    store_base = base_url or active_base_url()
    store_profile = profile or _active_profile_name
    if store_base:
        payload["base_url"] = store_base
    if store_profile:
        payload["profile"] = store_profile
    try:
        _TOKEN_JSON_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _TOKEN_FILE.write_text(f"{token} {pid}\n", encoding="utf-8")
    except Exception:
        pass


def _login_and_persist(
    *,
    login_max_wait_seconds: int | None = None,
    allow_10082_retry: bool = False,
) -> tuple[str, str]:
    if login_max_wait_seconds is not None:
        max_wait = max(30, int(login_max_wait_seconds))
    else:
        max_wait = int(float(_env("VSS_LOGIN_MAX_WAIT", "600") or "600"))
        max_wait = max(120, max_wait)
    token, pid = login_with_backoff(max_wait_seconds=max_wait, allow_10082_retry=allow_10082_retry)
    now = datetime.now(timezone.utc)
    _save_token_to_file(token, pid, issued_at=now)
    _set_last_vss_error(None)
    _vss_log.info("VSS token saved to .vss_token.json (profile=%s)", _active_profile_name or "?")
    return token, pid


def _api_login(profile: VssProfile, *, timeout: int = 30) -> dict:
    url = f"{profile.base_url}/vss/user/apiLogin.action"
    verify = _ssl_verify_for_url(profile.base_url)
    with _vss_api_lock:
        r = _session.post(
            url,
            json={"username": profile.username, "password": md5_hex(profile.password)},
            timeout=timeout,
            verify=verify,
        )
    r.raise_for_status()
    return r.json()


def _login_cooldown_active() -> bool:
    if _LAST_10082_AT is None:
        return False
    try:
        cooldown = float(_env("VSS_10082_COOLDOWN_SEC", "600") or "600")
    except ValueError:
        cooldown = 600.0
    return time.time() - _LAST_10082_AT < max(120.0, cooldown)


def _mark_10082() -> None:
    global _LAST_10082_AT
    _LAST_10082_AT = time.time()


def login_with_backoff(max_wait_seconds: int = 60, *, allow_10082_retry: bool = False) -> tuple[str, str]:
    """Log in via apiLogin across configured profiles, backing off on 10082."""
    if _login_cooldown_active() and not allow_10082_retry:
        wait = max(0.0, float(_env("VSS_10082_COOLDOWN_SEC", "600") or "600") - (time.time() - (_LAST_10082_AT or 0)))
        msg = (
            f"VSS login temporarily blocked after rate-limit (10082). "
            f"Wait ~{int(wait)}s or paste a fresh token+pid into .vss_token.json."
        )
        _set_last_vss_error(msg)
        raise RuntimeError(msg)

    profiles = _credential_profiles()
    if not profiles:
        msg = "No VSS credential profiles configured (set VSS_* or VSS_*_N in .env)"
        _set_last_vss_error(msg)
        raise RuntimeError(msg)

    cool_10082 = float(_env("VSS_10082_SLEEP_SEC", "120") or "120")
    cool_10082 = max(60.0, cool_10082)
    errors: list[str] = []

    for profile_idx, profile in enumerate(profiles):
        started = time.time()
        while True:
            try:
                j = _api_login(profile)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                errors.append(f"{profile.name}: connection error ({exc})")
                break

            status = j.get("status")
            if status == 10000 and isinstance(j.get("data"), dict):
                data = j["data"]
                tok = str(data.get("token") or "")
                pid = str(data.get("pid") or "")
                _set_active_profile(profile)
                _vss_log.info(
                    "VSS login OK via %s (%s), token stored for %.0fh",
                    profile.name,
                    profile.base_url,
                    _token_ttl_hours(),
                )
                return tok, pid

            if status == 10001:
                msg = f"{profile.name} ({profile.base_url}): wrong username/password"
                _vss_log.warning("VSS login failed (10001) — %s", msg)
                errors.append(msg)
                break

            if status == 10082:
                _mark_10082()
                if (
                    _load_token_from_file()
                    and not _env_truthy("VSS_10082_RETRY_LOGIN")
                    and not allow_10082_retry
                ):
                    msg = (
                        f"VSS login rate-limited (10082) on {profile.name}. "
                        "Use stored .vss_token.json or wait ~10 min."
                    )
                    _set_last_vss_error(msg)
                    raise RuntimeError(f"{msg} full={j}")
                elapsed = time.time() - started
                if elapsed >= max_wait_seconds:
                    msg = f"VSS login rate-limited (10082) on {profile.name} after {int(elapsed)}s"
                    errors.append(msg)
                    break
                wait = min(cool_10082, max(0.0, max_wait_seconds - elapsed - 1.0))
                if wait < 5.0:
                    errors.append(f"{profile.name}: 10082 lockout, not enough wait budget left")
                    break
                _vss_log.warning(
                    "VSS profile %s rate-limited (10082) — waiting %.0fs before retry",
                    profile.name,
                    wait,
                )
                time.sleep(wait)
                continue

            msg = f"{profile.name}: status={status} msg={j.get('msg')}"
            errors.append(msg)
            break

        if profile_idx < len(profiles) - 1:
            _vss_log.info("Trying next VSS credential profile…")

    combined = "; ".join(errors) or "all profiles failed"
    _set_last_vss_error(combined)
    raise RuntimeError(f"VSS login failed for all profiles: {combined}")


def _env_truthy(name: str, default: str = "0") -> bool:
    v = _env(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _has_persisted_token() -> bool:
    return _TOKEN_JSON_FILE.is_file() or _TOKEN_FILE.is_file()


def _stored_token_issued_at() -> datetime | None:
    rec = _load_token_record()
    if rec and rec.issued_at:
        return rec.issued_at
    return _VSS_TOKEN_AT


def _stored_token_within_ttl() -> bool:
    """True when saved token+pid is still inside the 23h reuse window."""
    issued_at = _stored_token_issued_at()
    if issued_at is None:
        return True
    return not _token_is_expired(issued_at)


def _vss_credentials_in_env() -> bool:
    """True when at least one VSS credential profile is configured."""
    return bool(_credential_profiles())


_thread_ctx = threading.local()


def set_vss_no_login(enabled: bool) -> None:
    """When True, ``ensure_token`` never calls apiLogin (used during manual/auto refresh)."""
    _thread_ctx.no_login = enabled


def _vss_no_login() -> bool:
    return bool(getattr(_thread_ctx, "no_login", False))


@contextmanager
def vss_no_login_mode():
    """Reuse the in-memory VSS session during refresh — no apiLogin / file reload."""
    set_vss_no_login(True)
    try:
        yield
    finally:
        set_vss_no_login(False)


def try_token_without_login() -> tuple[str, str] | None:
    """Return a token from memory, env, or file without calling apiLogin."""
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME

    with _lock:
        if _VSS_TOKEN:
            _LAST_TOKEN_SOURCE = "memory"
            return _VSS_TOKEN, _VSS_PID or ""

        env_tok = _env("VSS_TOKEN")
        env_pid = _env("VSS_PID")
        if env_tok:
            _LAST_TOKEN_SOURCE = "env"
            _FILE_TOKEN_MTIME = None
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = env_tok, env_pid, datetime.now(timezone.utc)
            return _VSS_TOKEN, _VSS_PID or ""

        rec = _load_token_record()
        if rec:
            return _apply_token_record(rec, source="file")
    return None


def _apply_token_record(
    rec: _TokenRecord,
    *,
    source: str,
) -> tuple[str, str]:
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME
    global _active_base_url, _active_profile_name
    _LAST_TOKEN_SOURCE = source
    _VSS_TOKEN, _VSS_PID = rec.token, rec.pid
    issued_at = rec.issued_at
    mt = _token_file_mtime()
    if mt is not None:
        file_dt = datetime.fromtimestamp(mt, tz=timezone.utc)
        if issued_at is None:
            issued_at = file_dt
        else:
            if issued_at.tzinfo is None:
                issued_at = issued_at.replace(tzinfo=timezone.utc)
            if file_dt > issued_at + timedelta(seconds=2):
                issued_at = file_dt
    _VSS_TOKEN_AT = issued_at or datetime.now(timezone.utc)
    if rec.base_url:
        _active_base_url = rec.base_url
    if rec.profile:
        _active_profile_name = rec.profile
    _FILE_TOKEN_MTIME = mt
    _set_last_vss_error(None)
    return _VSS_TOKEN, _VSS_PID or ""


def _memory_token_expired() -> bool:
    if not _VSS_TOKEN:
        return False
    return _token_is_expired(_VSS_TOKEN_AT)


def refresh_token_if_expired(*, login_max_wait_seconds: int | None = None) -> bool:
    """Proactively refresh stored token when age >= VSS_TOKEN_TTL_HOURS (default 23h)."""
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME

    if _env("VSS_TOKEN"):
        return False

    with _lock:
        issued_at = _stored_token_issued_at()
        if not _token_is_expired(issued_at):
            return False
        if _vss_no_login():
            return False
        if not _vss_credentials_in_env():
            return False
        try:
            token, pid = _login_and_persist(
                login_max_wait_seconds=login_max_wait_seconds,
                allow_10082_retry=True,
            )
        except Exception as exc:
            _vss_log.warning("token refresh failed: %s", exc)
            return False
        _LAST_TOKEN_SOURCE = "login"
        now = datetime.now(timezone.utc)
        _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = token, pid, now
        try:
            _FILE_TOKEN_MTIME = _TOKEN_JSON_FILE.stat().st_mtime
        except OSError:
            _FILE_TOKEN_MTIME = None
        _vss_log.info("VSS token refreshed proactively (TTL %.0fh)", _token_ttl_hours())
        return True


def ensure_token(
    *,
    force: bool = False,
    skip_file: bool = False,
    login_max_wait_seconds: int | None = None,
    allow_10082_retry: bool = False,
) -> tuple[str, str]:
    """Return the active VSS token.

    Lookup order (when no in-memory token exists yet, or ``force=True``):

      1) ``VSS_TOKEN`` env var
      2) ``.vss_token.json`` / ``.vss_token.txt`` (preferred when present)
      3) apiLogin via ``VSS_*`` / ``VSS_*_N`` profiles in ``.env`` (saved to json)

    Set ``skip_file=True`` to force step 3 (used when stored token returns 10023).
    """
    global _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT, _LAST_TOKEN_SOURCE, _FILE_TOKEN_MTIME

    with _lock:
        # PID may be empty on some responses; token alone must still count as a session.
        if not force and _VSS_TOKEN:
            mt = _token_file_mtime()
            if _FILE_TOKEN_MTIME is not None and mt is not None:
                try:
                    if mt > _FILE_TOKEN_MTIME:
                        _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = None, None, None
                        _FILE_TOKEN_MTIME = None
                except OSError:
                    pass
            if _VSS_TOKEN and not _memory_token_expired():
                _LAST_TOKEN_SOURCE = "memory"
                return _VSS_TOKEN, _VSS_PID or ""
            if _VSS_TOKEN and _memory_token_expired() and not _vss_no_login() and _vss_credentials_in_env():
                _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = None, None, None

        if force:
            # Drop the stale in-memory token so a newly pasted env/file token can take over
            # without forcing another apiLogin attempt.
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = None, None, None
            _FILE_TOKEN_MTIME = None

        env_tok = _env("VSS_TOKEN")
        env_pid = _env("VSS_PID")
        if env_tok:
            _LAST_TOKEN_SOURCE = "env"
            _FILE_TOKEN_MTIME = None
            _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = env_tok, env_pid, datetime.now(timezone.utc)
            return _VSS_TOKEN, _VSS_PID or ""

        if not skip_file:
            rec = _load_token_record()
            if rec:
                return _apply_token_record(rec, source="file")

        if _vss_no_login():
            rec = _load_token_record()
            if rec:
                return _apply_token_record(rec, source="file")
            raise RuntimeError(
                "VSS token not available for refresh (no apiLogin in refresh mode). "
                "Paste a new token into .vss_token.json or restart the app."
            )

        if not _vss_credentials_in_env():
            rec = _load_token_record()
            if rec:
                return _apply_token_record(rec, source="file")
            raise RuntimeError("No VSS credentials in .env (VSS_USERNAME/VSS_PASSWORD).")

        retry_login = allow_10082_retry or skip_file
        token, pid = _login_and_persist(
            login_max_wait_seconds=login_max_wait_seconds,
            allow_10082_retry=retry_login,
        )
        _LAST_TOKEN_SOURCE = "login"
        now = datetime.now(timezone.utc)
        _VSS_TOKEN, _VSS_PID, _VSS_TOKEN_AT = token, pid, now
        try:
            _FILE_TOKEN_MTIME = _TOKEN_JSON_FILE.stat().st_mtime
        except OSError:
            _FILE_TOKEN_MTIME = None
        return token, pid


def last_vss_token_source() -> str | None:
    """Where ``ensure_token()`` last took the token from: ``memory``, ``env``, ``file``, or ``login``."""
    return _LAST_TOKEN_SOURCE


def get_current_token() -> tuple[str, str] | None:
    """Return the in-memory token without ever logging in (for inspection)."""
    with _lock:
        if _VSS_TOKEN:
            return _VSS_TOKEN, _VSS_PID or ""
    return None


def _token_for_keepalive(*, allow_reauth: bool) -> tuple[str, str] | None:
    """Prefer in-memory token; never log in when refresh/no-reauth mode is active."""
    tok = get_current_token()
    if tok:
        return tok
    if _vss_no_login() or not allow_reauth:
        return try_token_without_login()
    return ensure_token()


def _token_api_status(token: str, path: str, payload: dict) -> int | None:
    """POST to a VSS endpoint and return the JSON status code."""
    payload = {**payload, "token": token}
    try:
        j = vss_post_raw(path, payload, timeout=20, max_attempts=2)
        return j.get("status") if isinstance(j, dict) else None
    except Exception:
        return None


def _token_is_live(token: str) -> bool:
    """True when token works on a real data endpoint (not just lang dict)."""
    st = _token_api_status(
        token,
        "/vss/fleet/findAll.action",
        {"pageNum": -1, "pageCount": -1},
    )
    return st in (10000, 10025)


def keepalive_ping(*, allow_reauth: bool = True) -> bool:
    """Touch a tiny endpoint to reset the VSS 30-min inactivity timer.

    Returns True if the token is still alive (or got refreshed), False if VSS
    is currently rejecting logins (10082) and the dashboard should keep using
    its cached data for now.

    When ``allow_reauth`` is False (refresh / background keepalive), the same
    in-memory token is pinged without reloading ``.vss_token.txt`` or calling
    apiLogin on 10023.
    """
    tok = _token_for_keepalive(allow_reauth=allow_reauth)
    if not tok:
        return False
    token, _ = tok
    if _token_is_live(token):
        return True
    status = _token_api_status(token, "/vss/lang/findLangDict.action", {"terminal": 2, "lang": "en"})
    if status == 10023:
        if _vss_no_login() or not allow_reauth:
            return False
        try:
            _vss_log.info("VSS stored token rejected (10023) — apiLogin via .env and saving .vss_token.json")
            token2, _ = ensure_token(
                force=True,
                skip_file=True,
                login_max_wait_seconds=120,
                allow_10082_retry=True,
            )
            return _token_is_live(token2)
        except RuntimeError:
            return False
    # A language-dictionary success is not enough: some expired sessions still
    # pass that endpoint while fleet/device endpoints return 10023.
    return False


def validate_or_renew_token(*, allow_reauth: bool = True) -> tuple[bool, str]:
    """Make sure the in-memory token is actually valid before heavy work.

    Returns (ok, message). When ok is False, the caller should fall back to
    cached data and try again on the next refresh tick.
    """
    try:
        ok = keepalive_ping(allow_reauth=allow_reauth)
        return (
            ok,
            "ok"
            if ok
            else "VSS session invalid/expired or login throttled (10082); using cached data",
        )
    except RuntimeError as e:
        return (False, f"token check failed: {e}")


def _retry_on_session_expired(call_fn):
    """Run ``call_fn(token)``; on 10023 reload file token, then apiLogin from .env."""
    token, _ = ensure_token()
    try:
        return call_fn(token)
    except RuntimeError as e:
        msg = str(e)
        if "10023" not in msg and "session has expired" not in msg.lower():
            raise
        token, _ = ensure_token(force=True)
        try:
            return call_fn(token)
        except RuntimeError as e2:
            msg2 = str(e2)
            if "10023" not in msg2 and "session has expired" not in msg2.lower():
                raise
            time.sleep(2.0)
            try:
                return call_fn(token)
            except RuntimeError:
                pass
            if not _vss_credentials_in_env() or _login_cooldown_active():
                err = (
                    "VSS session expired (10023). Paste a fresh token+pid into .vss_token.json "
                    "or wait for login rate-limit to clear."
                )
                _set_last_vss_error(err)
                raise RuntimeError(err) from e2
            _vss_log.info("VSS stored token rejected (10023) — apiLogin via .env profiles")
            token, _ = ensure_token(
                force=True,
                skip_file=True,
                login_max_wait_seconds=120,
                allow_10082_retry=True,
            )
            return call_fn(token)


def fleet_id_csv(fleet_ids: list[str] | str | None) -> str:
    if not fleet_ids:
        return ""
    if isinstance(fleet_ids, str):
        return fleet_ids
    return ",".join([str(x).strip() for x in fleet_ids if str(x).strip()])


def list_devices_page(token: str, page_num: int, page_count: int = 200, *, keyword: str = "", fleetid: str = "") -> dict:
    payload: dict[str, Any] = {
        "token": token,
        "pageNum": page_num,
        "pageCount": page_count,
        "keyword": keyword,
    }
    if fleetid:
        payload["fleetid"] = fleetid

    j = vss_post_raw("/vss/vehicle/findAll.action", payload)
    status = j.get("status")
    if status == 10000:
        return j.get("data") or {}
    if status == 10025:
        return {"dataList": [], "totalCount": 0}
    raise RuntimeError(f"findAll failed status={status} msg={j.get('msg')} full={j}")


def list_all_fleets(token: str) -> list[dict]:
    """Get every fleet via /vss/fleet/findAll.action (pageNum=-1, pageCount=-1).

    Tries JSON first, then form-urlencoded (matches the bundled web client).
    Returns [] on 10025 ("no data") and on transport failures.
    """
    payloads = [
        {"token": token, "pageNum": -1, "pageCount": -1},
        {"token": token, "pageNum": "-1", "pageCount": "-1"},
        {"token": token},
    ]

    for payload in payloads:
        try:
            j = vss_post_raw("/vss/fleet/findAll.action", payload, timeout=60, max_attempts=3)
        except Exception:
            continue
        st = j.get("status") if isinstance(j, dict) else None
        if st == 10025:
            return []
        if st != 10000:
            continue
        data = j.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            dl = data.get("dataList") or data.get("list") or []
            if isinstance(dl, list):
                return dl

    try:
        base = active_base_url()
        url = f"{base}/vss/fleet/findAll.action"
        with _vss_api_lock:
            r = _session.post(
                url,
                data={"token": token, "pageNum": "-1", "pageCount": "-1"},
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=60,
                verify=_ssl_verify_for_url(base),
            )
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and j.get("status") == 10000:
            data = j.get("data")
            if isinstance(data, dict):
                dl = data.get("dataList") or []
                if isinstance(dl, list):
                    return dl
        if isinstance(j, dict) and j.get("status") == 10025:
            return []
    except Exception:
        pass

    return []


def _fleet_fields(f: dict) -> tuple[str, str]:
    fid = (
        f.get("fleetid")
        or f.get("fleetId")
        or f.get("id")
        or f.get("guid")
        or f.get("fleetGuid")
        or ""
    )
    name = (
        f.get("fleetname")
        or f.get("fleetName")
        or f.get("name")
        or ""
    )
    return str(fid), str(name)


def _fleet_parent_id(f: dict) -> str:
    v = (
        f.get("pid")
        or f.get("parentId")
        or f.get("parentid")
        or f.get("parentFleetId")
        or f.get("parentfleetid")
        or ""
    )
    return str(v).strip()


def _fleets_children_index(all_fleets: list[dict]) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """parent_fleet_id -> [(child_id, child_name), ...], and id -> name."""
    by_parent: dict[str, list[tuple[str, str]]] = {}
    id_to_name: dict[str, str] = {}
    for f in all_fleets:
        if not isinstance(f, dict):
            continue
        fid, name = _fleet_fields(f)
        if not fid:
            continue
        id_to_name[fid] = name or fid
        pid = _fleet_parent_id(f)
        by_parent.setdefault(pid, []).append((fid, name or fid))
    return by_parent, id_to_name


def expand_fleet_tree_from_root(token: str, root_id: str) -> dict[str, str]:
    """All fleet IDs under ``root_id`` (including the root), using ``pid`` links from ``findAll``."""
    root_id = (root_id or "").strip()
    if not root_id:
        return {}
    all_f = list_all_fleets(token)
    by_parent, id_to_name = _fleets_children_index(all_f)
    if root_id not in id_to_name:
        _vss_log.warning(
            "DHL_ROOT_FLEET_ID=%s… not found in /fleet/findAll — check the GUID or user scope",
            root_id[:12],
        )
        # Do not crawl a synthetic single-id tree: /vehicle/findAll for unknown fleet ids is empty
        # and would incorrectly fall through to global keyword paging in older logic.
        return {}
    out: dict[str, str] = {}
    queue = [root_id]
    seen: set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        out[cur] = id_to_name.get(cur, cur)
        for child_id, cname in by_parent.get(cur, []):
            if child_id not in seen:
                queue.append(child_id)
    return out


def explicit_dhl_fleets_from_env(token: str) -> dict[str, str]:
    """Optional env: ``DHL_ROOT_FLEET_ID`` (umbrella + descendants) or ``DHL_FLEET_IDS`` (comma-separated)."""
    root = _env("DHL_ROOT_FLEET_ID", "").strip()
    if root:
        m = expand_fleet_tree_from_root(token, root)
        if m:
            _vss_log.info(
                "Using DHL_ROOT_FLEET_ID: %s fleet(s) to crawl (umbrella tree)",
                len(m),
            )
        else:
            _vss_log.error(
                "DHL_ROOT_FLEET_ID is set but resolved 0 fleets — wrong GUID, or fleet/findAll does not "
                "return that id / pid tree for this user."
            )
        return m
    raw = _env("DHL_FLEET_IDS", "").strip()
    if not raw:
        return {}
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    if not ids:
        return {}
    id_to_name: dict[str, str] = {}
    for f in list_all_fleets(token):
        if not isinstance(f, dict):
            continue
        fid, name = _fleet_fields(f)
        if fid in ids:
            id_to_name[fid] = name or fid
    out = {i: id_to_name.get(i, i) for i in ids}
    _vss_log.info("Using DHL_FLEET_IDS: %s fleet(s) to crawl", len(out))
    return out


def _dhl_fleet_pairs(token: str, *, contains: str = "DHL") -> list[tuple[str, str]]:
    """Fleet id/name pairs whose name contains ``contains`` (no session retry wrapper)."""
    q = (contains or "").strip().upper()
    out: list[tuple[str, str]] = []
    for f in list_all_fleets(token):
        if not isinstance(f, dict):
            continue
        fid, name = _fleet_fields(f)
        if not fid:
            continue
        if q and q not in name.upper():
            continue
        out.append((fid, name))
    return out


def discover_dhl_fleets(*, contains: str = "DHL") -> list[tuple[str, str]]:
    """Return [(fleet_id, fleet_name)] for fleets whose name contains the keyword."""

    def _call(token: str) -> list[tuple[str, str]]:
        return _dhl_fleet_pairs(token, contains=contains)

    return _retry_on_session_expired(_call)


def discover_dhl_devices(
    *,
    page_size: int = 200,
    max_pages: int = 60,
    contains: str = "DHL",
    skip_fleet_discovery: bool = False,
    fleet_fetch_workers: int = 1,
) -> list[dict]:
    """All devices that belong to DHL fleets.

    Resolution order (first match wins):
      0) **Env** ``DHL_ROOT_FLEET_ID`` — expand the umbrella fleet and all descendants (``pid`` tree)
         from ``/vss/fleet/findAll``, then page ``/vehicle/findAll`` per fleet.
      1) **Env** ``DHL_FLEET_IDS`` — comma-separated fleet GUIDs (same per-fleet paging).
      2) List fleets whose **name** contains ``contains`` (default ``DHL``), then page per fleet.
      3) **Fallback:** keyword ``findAll`` scan on device names / fleet names — **skipped** when fleets
         came from ``DHL_ROOT_FLEET_ID`` / ``DHL_FLEET_IDS`` unless ``DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1``.

    ``skip_fleet_discovery=True`` (e.g. ``DHL_FAST_DEVICE_KEYWORD_ONLY``) uses the global keyword scan;
    it runs after env fleet resolution and bypasses the env-only error stop when you explicitly opt into keyword mode.
    """
    q = (contains or "").strip().upper()
    fleet_id_to_name: dict[str, str] = {}

    def _call(token: str) -> list[dict]:
        nonlocal fleet_id_to_name
        # True when .env asks for fleet-scoped discovery (even if VSS resolves 0 fleets).
        env_fleet_sources_requested = bool(
            _env("DHL_ROOT_FLEET_ID", "").strip() or _env("DHL_FLEET_IDS", "").strip()
        )
        configured = explicit_dhl_fleets_from_env(token)
        if configured:
            fleet_id_to_name = configured
        elif skip_fleet_discovery:
            _vss_log.info("discover_dhl_devices: keyword-only (skip fleet list + per-fleet crawl)")
            fleet_id_to_name = {}
        elif env_fleet_sources_requested:
            fleet_id_to_name = {}
            _vss_log.error(
                "DHL_ROOT_FLEET_ID / DHL_FLEET_IDS is set but VSS returned no fleet ids to crawl "
                "(root missing from /fleet/findAll, or DHL_FLEET_IDS did not match any fleet). "
                "Skipping global keyword device scan; fix the GUID(s) or set "
                "DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1 to opt in to keyword paging.",
            )
        else:
            fleet_pairs = _dhl_fleet_pairs(token, contains=contains)
            fleet_id_to_name = {fid: name for fid, name in fleet_pairs if fid}
            if not fleet_id_to_name:
                _vss_log.warning(
                    "No fleets matched name %r in fleet/findAll — falling back to keyword device scan. "
                    "Set DHL_ROOT_FLEET_ID or DHL_FLEET_IDS in .env to crawl by fleet id instead.",
                    contains,
                )

        def _fetch_one(fid: str) -> list[dict]:
            out: list[dict] = []
            for page in range(1, max_pages + 1):
                try:
                    d = list_devices_page(token, page, page_size, fleetid=fid) or {}
                except Exception:
                    return out
                page_rows = d.get("dataList") or []
                if not page_rows:
                    break
                for r in page_rows:
                    if not isinstance(r, dict):
                        continue
                    if not r.get("fleetid"):
                        r["fleetid"] = fid
                    if not r.get("fleetName"):
                        r["fleetName"] = fleet_id_to_name.get(fid, "")
                    out.append(r)
            return out

        rows: list[dict] = []
        if fleet_id_to_name:
            w = max(1, min(fleet_fetch_workers, 1))
            with ThreadPoolExecutor(max_workers=w) as ex:
                for batch in ex.map(_fetch_one, list(fleet_id_to_name.keys())):
                    rows.extend(batch)

        if rows:
            by_id: dict[str, dict] = {}
            for r in rows:
                did = str(r.get("deviceno") or "")
                if did and did != "None":
                    by_id[did] = r
            return list(by_id.values())

        allow_kw = _env("DHL_ALLOW_KEYWORD_DEVICE_FALLBACK", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        # Empty ``fleet_id_to_name`` must still suppress keyword scan when .env asked for fleet ids
        # (unless keyword-only mode explicitly requested via ``skip_fleet_discovery``).
        if env_fleet_sources_requested and not allow_kw and not skip_fleet_discovery:
            if fleet_id_to_name:
                _vss_log.warning(
                    "discover_dhl_devices: 0 devices after paging /vehicle/findAll for %s fleet(s) from "
                    "DHL_ROOT_FLEET_ID / DHL_FLEET_IDS — skipping keyword scan. "
                    "Fix token/VSS access or set DHL_ALLOW_KEYWORD_DEVICE_FALLBACK=1 to force the old keyword crawl.",
                    len(fleet_id_to_name),
                )
            return []

        # Fallback: keyword scan on devices, match against deviceName too.
        seen: dict[str, dict] = {}
        for page in range(1, max_pages + 1):
            if page == 1 or page % 2 == 0 or page == max_pages:
                _vss_log.info(
                    "discover_dhl_devices: keyword scan page %s/%s (%s devices so far)",
                    page,
                    max_pages,
                    len(seen),
                )
            d = list_devices_page(token, page, page_size, keyword=contains) or {}
            page_rows = d.get("dataList") or []
            if not page_rows:
                break
            for r in page_rows:
                if not isinstance(r, dict):
                    continue
                fname = str(r.get("fleetName") or "")
                dname = str(r.get("devicename") or r.get("deviceName") or "")
                if q and (q in fname.upper() or q in dname.upper()):
                    fid = str(r.get("fleetid") or "")
                    if not r.get("fleetName") and fid in fleet_id_to_name:
                        r["fleetName"] = fleet_id_to_name[fid]
                    did = str(r.get("deviceno") or "")
                    if did and did != "None":
                        seen[did] = r
        return list(seen.values())

    def _run() -> list[dict]:
        return _retry_on_session_expired(_call)

    with _discover_lock:
        return _run()


def current_gps_and_status(token: str, device_ids: list[str] | str) -> list[dict]:
    if isinstance(device_ids, list):
        device_ids = ",".join(device_ids)
    j = vss_post(
        "/vss/vehicle/getDeviceStatus.action",
        {"token": token, "deviceID": device_ids},
    )
    data = j.get("data")
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        lst = data.get("dataList") or data.get("list") or data.get("rows")
        if isinstance(lst, list):
            return [r for r in lst if isinstance(r, dict)]
        if isinstance(data, dict) and (
            "deviceno" in data or "deviceguid" in data or "deviceID" in data or "deviceid" in data
        ):
            return [data]
    return []


def realtime_status_for_devices(
    device_ids: list[str],
    *,
    batch: int = 20,
    sleep_s: float = 0.0,
    max_workers: int = 1,
) -> list[dict]:
    """Pull realtime status for many devices, parallel batches, returning a flat list."""
    chunks = [device_ids[i : i + batch] for i in range(0, len(device_ids), batch)]

    def _call(token: str) -> list[dict]:
        def _fetch(chunk: list[str]) -> list[dict]:
            try:
                return current_gps_and_status(token, chunk)
            except RuntimeError as e:
                msg = str(e)
                if "10023" in msg or "session has expired" in msg.lower():
                    raise
                _vss_log.warning("getDeviceStatus failed (batch size %s): %s", len(chunk), e)
                return []
            except Exception as e:  # noqa: BLE001
                _vss_log.warning("getDeviceStatus failed (batch size %s): %s", len(chunk), e)
                return []

        out: list[dict] = []
        workers = max(1, min(max_workers, 1))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rows in ex.map(_fetch, chunks):
                out.extend(rows)
        if sleep_s:
            time.sleep(sleep_s)
        return out

    return _retry_on_session_expired(_call)


def _alarm_history_path() -> str:
    """VSS Web API V2.8 documents ``/vss/alarm/apiFindAllByTime.action`` (§3.9).

    Some deployments still accept the legacy ``findAllByTime.action``. Override with
    ``VSS_ALARM_FIND_PATH`` if needed.
    """
    # Default to legacy URL — most live VSS builds match the dashboard’s prior behaviour.
    # Set ``/vss/alarm/apiFindAllByTime.action`` for strict V2.8 manual alignment.
    p = _env("VSS_ALARM_FIND_PATH", "/vss/alarm/findAllByTime.action").strip()
    if not p:
        return "/vss/alarm/findAllByTime.action"
    return p if p.startswith("/") else f"/{p}"


def _alarms_one_request(token: str, payload: dict, *, path: str | None = None) -> dict:
    """Paged alarm history call with 10129 backoff."""
    url = path or _alarm_history_path()
    started = time.time()
    delay_s = 1.5
    while True:
        j = vss_post_raw(url, payload, timeout=90, max_attempts=4)
        st = j.get("status") if isinstance(j, dict) else None
        if st == 10000:
            return j.get("data") or {}
        if st == 10025:
            return {"dataList": [], "totalCount": 0}
        if st == 10129:
            if time.time() - started > 180:
                raise RuntimeError(f"alarms rate-limited (10129) too long: {j}")
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.7, 30.0)
            continue
        raise RuntimeError(f"alarm history failed ({url}) status={st} msg={j.get('msg') if isinstance(j, dict) else j}")


def _alarms_paged_for_device_batch(
    token: str,
    *,
    device_ids: list[str],
    begin_time: str,
    end_time: str,
    alarm_type_csv: str,
    page_count: int,
    max_pages: int,
) -> list[dict]:
    rows: list[dict] = []
    device_csv = ",".join(str(d) for d in device_ids if d)
    path = _alarm_history_path()
    use_api_shape = "apiFindAllByTime" in path
    for page in range(1, max_pages + 1):
        if use_api_shape:
            # Howen VSS Web API V2.8 §3.9 (application/json or form post)
            payload = {
                "token": token,
                "pageNum": page,
                "pageCount": page_count,
                "deviceID": device_csv,
                "beginTime": begin_time,
                "endTime": end_time,
                "alarmType": alarm_type_csv or "",
            }
        else:
            payload = {
                "token": token,
                "beginTime": begin_time,
                "endTime": end_time,
                "pageNum": page,
                "pageCount": page_count,
                "keyword": "",
                "alarmType": alarm_type_csv,
                "fleetIdList": "",
                "deviceGuid": "",
                "deviceID": device_csv,
            }
        data = _alarms_one_request(token, payload, path=path)
        page_rows = data.get("dataList") or []
        if not page_rows:
            return rows
        rows.extend(page_rows)
        total = data.get("totalCount")
        if isinstance(total, int) and len(rows) >= total:
            return rows
    return rows


def alarms_find_all_by_time_for_devices(
    *,
    begin_dt: datetime,
    end_dt: datetime,
    device_ids: list[str],
    alarm_type_csv: str = "",
    page_count: int = 500,
    max_pages: int = 30,
    batch_size: int = 50,
    max_workers: int = 6,
) -> list[dict]:
    """All alarms in [begin_dt, end_dt] for the given DHL device IDs.

    Uses the documented alarm-by-page API (default ``apiFindAllByTime``). Device IDs are
    queried in batches to avoid huge tenant-wide pulls. Set ``VSS_ALARM_FIND_PATH`` to
    ``/vss/alarm/findAllByTime.action`` only if your server requires the legacy URL.
    """
    begin_time = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    clean_ids = [str(d).strip() for d in device_ids if str(d).strip()]
    if not clean_ids:
        return []

    batches = [clean_ids[i : i + batch_size] for i in range(0, len(clean_ids), batch_size)]

    def _call(token: str) -> list[dict]:
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(
                    _alarms_paged_for_device_batch,
                    token,
                    device_ids=batch,
                    begin_time=begin_time,
                    end_time=end_time,
                    alarm_type_csv=alarm_type_csv,
                    page_count=page_count,
                    max_pages=max_pages,
                )
                for batch in batches
            ]
            for fut in as_completed(futures):
                try:
                    results.extend(fut.result())
                except RuntimeError as e:
                    msg = str(e)
                    if "10023" in msg or "session has expired" in msg.lower():
                        raise
                    _vss_log.warning("alarm history batch failed: %s", e)
                except Exception as e:  # noqa: BLE001
                    _vss_log.warning("alarm history batch failed: %s", e)
        return results

    return _retry_on_session_expired(_call)


def alarms_find_all_by_time(
    *,
    begin_dt: datetime,
    end_dt: datetime,
    fleet_ids: list[str],
    alarm_type_csv: str = "",
    page_count: int = 500,
    max_pages: int = 200,
) -> list[dict]:
    """Compatibility wrapper kept for legacy callers — fleetIdList may be ignored."""
    begin_time = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_time = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    fleet_csv = fleet_id_csv(fleet_ids)
    # V2.8 ``apiFindAllByTime`` is device-scoped; fleet filtering stays on the legacy URL.
    path = "/vss/alarm/findAllByTime.action"

    def _call(token: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            payload = {
                "token": token,
                "beginTime": begin_time,
                "endTime": end_time,
                "pageNum": page,
                "pageCount": page_count,
                "keyword": "",
                "alarmType": alarm_type_csv,
                "fleetIdList": fleet_csv,
                "deviceGuid": "",
                "deviceID": "",
            }
            data = _alarms_one_request(token, payload, path=path)
            page_rows = data.get("dataList") or []
            if not page_rows:
                return rows
            rows.extend(page_rows)
            total = data.get("totalCount")
            if isinstance(total, int) and len(rows) >= total:
                return rows
        return rows

    return _retry_on_session_expired(_call)


def get_lang_dict(lang: str = "en") -> dict:
    j = vss_post_raw(
        "/vss/lang/findLangDict.action",
        {"terminal": 2, "lang": lang},
        timeout=60,
        max_attempts=3,
    )
    if isinstance(j, dict) and isinstance(j.get("data"), dict):
        return j["data"]
    return j if isinstance(j, dict) else {}

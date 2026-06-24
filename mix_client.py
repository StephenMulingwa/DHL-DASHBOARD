"""MiX Telematics API client (positions + asset metadata).

Credentials: ``MIX_ACCOUNTS_JSON`` env var (preferred), ``accounts.json`` file
(see ``accounts.json.example``), or inline ``MIX_*`` env vars. Enable with ``MIX_ENABLED=1``.

South Africa (``mix_za``) is the default server key. Set ``MIX_GROUP_IDS`` for
explicit groups, ``MIX_GROUP_NAME_CONTAINS`` to match organisation names, or
``MIX_FETCH_ALL_GROUPS=1`` to pull every accessible organisation group.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)
_REPO_DIR = Path(__file__).resolve().parent
_session = requests.Session()

_lock = threading.Lock()
_bearer_token: str | None = None
_token_expires_at: float = 0.0
_org_groups_cache: list[dict[str, Any]] | None = None
_org_groups_cache_at: float = 0.0
_rate_lock = threading.Lock()
_rate_times: deque[float] = deque(maxlen=25)

_MIX_COLUMNS = [
    "GroupId",
    "GroupName",
    "AssetId",
    "AssetName",
    "Registration",
    "Make",
    "DriverId",
    "Latitude",
    "Longitude",
    "SpeedKmh",
    "Rpm",
    "Heading",
    "AltitudeM",
    "Address",
    "GpsSource",
    "Satellites",
    "EventTime",
    "AgeHours",
    "LastUpdated",
]

_DEFAULT_SERVER_KEY = "mix_za"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_truthy(name: str, default: str = "0") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def _server_key() -> str:
    return _env("MIX_SERVER_KEY", _DEFAULT_SERVER_KEY)


def _resolve_accounts_path() -> Path:
    raw = _env("MIX_ACCOUNTS_JSON_PATH")
    if raw:
        p = Path(os.path.expandvars(raw))
        if not p.is_absolute():
            p = (_REPO_DIR / p).resolve()
        return p
    return _REPO_DIR / "accounts.json"


def _inline_creds_complete() -> bool:
    keys = (
        "MIX_API_URL",
        "MIX_IDENTITY_URL",
        "MIX_CLIENT_ID",
        "MIX_CLIENT_SECRET",
        "MIX_USERNAME",
        "MIX_PASSWORD",
    )
    return all(_env(k) for k in keys)


def _load_accounts_blob() -> dict[str, Any]:
    """Load MiX credentials from MIX_ACCOUNTS_JSON env, file path, or inline MIX_* vars."""
    raw_json = _env("MIX_ACCOUNTS_JSON")
    if raw_json:
        try:
            blob = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MIX_ACCOUNTS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise ValueError("MIX_ACCOUNTS_JSON must be a JSON object keyed by server name")
        return blob

    path = _resolve_accounts_path()
    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            blob = json.load(fh)
        if not isinstance(blob, dict):
            raise ValueError(f"{path} must contain a JSON object keyed by server name")
        return blob

    raise FileNotFoundError(
        f"MiX credentials not found. Set MIX_ACCOUNTS_JSON in .env, "
        f"copy accounts.json.example to {path.name}, or set MIX_* env vars."
    )


def mix_enabled() -> bool:
    if _env_truthy("MIX_DISABLED"):
        return False
    if _env_truthy("MIX_ENABLED"):
        return True
    if _env("MIX_ACCOUNTS_JSON"):
        return True
    path = _resolve_accounts_path()
    return path.is_file() or _inline_creds_complete()


def mix_config_summary() -> str:
    if not mix_enabled():
        return "disabled"
    if _inline_creds_complete():
        return f"inline env ({_env('MIX_API_URL')})"
    if _env("MIX_ACCOUNTS_JSON"):
        return f"MIX_ACCOUNTS_JSON [{_server_key()}]"
    path = _resolve_accounts_path()
    return f"{path.name} [{_server_key()}]"


def _load_server_creds() -> dict[str, str]:
    if _inline_creds_complete():
        return {
            "ApiUrl": _env("MIX_API_URL"),
            "IdentityUrl": _env("MIX_IDENTITY_URL"),
            "IdentityClientId": _env("MIX_CLIENT_ID"),
            "IdentityClientSecret": _env("MIX_CLIENT_SECRET"),
            "IdentityUsername": _env("MIX_USERNAME"),
            "IdentityPassword": _env("MIX_PASSWORD"),
            "IdentityScope": _env("MIX_SCOPE", "openid profile offline_access"),
        }
    server_key = _server_key()
    all_creds = _load_accounts_blob()
    if server_key not in all_creds:
        raise KeyError(
            f"Key '{server_key}' not in MiX accounts. Available: {list(all_creds.keys())}"
        )
    return all_creds[server_key]


def api_base_url() -> str:
    return _load_server_creds()["ApiUrl"].rstrip("/")


def ensure_bearer_token() -> str:
    """Return a cached MiX bearer token, refreshing when near expiry."""
    global _bearer_token, _token_expires_at
    with _lock:
        if _bearer_token and time.time() < _token_expires_at - 60:
            return _bearer_token

    creds = _load_server_creds()
    token_url = f"{creds['IdentityUrl'].rstrip('/')}/core/connect/token"
    scope = creds.get("IdentityScope", "").replace("+", " ")
    if not scope:
        scope = "openid profile offline_access MiX.Integrate"
    payload = {
        "grant_type": "password",
        "client_id": creds["IdentityClientId"],
        "client_secret": creds["IdentityClientSecret"],
        "username": creds["IdentityUsername"],
        "password": creds["IdentityPassword"],
        "scope": scope,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    log.info("MiX: requesting bearer token from %s (user=%s)", token_url, creds["IdentityUsername"])
    resp = _session.post(token_url, data=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        detail = _mix_token_error_detail(resp)
        raise RuntimeError(
            f"MiX token request failed ({resp.status_code}): {detail}. "
            f"Check IdentityUsername/IdentityPassword in MIX_ACCOUNTS_JSON for {_server_key()}."
        )
    data = resp.json()
    token = str(data["access_token"])
    expires_in = int(data.get("expires_in", 3600))
    with _lock:
        _bearer_token = token
        _token_expires_at = time.time() + expires_in
    log.info("MiX: token acquired (valid ~%s min)", expires_in // 60)
    return token


def _mix_token_error_detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            parts = [str(body.get(k)) for k in ("error", "error_description", "message") if body.get(k)]
            if parts:
                return " — ".join(parts)
    except Exception:
        pass
    text = (resp.text or "").strip()
    if text.startswith("<!DOCTYPE") or text.startswith("<html"):
        return "Unauthorized (invalid MiX username, password, or client credentials)"
    return text[:200]


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_organisation_groups(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """List organisation groups visible to the authenticated MiX user."""
    global _org_groups_cache, _org_groups_cache_at
    if (
        not force_refresh
        and _org_groups_cache is not None
        and time.time() - _org_groups_cache_at < 3600
    ):
        return list(_org_groups_cache)

    token = ensure_bearer_token()
    api = api_base_url()
    url = f"{api}/api/organisationgroups"
    resp = _mix_http("get", url, headers=_api_headers(token), timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"MiX organisation groups failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"MiX organisation groups unexpected response: {type(data)}")
    with _lock:
        _org_groups_cache = data
        _org_groups_cache_at = time.time()
    log.info("MiX: %s organisation groups loaded", len(data))
    return data


def resolve_group_targets() -> list[dict[str, Any]]:
    """Return ``[{GroupId, Name}, ...]`` based on env configuration."""
    explicit = _env("MIX_GROUP_IDS")
    if explicit:
        names = {g["GroupId"]: g.get("Name", "") for g in fetch_organisation_groups()}
        out: list[dict[str, Any]] = []
        for part in explicit.split(","):
            part = part.strip()
            if not part:
                continue
            gid = int(part)
            out.append({"GroupId": gid, "Name": names.get(gid, str(gid))})
        if not out:
            raise ValueError("MIX_GROUP_IDS is empty")
        return out

    groups = fetch_organisation_groups()
    needle = _env("MIX_GROUP_NAME_CONTAINS")
    if needle:
        groups = [g for g in groups if needle.lower() in str(g.get("Name", "")).lower()]
        if not groups:
            raise RuntimeError(f"No MiX groups match MIX_GROUP_NAME_CONTAINS={needle!r}")

    if _env_truthy("MIX_FETCH_ALL_GROUPS") or not needle:
        if not _env_truthy("MIX_FETCH_ALL_GROUPS") and not needle:
            raise RuntimeError(
                "Set MIX_GROUP_IDS, MIX_GROUP_NAME_CONTAINS, or MIX_FETCH_ALL_GROUPS=1 in .env"
            )
        return [{"GroupId": g["GroupId"], "Name": g.get("Name", "")} for g in groups]

    return [{"GroupId": g["GroupId"], "Name": g.get("Name", "")} for g in groups]


def group_ids() -> list[int]:
    return [int(g["GroupId"]) for g in resolve_group_targets()]


def resolve_organisation_id() -> int:
    """MiX library events require an OrganisationGroup id, not a site/group id from MIX_GROUP_IDS."""
    explicit = (_env("MIX_ORGANISATION_ID") or "").strip()
    if explicit:
        return int(explicit)
    groups = fetch_organisation_groups()
    configured = set(group_ids())
    for g in groups:
        if int(g.get("GroupId", 0)) in configured and g.get("Type") == "OrganisationGroup":
            return int(g["GroupId"])
    for g in groups:
        if g.get("Type") == "OrganisationGroup":
            return int(g["GroupId"])
    for g in groups:
        if g.get("Type") in ("OrganisationSubGroup", "SiteGroup", "DefaultSite"):
            continue
        if g.get("Type") in ("MultiLevelOrg", "RsoGroup", "DealerGroup"):
            return int(g["GroupId"])
    return group_ids()[0]


def _safe_get(record: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        if k in record and record[k] is not None:
            return str(record[k])
    return default


def _throttle_mix_api() -> None:
    """MiX ZA allows ~20 API calls/min — stay under that when scanning many groups."""
    try:
        max_per_min = int(_env("MIX_MAX_CALLS_PER_MINUTE", "15") or "15")
    except ValueError:
        max_per_min = 15
    max_per_min = max(5, min(max_per_min, 19))
    with _rate_lock:
        now = time.time()
        while _rate_times and now - _rate_times[0] > 60.0:
            _rate_times.popleft()
        if len(_rate_times) >= max_per_min:
            wait = 60.0 - (now - _rate_times[0]) + 0.25
            if wait > 0:
                time.sleep(wait)
        _rate_times.append(time.time())


def _mix_http(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 90,
    **kwargs: Any,
) -> requests.Response:
    """MiX API call with client-side throttle and 429 backoff."""
    last: requests.Response | None = None
    for attempt in range(4):
        _throttle_mix_api()
        resp = getattr(_session, method.lower())(url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code != 429:
            return resp
        last = resp
        with _rate_lock:
            oldest = _rate_times[0] if _rate_times else time.time()
        wait = max(5.0, 61.0 - (time.time() - oldest))
        log.warning(
            "MiX rate limited (429) on %s; retry in %.0fs (attempt %s/4)",
            url.split("/api/", 1)[-1][:60],
            wait,
            attempt + 1,
        )
        time.sleep(wait)
    if last is None:
        raise RuntimeError("MiX request failed before any response")
    return last


def _post_positions_for_groups(
    group_ids_batch: list[int],
    *,
    quantity: int,
    cached_since: str | None,
    ensure_reverse_geocoded: bool,
    token: str,
    api_url: str,
) -> list[dict[str, Any]]:
    url = f"{api_url}/api/positions/groups/latest/{quantity}"
    params: dict[str, str] = {}
    if cached_since:
        params["cachedSince"] = cached_since
    params["ensureReverseGeocoded"] = str(ensure_reverse_geocoded).lower()
    resp = _mix_http(
        "post",
        url,
        headers=_api_headers(token),
        params=params,
        json=group_ids_batch,
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MiX positions failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"MiX positions unexpected response: {type(data)}")
    return data


def fetch_latest_positions(
    *,
    quantity: int | None = None,
    cached_since: str | None = None,
    ensure_reverse_geocoded: bool | None = None,
) -> list[dict[str, Any]]:
    creds = _load_server_creds()
    api_url = creds["ApiUrl"].rstrip("/")
    token = ensure_bearer_token()
    qty = quantity if quantity is not None else int(_env("MIX_QUANTITY", "1") or "1")
    if ensure_reverse_geocoded is None:
        ensure_reverse_geocoded = _env_truthy("MIX_ENSURE_REVERSE_GEOCODED", "1")
    if cached_since is None:
        cached_since = _env("MIX_CACHED_SINCE") or None

    targets = resolve_group_targets()
    log.info("MiX: fetching positions for %s group(s)", len(targets))

    # One group per request is most reliable across MiX ZA tenants.
    def _one_group(target: dict[str, Any]) -> list[dict[str, Any]]:
        gid = int(target["GroupId"])
        try:
            rows = _post_positions_for_groups(
                [gid],
                quantity=qty,
                cached_since=cached_since,
                ensure_reverse_geocoded=ensure_reverse_geocoded,
                token=token,
                api_url=api_url,
            )
        except Exception as e:
            log.warning("MiX: skip group %s (%s): %s", gid, target.get("Name"), e)
            return []
        for row in rows:
            row.setdefault("GroupId", gid)
            row.setdefault("GroupName", target.get("Name", ""))
        return rows

    workers = max(1, min(int(_env("MIX_GROUP_WORKERS", "2") or "2"), 4))
    if len(targets) == 1 or workers == 1:
        out: list[dict[str, Any]] = []
        for t in targets:
            out.extend(_one_group(t))
        return out

    merged: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one_group, t): t for t in targets}
        for fut in as_completed(futs):
            merged.extend(fut.result())
    log.info("MiX: %s position record(s) across %s groups", len(merged), len(targets))
    return merged


def fetch_assets_for_group(api_url: str, token: str, group_id: int) -> dict[str, dict[str, Any]]:
    """Probe known MiX asset endpoints; return AssetId -> record map."""
    base = api_url.rstrip("/")
    headers = _api_headers(token)
    candidates = [
        ("GET", f"{base}/api/assets/groups/{group_id}"),
        ("GET", f"{base}/api/assets/group/{group_id}"),
        ("POST", f"{base}/api/assets/groups"),
        ("GET", f"{base}/api/assets?groupId={group_id}"),
        ("GET", f"{base}/api/v1/assets/groups/{group_id}"),
        ("GET", f"{base}/api/groups/{group_id}/assets"),
    ]
    for method, url in candidates:
        try:
            if method == "GET":
                resp = _session.get(url, headers=headers, timeout=30)
            else:
                resp = _session.post(url, headers=headers, json=[group_id], timeout=30)
        except requests.RequestException as e:
            log.debug("MiX assets %s %s: %s", method, url, e)
            continue
        if resp.status_code != 200:
            continue
        assets = resp.json()
        if not isinstance(assets, list) or not assets:
            continue
        log.info("MiX: assets via %s %s (%s rows)", method, url, len(assets))
        out: dict[str, dict[str, Any]] = {}
        for a in assets:
            aid = a.get("AssetId", a.get("assetId"))
            if aid is not None:
                out[str(aid)] = a
        return out
    log.debug("MiX: no assets endpoint for group %s", group_id)
    return {}


def _tacho_speed_line() -> str:
    return _env("MIX_TACHO_SPEED_LINE", "F1") or "F1"


def _tacho_rpm_line() -> str:
    return _env("MIX_TACHO_RPM_LINE", "F2") or "F2"


def _tacho_minutes() -> int:
    try:
        return max(1, min(int(_env("MIX_TACHO_MINUTES", "59") or "59"), 59))
    except ValueError:
        return 59


def _tacho_key_for_line(definitions: list[dict[str, Any]], line_name: str) -> int | None:
    for item in definitions:
        if str(item.get("LineName", "")).strip() == line_name:
            key = item.get("Key")
            if key is not None:
                return int(key)
    return None


def _tacho_value_at_key(interval: dict[str, Any], key: int | None) -> float | None:
    if key is None:
        return None
    for item in interval.get("Data") or []:
        if item.get("Key") == key:
            try:
                return float(item.get("Value"))
            except (TypeError, ValueError):
                return None
    return None


def fetch_asset_tacho(
    api_url: str,
    token: str,
    asset_id: int,
    *,
    minutes: int | None = None,
) -> dict[str, Any] | None:
    """Fetch tacho intervals for one asset (MiX allows up to 1 hour per request)."""
    window = minutes if minutes is not None else _tacho_minutes()
    to_dt = datetime.now(timezone.utc)
    fr_dt = to_dt - timedelta(minutes=max(1, min(window, 59)))
    fr = fr_dt.strftime("%Y%m%d%H%M%S")
    to = to_dt.strftime("%Y%m%d%H%M%S")
    url = f"{api_url.rstrip('/')}/api/tachos/asset/{asset_id}/range/from/{fr}/to/{to}"
    _throttle_mix_api()
    try:
        resp = _session.get(url, headers=_api_headers(token), timeout=45)
    except requests.RequestException as e:
        log.debug("MiX tacho asset %s: %s", asset_id, e)
        return None
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        log.debug("MiX tacho asset %s failed: %s", asset_id, resp.status_code)
        return None
    data = resp.json()
    return data if isinstance(data, dict) else None


def analyze_tacho(tacho: dict[str, Any] | None) -> dict[str, Any]:
    """Parse tacho intervals: F1 = speed (km/h), F2 = RPM.

    ``has_rpm_feed`` / ``has_speed_feed`` mean the tacho channel exists and returned
    readings (including 0 when the engine is off). Use those flags for health checks,
    not whether the latest RPM is > 0.
    """
    out: dict[str, Any] = {
        "has_speed_feed": False,
        "has_rpm_feed": False,
        "speed_kmh": None,
        "rpm": None,
        "max_speed_kmh": None,
        "speed_jump_kmh": None,
        "max_rpm": None,
        "rpm_std": None,
        "interval_count": 0,
        "interval_time": "",
    }
    if not tacho:
        return out

    definitions = tacho.get("ParameterDefinitions") or []
    speed_key = _tacho_key_for_line(definitions, _tacho_speed_line())
    rpm_key = _tacho_key_for_line(definitions, _tacho_rpm_line())
    intervals = tacho.get("Intervals") or []
    out["interval_count"] = len(intervals)
    if not intervals:
        return out

    speeds: list[float] = []
    rpms: list[float] = []
    for interval in intervals:
        if speed_key is not None:
            speed = _tacho_value_at_key(interval, speed_key)
            if speed is not None and speed >= 0:
                speeds.append(speed)
        if rpm_key is not None:
            rpm = _tacho_value_at_key(interval, rpm_key)
            if rpm is not None and rpm >= 0:
                rpms.append(rpm)

    latest = intervals[-1]
    out["interval_time"] = str(latest.get("IntervalDateTime") or "")

    if speeds:
        out["has_speed_feed"] = True
        out["speed_kmh"] = speeds[-1]
        out["max_speed_kmh"] = max(speeds)
        if len(speeds) > 1:
            jump = 0.0
            for a, b in zip(speeds, speeds[1:]):
                jump = max(jump, abs(a - b))
            out["speed_jump_kmh"] = jump

    if rpms:
        out["has_rpm_feed"] = True
        out["rpm"] = rpms[-1]
        out["max_rpm"] = max(rpms)
        active = [r for r in rpms if r > 0]
        if len(active) > 1:
            out["rpm_std"] = float(pd.Series(active).std(ddof=0))
        elif len(active) == 1:
            out["rpm_std"] = 0.0

    return out


def parse_tacho_snapshot(tacho: dict[str, Any] | None) -> dict[str, Any]:
    """Latest tacho speed (F1) and RPM (F2) from the most recent interval."""
    a = analyze_tacho(tacho)
    return {
        "speed_kmh": a["speed_kmh"],
        "rpm": a["rpm"],
        "interval_time": a["interval_time"],
        "interval_count": a["interval_count"],
        "has_speed_feed": a["has_speed_feed"],
        "has_rpm_feed": a["has_rpm_feed"],
    }


def tacho_interval_stats(
    tacho: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Return (latest_speed, max_speed, speed_jump, max_rpm, rpm_std) from tacho intervals."""
    a = analyze_tacho(tacho)
    return (
        a["speed_kmh"],
        a["max_speed_kmh"],
        a["speed_jump_kmh"],
        a["max_rpm"],
        a["rpm_std"],
    )


_tacho_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_tacho_cache_lock = threading.Lock()


def clear_tacho_cache() -> None:
    """Drop in-process tacho responses (call on dashboard refresh)."""
    with _tacho_cache_lock:
        _tacho_cache.clear()


def _tacho_cache_ttl_sec() -> int:
    try:
        return max(60, int(_env("MIX_TACHO_CACHE_SEC", "300") or "300"))
    except ValueError:
        return 300


def fetch_tacho_for_assets(
    api_url: str,
    token: str,
    asset_ids: list[int],
    *,
    minutes: int | None = None,
    force: bool = False,
) -> dict[str, dict[str, Any] | None]:
    """Fetch tacho payloads for many assets; reuse a short-lived in-process cache."""
    if not asset_ids:
        return {}

    ttl = _tacho_cache_ttl_sec()
    now = time.time()
    out: dict[str, dict[str, Any] | None] = {}
    missing: list[int] = []

    with _tacho_cache_lock:
        for aid in asset_ids:
            key = str(aid)
            if not force:
                cached = _tacho_cache.get(key)
                if cached and now - cached[0] < ttl:
                    out[key] = cached[1]
                    continue
            missing.append(aid)

    for aid in missing:
        tacho = fetch_asset_tacho(api_url, token, aid, minutes=minutes)
        key = str(aid)
        out[key] = tacho
        with _tacho_cache_lock:
            _tacho_cache[key] = (time.time(), tacho)

    return out


def enrich_dataframe_with_tacho(
    df: pd.DataFrame,
    api_url: str,
    token: str,
    *,
    asset_ids: list[int] | None = None,
) -> pd.DataFrame:
    """Overlay SpeedKmh and Rpm from tacho data (F1/F2) for each asset."""
    if df is None or df.empty or not _env_truthy("MIX_TACHO_ENRICH_POSITIONS", "1"):
        return df

    out = df.copy()
    if "Rpm" not in out.columns:
        out["Rpm"] = ""

    ids = asset_ids
    if ids is None:
        ids = []
        for raw in out["AssetId"].astype(str):
            try:
                ids.append(int(raw))
            except ValueError:
                continue

    raw_by_asset = fetch_tacho_for_assets(api_url, token, ids)
    snapshots = {key: analyze_tacho(raw) for key, raw in raw_by_asset.items()}

    filled_speed = 0
    filled_rpm = 0
    for idx, row in out.iterrows():
        snap = snapshots.get(str(row.get("AssetId", "")).strip(), {})
        speed = snap.get("speed_kmh")
        rpm = snap.get("rpm")
        if speed is not None:
            out.at[idx, "SpeedKmh"] = str(speed)
            filled_speed += 1
        if snap.get("has_rpm_feed"):
            val = rpm if rpm is not None else 0
            out.at[idx, "Rpm"] = str(int(val) if val == int(val) else val)
            filled_rpm += 1

    log.info(
        "MiX: tacho enrichment — %s assets, %s with speed, %s with RPM",
        len(ids),
        filled_speed,
        filled_rpm,
    )
    return out


def _parse_event_age_hours(ts: str) -> float | None:
    if not ts or not str(ts).strip():
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        except ValueError:
            continue
    return None


def positions_to_dataframe(
    raw: list[dict[str, Any]],
    asset_lookup: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    asset_lookup = asset_lookup or {}
    tz = timezone(timedelta(hours=int(_env("MIX_DISPLAY_TZ_OFFSET_HOURS", "3") or "3")))
    run_ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    records: list[dict[str, Any]] = []

    for item in raw:
        asset_id = _safe_get(item, "AssetId", "assetId")
        info = asset_lookup.get(asset_id, {})
        event_time = _safe_get(item, "Timestamp", "EventTime")
        records.append(
            {
                "GroupId": _safe_get(item, "GroupId", "groupId"),
                "GroupName": _safe_get(item, "GroupName", "groupName"),
                "AssetId": asset_id,
                "AssetName": _safe_get(info, "Description", "description"),
                "Registration": _safe_get(info, "RegistrationNumber", "registrationNumber"),
                "Make": _safe_get(info, "Make", "make"),
                "DriverId": _safe_get(item, "DriverId", "driverId"),
                "Latitude": _safe_get(item, "Latitude", "latitude"),
                "Longitude": _safe_get(item, "Longitude", "longitude"),
                "SpeedKmh": _safe_get(item, "SpeedKilometresPerHour", "speedKilometresPerHour"),
                "Rpm": "",
                "Heading": _safe_get(item, "Heading", "heading"),
                "AltitudeM": _safe_get(item, "AltitudeMetres", "altitudeMetres"),
                "Address": _safe_get(item, "FormattedAddress", "formattedAddress"),
                "GpsSource": _safe_get(item, "Source", "source"),
                "Satellites": _safe_get(item, "NumberOfSatellites", "numberOfSatellites"),
                "EventTime": event_time,
                "AgeHours": _parse_event_age_hours(event_time),
                "LastUpdated": run_ts,
            }
        )

    if not records:
        return empty_positions_dataframe()
    df = pd.DataFrame(records)
    for col in _MIX_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[_MIX_COLUMNS]


def empty_positions_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_MIX_COLUMNS)


def _build_asset_lookup(api_url: str, token: str, gids: list[int] | None = None) -> dict[str, dict[str, Any]]:
    """AssetId (str) -> asset record from ``/api/assets/group/{id}``."""
    lookup: dict[str, dict[str, Any]] = {}
    for gid in gids or group_ids():
        for a in _fetch_group_assets_list(api_url, token, gid):
            aid = a.get("AssetId", a.get("assetId"))
            if aid is not None:
                lookup[str(aid)] = a
    return lookup


def enrich_positions_dataframe(df: pd.DataFrame, asset_lookup: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Fill AssetName / Registration / Make from asset metadata when missing."""
    if df is None or df.empty or not asset_lookup:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        info = asset_lookup.get(str(row.get("AssetId", "")).strip(), {})
        if not info:
            continue
        if not str(row.get("AssetName", "")).strip():
            out.at[idx, "AssetName"] = _safe_get(info, "Description", "description")
        if not str(row.get("Registration", "")).strip():
            out.at[idx, "Registration"] = _safe_get(
                info, "RegistrationNumber", "registrationNumber", "Registration", "registration"
            )
        if not str(row.get("Make", "")).strip():
            out.at[idx, "Make"] = _safe_get(info, "Make", "make")
    return out


def load_positions_with_metadata() -> pd.DataFrame:
    """Fetch MiX positions and asset names/registrations (no tacho yet)."""
    creds = _load_server_creds()
    api_url = creds["ApiUrl"]
    token = ensure_bearer_token()
    raw = fetch_latest_positions()
    asset_lookup = _build_asset_lookup(api_url, token)
    if asset_lookup:
        log.info("MiX: loaded metadata for %s asset(s)", len(asset_lookup))
    else:
        log.warning("MiX: no asset metadata returned — names/registrations will be blank")
    df = positions_to_dataframe(raw, asset_lookup)
    df = enrich_positions_dataframe(df, asset_lookup)
    named = int(df["AssetName"].astype(str).str.strip().ne("").sum()) if not df.empty else 0
    log.info("MiX: positions metadata ready — %s rows, %s with asset names", len(df), named)
    return df


def load_positions_dataframe() -> pd.DataFrame:
    """Fetch latest MiX positions (+ asset names/registrations + tacho) as a DataFrame."""
    creds = _load_server_creds()
    api_url = creds["ApiUrl"]
    token = ensure_bearer_token()
    df = load_positions_with_metadata()
    if not df.empty:
        asset_ids = [int(a) for a in df["AssetId"].astype(str) if str(a).strip().isdigit()]
        df = enrich_dataframe_with_tacho(df, api_url, token, asset_ids=asset_ids)
    named = int(df["AssetName"].astype(str).str.strip().ne("").sum()) if not df.empty else 0
    log.info("MiX: positions ready — %s rows, %s with asset names", len(df), named)
    return df


def _fetch_group_assets_list(api_url: str, token: str, group_id: int) -> list[dict[str, Any]]:
    """Return assets for a MiX group (preferred endpoint for ZA)."""
    base = api_url.rstrip("/")
    headers = _api_headers(token)
    url = f"{base}/api/assets/group/{group_id}"
    _throttle_mix_api()
    try:
        resp = _session.get(url, headers=headers, timeout=45)
    except requests.RequestException as e:
        log.debug("MiX assets/group/%s: %s", group_id, e)
        return list(fetch_assets_for_group(api_url, token, group_id).values())
    if resp.status_code != 200:
        return list(fetch_assets_for_group(api_url, token, group_id).values())
    data = resp.json()
    return data if isinstance(data, list) else []

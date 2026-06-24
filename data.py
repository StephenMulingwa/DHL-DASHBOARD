"""Data loaders for the DHL dashboard.

Each public function returns a pandas DataFrame and is cached for `TTL_SECONDS`
so the dashboard does not hammer the VSS API. Use `bust_cache()` to force a refresh.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from vss_client import (
    alarms_find_all_by_time_for_devices,
    discover_dhl_devices,
    get_lang_dict,
    realtime_status_for_devices,
)


def _cache_ttl_seconds() -> int:
    """Keep cache max age aligned with ``DHL_AUTO_REFRESH_MINUTES`` (see ``app.py``)."""
    try:
        minutes = int(os.environ.get("DHL_AUTO_REFRESH_MINUTES", "60").strip() or "60")
    except ValueError:
        minutes = 60
    minutes = max(1, min(minutes, 24 * 60))
    return minutes * 60


TTL_SECONDS = _cache_ttl_seconds()
_MIX_CATALOG_PATH = Path(__file__).resolve().parent / ".mix_asset_catalog.csv"

log = logging.getLogger(__name__)
_REPO_DIR = Path(__file__).resolve().parent
_DEVICE_SNAPSHOT_COLUMNS = ["DeviceID", "DeviceName", "FleetID", "Fleet"]


def _device_snapshot_path() -> Path:
    """CSV written after successful discovery; override with ``DHL_DEVICE_SNAPSHOT_PATH``."""
    raw = os.environ.get("DHL_DEVICE_SNAPSHOT_PATH", "").strip()
    if raw:
        p = Path(os.path.expandvars(raw))
        if not p.is_absolute():
            p = (_REPO_DIR / p).resolve()
        return p
    return _REPO_DIR / ".dhl_devices_snapshot.csv"


def _read_device_snapshot_frame() -> tuple[pd.DataFrame | None, float | None]:
    """Load and validate ``.dhl_devices_snapshot.csv`` (or ``DHL_DEVICE_SNAPSHOT_PATH``).

    Returns ``(df, age_hours)``; ``df`` is None if missing/unreadable/empty columns/empty rows.
    ``age_hours`` is None only if the file is missing or ``stat`` fails.
    """
    path = _device_snapshot_path()
    if _device_snapshot_disabled() or not path.is_file():
        return None, None
    try:
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return None, None
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception as e:  # noqa: BLE001
        log.warning("device snapshot unreadable (%s): %s", path, e)
        return None, age_h
    if not set(_DEVICE_SNAPSHOT_COLUMNS).issubset(df.columns):
        return None, age_h
    out = df[_DEVICE_SNAPSHOT_COLUMNS].copy()
    out = out[out["DeviceID"].str.strip().ne("") & out["DeviceID"].ne("None")]
    out = out.drop_duplicates(subset=["DeviceID"]).reset_index(drop=True)
    if out.empty:
        return None, age_h
    return out, age_h


def _device_snapshot_disabled() -> bool:
    return os.environ.get("DHL_DEVICE_SNAPSHOT_DISABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _device_snapshot_max_age_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("DHL_DEVICE_SNAPSHOT_MAX_HOURS", "168") or "168"))
    except ValueError:
        return 168.0


def _load_device_snapshot(reason: str) -> pd.DataFrame | None:
    """Last good device list from disk (after VSS errors, 10082, timeouts)."""
    if _device_snapshot_disabled():
        return None
    out, age_h = _read_device_snapshot_frame()
    if out is None or age_h is None:
        return None
    max_age = _device_snapshot_max_age_hours()
    if age_h > max_age:
        log.warning(
            "device snapshot ignored (%.1f h old, max %.1f h) — %s",
            age_h,
            max_age,
            reason,
        )
        return None
    log.warning(
        "using on-disk device snapshot (%s devices, %.1f h old): %s — refresh when VSS is healthy",
        len(out),
        age_h,
        reason,
    )
    return out


def _load_device_snapshot_primary() -> pd.DataFrame | None:
    """Intentional baseline from CSV: same age rules as fallback, INFO-level log."""
    if _device_snapshot_disabled():
        return None
    out, age_h = _read_device_snapshot_frame()
    if out is None or age_h is None:
        return None
    max_age = _device_snapshot_max_age_hours()
    if age_h > max_age:
        log.warning(
            "device snapshot too old for primary mode (%.1f h > max %.1f h): %s",
            age_h,
            max_age,
            _device_snapshot_path(),
        )
        return None
    log.info(
        "device baseline from snapshot: %s devices, %.1f h old (%s) — realtime/alarms still queried from VSS",
        len(out),
        age_h,
        _device_snapshot_path().name,
    )
    return out


def _save_device_snapshot(df: pd.DataFrame) -> None:
    if _device_snapshot_disabled() or df is None or df.empty:
        return
    try:
        df[_DEVICE_SNAPSHOT_COLUMNS].to_csv(_device_snapshot_path(), index=False)
    except OSError as e:
        log.warning("could not save device snapshot: %s", e)


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def fast_mode() -> bool:
    """Default ON for quicker first loads (keyword device list + shorter alarm window).

    Set ``DHL_FAST_MODE=0`` for full fleet crawl + 24h alarms (slowest, use when you need full fidelity).
    """
    v = os.environ.get("DHL_FAST_MODE", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def alarm_query_hours() -> int:
    """Hours of alarm history to request from VSS. ``0`` = skip alarm API calls (empty table)."""
    if not fast_mode():
        return 24
    if _env_truthy("DHL_FAST_SKIP_ALARMS"):
        return 0
    try:
        h = int(os.environ.get("DHL_FAST_ALARM_HOURS", "2").strip() or "2")
    except ValueError:
        h = 2
    return max(1, min(h, 24))


def alarms_kpi_label() -> str:
    """Short label for Overview KPI (reflects fast mode)."""
    h = alarm_query_hours()
    if h == 0:
        return "Alarms (skipped, fast)"
    if h == 24:
        return "Alarms in 24h"
    return f"Alarms ({h}h, fast)"


@dataclass
class _CacheEntry:
    value: Any
    at: float = field(default_factory=time.time)


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()
_cache_producer_locks: dict[str, threading.Lock] = {}
_mix_load_lock = threading.Lock()
_mix_health_error: str | None = None
# Monotonic time of last ``bust_cache(None)`` (manual / scheduled full refresh).
_last_full_bust_at: float | None = None


def _cached(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry.at < ttl:
            return entry.value
        prod_lock = _cache_producer_locks.setdefault(key, threading.Lock())

    # If a background refresh is already producing this value, keep serving the
    # stale value rather than making page requests pile up behind the same VSS
    # workflow. The producer will replace the cache when it finishes.
    if not prod_lock.acquire(blocking=False):
        if entry:
            return entry.value
        with prod_lock:
            with _cache_lock:
                entry = _cache.get(key)
                if entry:
                    return entry.value
        value = producer()
        with _cache_lock:
            _cache[key] = _CacheEntry(value=value)
        return value

    try:
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.time() - entry.at < ttl:
                return entry.value
        value = producer()
        with _cache_lock:
            _cache[key] = _CacheEntry(value=value)
        return value
    finally:
        prod_lock.release()


def bust_cache(prefix: str | None = None) -> None:
    global _last_full_bust_at
    with _cache_lock:
        if prefix is None:
            _cache.clear()
            _last_full_bust_at = time.time()
        else:
            for k in list(_cache.keys()):
                if k.startswith(prefix):
                    _cache.pop(k, None)


def _clear_mix_disk_catalog() -> None:
    try:
        if _MIX_CATALOG_PATH.is_file():
            _MIX_CATALOG_PATH.unlink()
    except OSError as e:
        log.debug("MiX asset catalog disk delete skipped: %s", e)


def clear_all_dashboard_caches(*, include_disk_catalog: bool = True) -> None:
    """Drop in-memory caches, MiX tacho cache, and optional on-disk MiX catalog."""
    global _last_full_bust_at
    try:
        from mix_client import clear_tacho_cache

        clear_tacho_cache()
    except Exception:
        pass
    with _cache_lock:
        _cache.clear()
        _last_full_bust_at = time.time()
    if include_disk_catalog:
        _clear_mix_disk_catalog()


def bust_cache_for_refresh(*, keep_devices: bool = False, clear_mix_disk_catalog: bool = False) -> None:
    """Clear dashboard caches for a manual refresh.

    When ``keep_devices`` is True (snapshot mode), VSS realtime/alarms are cleared;
    MiX caches are cleared too so names/registrations reload on refresh.
    """
    global _last_full_bust_at
    try:
        from mix_client import clear_tacho_cache

        clear_tacho_cache()
    except Exception:
        pass
    with _cache_lock:
        if keep_devices:
            _cache.pop("realtime_status", None)
            _cache.pop("alarms_24h", None)
            _cache.pop("mix_positions", None)
            _cache.pop("mix_health", None)
            if clear_mix_disk_catalog:
                _cache.pop("mix_asset_catalog", None)
            # Keep mix_asset_catalog in memory so names survive refresh while MiX reloads.
        else:
            _cache.clear()
        _last_full_bust_at = time.time()
    if clear_mix_disk_catalog:
        _clear_mix_disk_catalog()


def cache_put(key: str, value: Any) -> None:
    """Write a cache entry (used by background refresh without clearing the UI first)."""
    with _cache_lock:
        _cache[key] = _CacheEntry(value=value)


def cache_age_seconds(key: str) -> float | None:
    with _cache_lock:
        entry = _cache.get(key)
    if not entry:
        return None
    return time.time() - entry.at


def cache_get(key: str, ttl: int = TTL_SECONDS) -> Any:
    """Return cached value if present and fresh, else None (does NOT compute)."""
    with _cache_lock:
        entry = _cache.get(key)
    if not entry:
        return None
    if time.time() - entry.at >= ttl:
        return None
    return entry.value


def cache_peek(key: str) -> Any:
    """Return cached value if present (any age). Used so the UI keeps showing data during reload."""
    with _cache_lock:
        entry = _cache.get(key)
    return entry.value if entry else None


# DHL device baseline

def _col(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def _load_dhl_devices() -> pd.DataFrame:
    empty = pd.DataFrame(columns=_DEVICE_SNAPSHOT_COLUMNS)
    if _env_truthy("DHL_DEVICES_FROM_SNAPSHOT"):
        snap = _load_device_snapshot_primary()
        if snap is not None:
            return snap
        log.warning(
            "DHL_DEVICES_FROM_SNAPSHOT is set but no usable CSV at %s — falling back to VSS device discovery",
            _device_snapshot_path(),
        )
    try:
        if fast_mode():
            max_pages = int(os.environ.get("DHL_FAST_DEVICE_MAX_PAGES", "25") or "25")
            max_pages = max(5, min(max_pages, 60))
            # IMPORTANT: "Proper" DHL device discovery is fleet-based:
            #   1) find fleets whose name contains DHL
            #   2) page devices per fleet
            # Keyword-only is only an opt-in fallback because it can be incomplete / misleading.
            kw_only = os.environ.get("DHL_FAST_DEVICE_KEYWORD_ONLY", "0").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            workers = int(os.environ.get("DHL_FAST_DEVICE_FLEET_WORKERS", "1") or "1")
            rows = discover_dhl_devices(
                page_size=200,
                max_pages=max_pages,
                contains="DHL",
                skip_fleet_discovery=kw_only,
                fleet_fetch_workers=max(1, workers),
            )
        else:
            workers = int(os.environ.get("DHL_DEVICE_FLEET_WORKERS", "1") or "1")
            rows = discover_dhl_devices(
                page_size=200,
                max_pages=60,
                contains="DHL",
                fleet_fetch_workers=max(1, workers),
            )
    except Exception as e:  # noqa: BLE001
        snap = _load_device_snapshot(f"VSS error during device discovery: {e}")
        if snap is not None:
            return snap
        raise

    if not rows:
        snap = _load_device_snapshot("VSS returned no devices")
        if snap is not None:
            return snap
        return empty

    df = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "DeviceID": _col(df, "deviceno").astype(str),
            "DeviceName": _col(df, "devicename").astype(str),
            "FleetID": _col(df, "fleetid").astype(str),
            "Fleet": _col(df, "fleetName").astype(str),
        }
    )
    out = out[out["DeviceID"].str.strip().ne("") & out["DeviceID"].ne("None")]
    out = out.drop_duplicates(subset=["DeviceID"]).reset_index(drop=True)
    _save_device_snapshot(out)
    return out


def load_dhl_devices() -> pd.DataFrame:
    """349-ish row baseline of every device in DHL fleets."""
    return _cached("dhl_devices", TTL_SECONDS, _load_dhl_devices).copy()


def refresh_dhl_devices() -> pd.DataFrame:
    """Force a device-list reload and update the cache (stale-while-revalidate)."""
    value = _load_dhl_devices()
    cache_put("dhl_devices", value)
    return value.copy()


def get_dhl_devices_cached() -> pd.DataFrame | None:
    val = cache_get("dhl_devices")
    return val.copy() if isinstance(val, pd.DataFrame) else None


def get_realtime_cached() -> pd.DataFrame | None:
    val = cache_get("realtime_status")
    return val.copy() if isinstance(val, pd.DataFrame) else None


def get_alarms_cached() -> pd.DataFrame | None:
    val = cache_get("alarms_24h")
    return val.copy() if isinstance(val, pd.DataFrame) else None


# MiX telematics positions


def _load_mix_positions() -> pd.DataFrame:
    from mix_client import (
        empty_positions_dataframe,
        enrich_dataframe_with_tacho,
        ensure_bearer_token,
        api_base_url,
        load_positions_with_metadata,
        mix_enabled,
    )

    if not mix_enabled():
        return empty_positions_dataframe()
    with _mix_load_lock:
        try:
            df = load_positions_with_metadata()
            put_mix_asset_catalog(df)
            # Publish names/registrations before the slow per-asset tacho pass.
            cache_put("mix_positions", _ensure_mix_asset_names(df.copy()))
            if not df.empty:
                api_url = api_base_url()
                token = ensure_bearer_token()
                asset_ids = [int(a) for a in df["AssetId"].astype(str) if str(a).strip().isdigit()]
                df = enrich_dataframe_with_tacho(df, api_url, token, asset_ids=asset_ids)
            df = _ensure_mix_asset_names(df)
            cache_put("mix_positions", df.copy())
            return df
        except Exception as e:  # noqa: BLE001
            log.warning("MiX positions load failed: %s", e)
            raise


def load_mix_positions() -> pd.DataFrame:
    return _cached("mix_positions", TTL_SECONDS, _load_mix_positions).copy()


def refresh_mix_positions() -> pd.DataFrame:
    value = _load_mix_positions()
    value = _apply_mix_asset_catalog(value)
    cache_put("mix_positions", value)
    return value.copy()


def put_mix_asset_catalog(df: pd.DataFrame | None) -> None:
    """Cache AssetId / name / registration for MiX filter dropdowns."""
    if df is None or df.empty or "AssetId" not in df.columns:
        return
    cols = [c for c in ("AssetId", "AssetName", "Registration", "GroupName", "Make") if c in df.columns]
    if not cols:
        return
    incoming = df[cols].copy()
    incoming["AssetId"] = incoming["AssetId"].astype(str)
    for col in ("AssetName", "Registration", "GroupName", "Make"):
        if col in incoming.columns:
            incoming[col] = incoming[col].fillna("").astype(str).str.strip()
    incoming = incoming[
        incoming.get("AssetName", pd.Series(dtype=str)).astype(str).str.strip().ne("")
        | incoming.get("Registration", pd.Series(dtype=str)).astype(str).str.strip().ne("")
    ]
    if incoming.empty:
        return
    existing = get_mix_asset_catalog()
    if existing is not None and not existing.empty:
        combined = pd.concat([existing, incoming], ignore_index=True)
        combined = combined.drop_duplicates(subset=["AssetId"], keep="last")
    else:
        combined = incoming.drop_duplicates(subset=["AssetId"], keep="last")
    cache_put("mix_asset_catalog", combined)
    try:
        combined.to_csv(_MIX_CATALOG_PATH, index=False)
    except OSError as e:
        log.debug("MiX asset catalog disk write skipped: %s", e)


def _load_mix_asset_catalog_from_disk() -> pd.DataFrame | None:
    if not _MIX_CATALOG_PATH.is_file():
        return None
    try:
        df = pd.read_csv(_MIX_CATALOG_PATH, dtype=str).fillna("")
        if df.empty or "AssetId" not in df.columns:
            return None
        cache_put("mix_asset_catalog", df)
        return df.copy()
    except Exception as e:  # noqa: BLE001
        log.debug("MiX asset catalog disk read skipped: %s", e)
        return None


def get_mix_asset_catalog() -> pd.DataFrame | None:
    val = cache_peek("mix_asset_catalog")
    if isinstance(val, pd.DataFrame) and not val.empty:
        return val.copy()
    return _load_mix_asset_catalog_from_disk()


def _apply_mix_asset_catalog(df: pd.DataFrame) -> pd.DataFrame:
    """Join AssetName / Registration / Make onto positions rows by AssetId."""
    catalog = get_mix_asset_catalog()
    if catalog is None or catalog.empty or df.empty or "AssetId" not in df.columns:
        return df

    out = df.copy()
    out["AssetId"] = out["AssetId"].astype(str)
    cat = catalog.drop_duplicates(subset=["AssetId"], keep="last").set_index("AssetId")
    ids = out["AssetId"]

    for col in ("AssetName", "Registration", "Make", "GroupName"):
        if col not in cat.columns or col not in out.columns:
            continue
        mapped = ids.map(cat[col]).fillna("").astype(str).str.strip()
        current = out[col].fillna("").astype(str).str.strip()
        out[col] = mapped.where(mapped.ne(""), current)
    return out


def _backfill_mix_positions_metadata_from_health(df: pd.DataFrame) -> pd.DataFrame:
    """Fill blank AssetName / Registration / Make from mix_health when available."""
    if df is None or df.empty or "AssetId" not in df.columns:
        return df

    health = cache_peek("mix_health")
    if not isinstance(health, pd.DataFrame) or health.empty or "AssetId" not in health.columns:
        return df

    meta = health.drop_duplicates(subset=["AssetId"]).copy()
    meta["AssetId"] = meta["AssetId"].astype(str)
    meta = meta.set_index("AssetId", drop=False)
    out = df.copy()
    ids = out["AssetId"].astype(str)

    for col in ("AssetName", "Registration", "Make", "GroupName"):
        if col not in meta.columns or col not in out.columns:
            continue
        current = out[col].fillna("").astype(str).str.strip()
        mapped = ids.map(meta[col])
        mapped = mapped.fillna("").astype(str).str.strip()
        fill = current.eq("") & mapped.ne("")
        if fill.any():
            out.loc[fill, col] = mapped[fill]
    return out


def get_mix_positions_cached() -> pd.DataFrame | None:
    val = cache_peek("mix_positions")
    if not isinstance(val, pd.DataFrame):
        return None
    df = val.copy()
    if df.empty or "AssetId" not in df.columns:
        return df
    return _ensure_mix_asset_names(df)


def _ensure_mix_asset_names(df: pd.DataFrame) -> pd.DataFrame:
    """Join catalog + health metadata; fall back to registration when name is blank."""
    out = _backfill_mix_positions_metadata_from_health(_apply_mix_asset_catalog(df))
    if out.empty or "AssetId" not in out.columns or "AssetName" not in out.columns:
        return out
    blank = out["AssetName"].fillna("").astype(str).str.strip().eq("")
    if blank.any() and "Registration" in out.columns:
        reg = out["Registration"].fillna("").astype(str).str.strip()
        fill = blank & reg.ne("")
        if fill.any():
            out.loc[fill, "AssetName"] = reg[fill]
    return out


def _mix_dropdown_options(values) -> list[dict]:
    return [{"label": str(v), "value": str(v)} for v in sorted({str(x) for x in values if str(x).strip()})]


def get_mix_filter_options() -> tuple[list[dict], list[dict], list[dict]]:
    """Dropdown options for MiX positions filters (shared asset catalog + caches)."""
    _sync_mix_catalog_from_caches()
    catalog = get_mix_asset_catalog()
    df = get_mix_positions_cached()
    health_raw = cache_peek("mix_health")
    health = health_raw.copy() if isinstance(health_raw, pd.DataFrame) else None

    def _col_values(frame: pd.DataFrame | None, name: str) -> pd.Series:
        if frame is None or frame.empty or name not in frame.columns:
            return pd.Series(dtype=str)
        return frame[name].fillna("").astype(str).str.strip()

    group_vals = pd.concat(
        [_col_values(catalog, "GroupName"), _col_values(df, "GroupName"), _col_values(health, "GroupName")]
    )
    asset_vals = pd.concat(
        [
            _col_values(catalog, "AssetName"),
            _col_values(df, "AssetName"),
            _col_values(health, "AssetName"),
        ]
    )
    reg_vals = pd.concat(
        [
            _col_values(catalog, "Registration"),
            _col_values(df, "Registration"),
            _col_values(health, "Registration"),
        ]
    )

    groups = _mix_dropdown_options(group_vals)
    assets = _mix_dropdown_options(asset_vals)
    regs = _mix_dropdown_options(reg_vals)
    if not assets and health is not None and not health.empty:
        put_mix_asset_catalog(health)
        assets = _mix_dropdown_options(_col_values(get_mix_asset_catalog(), "AssetName"))
        regs = _mix_dropdown_options(_col_values(get_mix_asset_catalog(), "Registration"))
    return groups, assets, regs


def get_mix_asset_dropdown_options() -> list[dict]:
    """Asset name + registration options (shared by Live positions and health tabs)."""
    _groups, assets, regs = get_mix_filter_options()
    seen: set[str] = set()
    options: list[dict] = []
    for o in assets + regs:
        val = str(o.get("value", "")).strip()
        if not val or val in seen:
            continue
        seen.add(val)
        options.append(o)
    options.sort(key=lambda x: str(x.get("label", "")).lower())
    return options


def last_mix_error() -> str | None:
    return _mix_health_error


def _load_mix_health() -> pd.DataFrame:
    global _mix_health_error
    from mix_health import build_health_dataframe, empty_health_dataframe
    from mix_client import mix_enabled

    if not mix_enabled():
        _mix_health_error = None
        return empty_health_dataframe()
    with _mix_load_lock:
        try:
            df = build_health_dataframe()
            _mix_health_error = None
            cache_put("mix_health", df)
            return df
        except Exception as exc:
            _mix_health_error = str(exc)
            raise


def load_mix_health() -> pd.DataFrame:
    return _cached("mix_health", TTL_SECONDS, _load_mix_health).copy()


def refresh_mix_health() -> pd.DataFrame:
    value = _load_mix_health()
    cache_put("mix_health", value)
    return value.copy()


def invalidate_stale_mix_caches() -> bool:
    """Drop MiX caches built with an older health schema (e.g. tacho-based RPM flags)."""
    health = cache_peek("mix_health")
    if not isinstance(health, pd.DataFrame) or health.empty:
        return False
    stale = "RpmFault7d" not in health.columns
    if not stale and "Issues" in health.columns:
        stale = bool(
            health["Issues"].astype(str).str.contains("No RPM data", case=False, na=False).any()
        )
    if not stale:
        return False
    with _cache_lock:
        _cache.pop("mix_positions", None)
        _cache.pop("mix_health", None)
        _cache.pop("mix_asset_catalog", None)
    _clear_mix_disk_catalog()
    log.info("MiX: cleared stale health/positions cache (schema or RPM rules updated)")
    return True


def _sync_mix_catalog_from_caches() -> None:
    """Keep asset catalog populated for dropdowns (prefer health, then positions)."""
    for key in ("mix_health", "mix_positions"):
        frame = cache_peek(key)
        if isinstance(frame, pd.DataFrame) and not frame.empty and "AssetId" in frame.columns:
            put_mix_asset_catalog(frame)
            return


def get_mix_health_cached() -> pd.DataFrame | None:
    val = cache_peek("mix_health")
    if not isinstance(val, pd.DataFrame):
        return None
    if invalidate_stale_mix_caches():
        return None
    return val.copy()


def dataframe_to_store(df: pd.DataFrame | None) -> dict | None:
    """Serialize a DataFrame for ``dcc.Store`` (Dash multi-page safe)."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    return {"records": df.to_dict("records"), "columns": list(df.columns)}


def store_to_dataframe(store: dict | None) -> pd.DataFrame | None:
    """Rebuild a DataFrame from ``dcc.Store`` payload."""
    if not store or not store.get("records"):
        return None
    cols = store.get("columns")
    if cols:
        return pd.DataFrame(store["records"], columns=cols)
    return pd.DataFrame(store["records"])


def mix_integration_enabled() -> bool:
    from mix_client import mix_enabled

    return mix_enabled()


# Realtime status


def _baseline_by_device_id(devices_df: pd.DataFrame) -> dict[str, dict]:
    """Baseline rows keyed by ``DeviceID`` plus int-normalized aliases (``03098`` ↔ ``3098``)."""
    out: dict[str, dict] = {}
    for row in devices_df.to_dict(orient="records"):
        did = str(row.get("DeviceID", "")).strip()
        if not did or did == "None":
            continue
        rec = {**row, "DeviceID": did}
        out[did] = rec
        if did.isdigit():
            compact = str(int(did))
            if compact != did:
                out.setdefault(compact, rec)
    return out


def _alarm_device_canonical_map(devices_df: pd.DataFrame) -> dict[str, str]:
    """Map API id strings (including int form) -> canonical ``DeviceID`` from the baseline."""
    m: dict[str, str] = {}
    for did in devices_df["DeviceID"].astype(str).str.strip():
        if not did or did == "None":
            continue
        m[did] = did
        if did.isdigit():
            m.setdefault(str(int(did)), did)
    return m


_STATUS_TYPE_MAP = {
    1: "Normal",
    2: "Offline Long Time",
    3: "Storage Error",
    4: "Disk Failure",
    5: "Power Off",
    6: "GPS Failure",
    7: "Camera Failure",
}


def _row_int(row: dict, *names: str, default: int | None = None) -> int | None:
    for n in names:
        if n in row and row[n] is not None and row[n] != "":
            try:
                return int(row[n])
            except (TypeError, ValueError):
                pass
    return default


def _row_float(row: dict, *names: str, default: float | None = None) -> float | None:
    for n in names:
        if n in row and row[n] is not None and row[n] != "":
            try:
                return float(row[n])
            except (TypeError, ValueError):
                pass
    return default


def _row_str(row: dict, *names: str, default: str = "") -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n])
    return default


_CHANNEL_RE = re.compile(r"ch(\d+)", re.I)


def parse_channels(formatter: str) -> list[int]:
    """Parse 'ch1;ch2;ch3;' into [1, 2, 3]."""
    if not isinstance(formatter, str) or not formatter.strip():
        return []
    return [int(m.group(1)) for m in _CHANNEL_RE.finditer(formatter)]


# Per-channel columns + Real-Time filter dropdown (CH1..CH4 — last camera channel).
RT_VIDEO_LOST_CHANNEL_MAX = 4


def _realtime_fetch_params() -> tuple[int, float, int]:
    """VSS-friendly defaults: low concurrency avoids throttling/timeouts that *slow* overall progress.

    Override with DHL_REALTIME_BATCH / DHL_REALTIME_MAX_WORKERS only if your server handles it.
    """
    try:
        batch = int(os.environ.get("DHL_REALTIME_BATCH", "0") or "0")
    except ValueError:
        batch = 0
    try:
        max_workers = int(os.environ.get("DHL_REALTIME_MAX_WORKERS", "0") or "0")
    except ValueError:
        max_workers = 0
    try:
        sleep_s = float(os.environ.get("DHL_REALTIME_SLEEP_S", "0") or "0")
    except ValueError:
        sleep_s = 0.0
    if batch <= 0:
        # Larger batches = fewer HTTP round-trips to VSS (faster fleet realtime pull).
        batch = 24 if fast_mode() else 16
    if max_workers <= 0:
        max_workers = 1
    batch = max(3, min(batch, 50))
    max_workers = max(1, min(max_workers, 1))
    return batch, max(0.0, sleep_s), max_workers


def _module_flag(module: dict, key: str) -> str:
    v = module.get(key) if isinstance(module, dict) else None
    if v is None or v == "":
        return ""
    try:
        return "Working" if int(v) == 1 else "Not Working"
    except (TypeError, ValueError):
        return "Unknown"


def _parse_state_json(raw: dict) -> dict:
    sj = raw.get("stateJson")
    if isinstance(sj, dict):
        return sj
    if isinstance(sj, str) and sj.strip():
        try:
            parsed = json.loads(sj)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _normalize_realtime_row(raw: dict, baseline_by_id: dict[str, dict]) -> dict:
    dev_raw = _row_str(raw, "deviceguid", "deviceID", "deviceid", "deviceno")
    k = str(dev_raw).strip()
    base = baseline_by_id.get(k) or {}
    if not base and k.isdigit():
        base = baseline_by_id.get(str(int(k))) or {}
    canonical = str(base.get("DeviceID", k)).strip() if base else k

    state = _parse_state_json(raw)
    module = state.get("module") if isinstance(state, dict) else {}
    if not isinstance(module, dict):
        module = {}

    record_state_fmt = _row_str(raw, "recordstateFormatter", "recordStateFormatter")
    video_lost_fmt = _row_str(raw, "videoloststateFormatter", "videoLostStateFormatter")
    video_mask_fmt = _row_str(raw, "videomaskstateFormatter", "videoMaskStateFormatter")

    time_str = _row_str(raw, "reportTime", "createtime", "createTime", "time", "Time")
    age_hours: float | None = None
    if time_str:
        ts = pd.to_datetime(time_str, errors="coerce")
        if pd.notna(ts):
            age_hours = max(0.0, (datetime.now() - ts.to_pydatetime()).total_seconds() / 3600.0)

    acc_val = raw.get("accState")
    if acc_val is None or acc_val == "":
        acc_val = raw.get("accstate")
    try:
        ignition = "On" if int(acc_val) == 1 else "Off"
    except (TypeError, ValueError):
        ignition = "Unknown"

    lost_raw = parse_channels(video_lost_fmt)
    lost_channels = [c for c in lost_raw if 1 <= c <= RT_VIDEO_LOST_CHANNEL_MAX]
    # KPI + module bar: "channel fault" uses videoloststateFormatter (per product request).
    not_recording_flag = "Not Working" if lost_channels else "Working"
    video_lost_by_ch = {n: ("Not Working" if n in lost_channels else "Working") for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1)}

    if age_hours is None:
        status_type = "Status Unknown"
    elif age_hours > 168:
        status_type = "Offline Long Time"
    elif age_hours > 24:
        status_type = "Stale"
    else:
        status_type = "Normal"

    return {
        "DeviceID": canonical,
        "DeviceName": _row_str(raw, "deviceName", "devicename") or base.get("DeviceName", ""),
        "FleetID": base.get("FleetID", ""),
        "Fleet": base.get("Fleet", "") or _row_str(raw, "fleetName"),
        "Time": time_str,
        "AgeHours": age_hours,
        "Ignition": ignition,
        "MobileNetwork": _module_flag(module, "mobile"),
        "GPSModule": _module_flag(module, "location"),
        "GsensorModule": _module_flag(module, "gsensor"),
        "WifiModule": _module_flag(module, "wifi"),
        "NotRecordingFlag": not_recording_flag,
        "VideoLostChannels": ",".join(str(c) for c in sorted(set(lost_channels))),
        **{f"VideoLost_Ch{n}": video_lost_by_ch[n] for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1)},
        "recordstateFormatter": record_state_fmt,
        "videoloststateFormatter": video_lost_fmt,
        "videomaskstateFormatter": video_mask_fmt,
        "netType": _row_int(raw, "netType"),
        "signalValue": _row_int(raw, "signalValue"),
        "cpuTemp": _row_float(raw, "cpuTemp"),
        "diskTemp": _row_float(raw, "diskTemp"),
        "devVoltage": _row_float(raw, "devVoltage"),
        "batVoltage": _row_float(raw, "batVoltage"),
        "StatusType": status_type,
    }


def _realtime_baseline_from_devices(devices_df: pd.DataFrame) -> pd.DataFrame:
    """Device list with unknown live fields when VSS realtime is unavailable."""
    base = devices_df[["DeviceID", "DeviceName", "FleetID", "Fleet"]].copy()
    merged = base.copy()
    empty_extra = ["VideoLostChannels"] + [f"VideoLost_Ch{n}" for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1)]
    for col in [
        "Time", "AgeHours", "Ignition", "MobileNetwork", "GPSModule", "GsensorModule",
        "WifiModule", "NotRecordingFlag", *empty_extra, "recordstateFormatter", "videoloststateFormatter",
        "videomaskstateFormatter", "netType", "signalValue", "cpuTemp", "diskTemp",
        "devVoltage", "batVoltage", "StatusType",
    ]:
        merged[col] = pd.Series(dtype="object")
    merged["StatusType"] = "Status Unknown"
    return merged


def _load_realtime_status() -> pd.DataFrame:
    devices_df = load_dhl_devices()
    if devices_df.empty:
        return _realtime_baseline_from_devices(
            pd.DataFrame(columns=["DeviceID", "DeviceName", "FleetID", "Fleet"])
        )

    baseline_by_id = _baseline_by_device_id(devices_df)
    device_ids = sorted({str(r["DeviceID"]).strip() for r in baseline_by_id.values() if str(r.get("DeviceID", "")).strip()})

    rb, rs, rw = _realtime_fetch_params()
    try:
        rows = realtime_status_for_devices(device_ids, batch=rb, sleep_s=rs, max_workers=rw)
    except Exception as e:
        log.warning("realtime VSS fetch failed — using device baseline: %s", e)
        return _realtime_baseline_from_devices(devices_df)
    if not rows:
        rows = []

    norm = [_normalize_realtime_row(r, baseline_by_id) for r in rows if isinstance(r, dict)]
    rt_df = pd.DataFrame(norm)

    base = devices_df[["DeviceID", "DeviceName", "FleetID", "Fleet"]].copy()
    if rt_df.empty:
        return _realtime_baseline_from_devices(devices_df)

    rt_df = rt_df.drop(columns=["DeviceName", "FleetID", "Fleet"], errors="ignore")
    merged = base.merge(rt_df, on="DeviceID", how="left")
    merged["StatusType"] = merged["StatusType"].fillna("Status Unknown")
    if "VideoLostChannels" in merged.columns:
        merged["VideoLostChannels"] = merged["VideoLostChannels"].fillna("")
    for n in range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1):
        col = f"VideoLost_Ch{n}"
        if col in merged.columns:
            merged[col] = merged[col].fillna("Working")
    return merged


def load_realtime_status() -> pd.DataFrame:
    return _cached("realtime_status", TTL_SECONDS, _load_realtime_status).copy()


def refresh_realtime_status() -> pd.DataFrame:
    """Force a realtime pull and update the cache without clearing it first."""
    value = _load_realtime_status()
    cache_put("realtime_status", value)
    return value.copy()


# Alarms (last 24h)

# Only these 8 alarm types are shown anywhere in the dashboard.
TARGET_ALARMS: list[str] = [
    "Rollover",
    "Camera Covered",
    "Video Lost",
    "Storage Error",
    "Low Voltage Alarm",
    "Power Down During Driving",
    "Vehicle Offline for a Long Time",
    "Driver Leave",
]


def _load_lang_dict() -> dict:
    return get_lang_dict("en") or {}


def _build_label_to_code(lang_data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(lang_data, dict):
        return out
    for k, v in lang_data.items():
        if not isinstance(k, str) or not k.startswith("alarm.type-"):
            continue
        code = k.split("alarm.type-", 1)[1]
        label = str(v or "").strip()
        if not label:
            continue
        out[label.lower()] = code
    return out


def _target_alarm_codes() -> list[str]:
    lang = _cached("lang_en", 24 * 3600, _load_lang_dict)
    label_to_code = _build_label_to_code(lang)
    codes: list[str] = []
    missing: list[str] = []
    for label in TARGET_ALARMS:
        code = label_to_code.get(label.lower())
        if code:
            codes.append(code)
        else:
            missing.append(label)
    if missing:
        # Not fatal: API will return all types, pandas filter still scopes by name.
        print("WARN: no alarm code found in lang dict for:", missing)
    return codes


def _normalize_label(s: str) -> str:
    return " ".join(str(s or "").split()).lower()


_TARGET_ALARM_NORMALIZED = {_normalize_label(a) for a in TARGET_ALARMS}


def _load_alarms_last_hours(hours: int) -> pd.DataFrame:
    devices_df = load_dhl_devices()
    if devices_df.empty:
        return pd.DataFrame()

    device_ids = sorted({d for d in devices_df["DeviceID"].astype(str) if d and d != "None"})
    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(hours=hours)

    codes = _target_alarm_codes()
    alarm_type_csv = ",".join(codes)

    max_pages = 30
    max_workers = 6
    if fast_mode() and hours < 24:
        max_workers = max(2, int(os.environ.get("DHL_FAST_ALARM_WORKERS", "3") or "3"))
        max_pages = max(3, int(os.environ.get("DHL_FAST_ALARM_MAX_PAGES", "10") or "10"))

    # NOTE: this server ignores fleetIdList on /alarm/findAllByTime — query by deviceID batches.
    try:
        rows = alarms_find_all_by_time_for_devices(
            begin_dt=begin_dt,
            end_dt=end_dt,
            device_ids=device_ids,
            alarm_type_csv=alarm_type_csv,
            page_count=500,
            max_pages=max_pages,
            batch_size=50,
            max_workers=max_workers,
        )
    except Exception as e:
        log.warning("alarms VSS fetch failed — returning empty set: %s", e)
        return pd.DataFrame(columns=[
            "DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName",
            "AlarmTime", "Lat", "Lon", "Speed", "PlateNo",
        ])

    if not rows:
        return pd.DataFrame(columns=[
            "DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName",
            "AlarmTime", "Lat", "Lon", "Speed", "PlateNo",
        ])

    df = pd.DataFrame(rows)

    if "deviceno" in df.columns:
        df["DeviceID"] = df["deviceno"].astype(str).str.strip()
    elif "deviceID" in df.columns:
        df["DeviceID"] = df["deviceID"].astype(str).str.strip()
    else:
        df["DeviceID"] = df.get("deviceguid", "").astype(str).str.strip()

    canon_by_api = _alarm_device_canonical_map(devices_df)

    def _alarm_id_to_canonical(aid: str) -> str | None:
        s = str(aid).strip()
        if not s or s == "None":
            return None
        if s in canon_by_api:
            return canon_by_api[s]
        if s.isdigit():
            return canon_by_api.get(str(int(s)))
        if s.endswith(".0") and s[:-2].isdigit():
            return canon_by_api.get(str(int(s[:-2])))
        return None

    df["DeviceID"] = df["DeviceID"].map(_alarm_id_to_canonical)
    df = df[df["DeviceID"].notna()].copy()

    if df.empty:
        return pd.DataFrame(columns=[
            "DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName",
            "AlarmTime", "Lat", "Lon", "Speed", "PlateNo",
        ])

    df["DeviceName"] = df.get("deviceName", df.get("devicename", "")).astype(str)
    df["Fleet"] = df.get("fleetName", "").astype(str)

    if "alarmtype" in df.columns:
        df["AlarmCode"] = df["alarmtype"].astype(str)
    elif "alarmType" in df.columns:
        df["AlarmCode"] = df["alarmType"].astype(str)
    else:
        df["AlarmCode"] = ""

    if "alarmTypeValue" in df.columns:
        df["AlarmName"] = df["alarmTypeValue"].astype(str)
    else:
        lang = _cached("lang_en", 24 * 3600, _load_lang_dict)
        df["AlarmName"] = df["AlarmCode"].apply(lambda c: str(lang.get(f"alarm.type-{c}", f"Type {c}")))

    time_src = df["reportTime"] if "reportTime" in df.columns else None
    if time_src is None and "createtime" in df.columns:
        time_src = df["createtime"]
    elif time_src is None:
        time_src = pd.Series([pd.NaT] * len(df))
    df["AlarmTime"] = pd.to_datetime(time_src, errors="coerce")

    def _split_gps(v: Any) -> tuple[float | None, float | None]:
        """VSS manual §3.9: ``AlarmGps`` as 'lon,lat'. Legacy rows may use ``alarmGps``."""
        if not isinstance(v, str) or "," not in v:
            return (None, None)
        try:
            a, b = v.split(",", 1)
            lon = float(a)
            lat = float(b)
            return (lat, lon)
        except Exception:
            return (None, None)

    if "AlarmGps" in df.columns:
        gps_col = df["AlarmGps"]
    elif "alarmGps" in df.columns:
        gps_col = df["alarmGps"]
    else:
        gps_col = pd.Series([""] * len(df), index=df.index, dtype=object)
    coords = gps_col.apply(_split_gps)
    df["Lat"] = coords.apply(lambda t: t[0])
    df["Lon"] = coords.apply(lambda t: t[1])
    # Drop obvious junk coordinates so the map isn't dragged out to the ocean.
    df.loc[~df["Lat"].between(-90, 90, inclusive="both"), "Lat"] = None
    df.loc[~df["Lon"].between(-180, 180, inclusive="both"), "Lon"] = None
    df["Speed"] = pd.to_numeric(df.get("speed"), errors="coerce")
    df["PlateNo"] = df.get("plateNo", "").astype(str)

    # Final scope: keep only the 8 alarm types asked for, even if the API returned extras.
    # We canonicalise the AlarmName to the exact label from TARGET_ALARMS so downstream
    # filters/charts get clean values.
    canonical_by_norm = {_normalize_label(a): a for a in TARGET_ALARMS}
    df["_norm"] = df["AlarmName"].map(_normalize_label)
    df = df[df["_norm"].isin(_TARGET_ALARM_NORMALIZED)].copy()
    if not df.empty:
        df["AlarmName"] = df["_norm"].map(canonical_by_norm)
    df = df.drop(columns=["_norm"], errors="ignore")

    keep = [
        "DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName",
        "AlarmTime", "Lat", "Lon", "Speed", "PlateNo",
    ]
    return df[keep].sort_values("AlarmTime", ascending=False).reset_index(drop=True)


def _empty_alarms_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "DeviceID", "DeviceName", "Fleet", "AlarmCode", "AlarmName",
        "AlarmTime", "Lat", "Lon", "Speed", "PlateNo",
    ])


def load_alarms_last_24h() -> pd.DataFrame:
    h = alarm_query_hours()
    if h <= 0:
        return _cached("alarms_24h", TTL_SECONDS, _empty_alarms_dataframe).copy()
    return _cached("alarms_24h", TTL_SECONDS, lambda: _load_alarms_last_hours(h)).copy()


def refresh_alarms_last_24h() -> pd.DataFrame:
    """Force an alarm-history pull and update the cache without clearing it first."""
    h = alarm_query_hours()
    if h <= 0:
        value = _empty_alarms_dataframe()
    else:
        value = _load_alarms_last_hours(h)
    cache_put("alarms_24h", value)
    return value.copy()


# Convenience for the UI: last refresh wall-clock as ISO string per cache key

def cache_freshness() -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("dhl_devices", "realtime_status", "alarms_24h", "mix_positions", "mix_health"):
        age = cache_age_seconds(key)
        if age is None:
            out[key] = "not loaded"
        else:
            ts = datetime.now() - timedelta(seconds=age)
            out[key] = ts.strftime("%Y-%m-%d %H:%M:%S")
    return out


def last_bust_cache_iso() -> str | None:
    """Wall time of the last full ``bust_cache()`` (Refresh data / auto-refresh), or None if never."""
    if _last_full_bust_at is None:
        return None
    return datetime.fromtimestamp(_last_full_bust_at).strftime("%Y-%m-%d %H:%M:%S")


def cache_latest_data_iso() -> str | None:
    """Newest ``at`` timestamp among main dashboard caches (when any loader last finished)."""
    keys = ("dhl_devices", "realtime_status", "alarms_24h", "mix_positions", "mix_health")
    latest: float | None = None
    with _cache_lock:
        for k in keys:
            e = _cache.get(k)
            if e is not None and (latest is None or e.at > latest):
                latest = e.at
    if latest is None:
        return None
    return datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")

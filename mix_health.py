"""MiX DHL asset health rules (non-downloading, GPS, speed, RPM, spikes)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from mix_client import (
    _parse_event_age_hours,
    _safe_get,
    analyze_tacho,
    api_base_url,
    ensure_bearer_token,
    fetch_latest_positions,
    fetch_tacho_for_assets,
    group_ids,
    resolve_group_targets,
)
from mix_events import fetch_rpm_fault_events, resolve_rpm_fault_event_type_ids

log = logging.getLogger(__name__)

ISSUE_NON_DOWNLOADING = "Non downloading"
ISSUE_NO_SPEED = "No speed data"
ISSUE_NO_GPS = "No GPS data"
# MiX diagnostic library event — NOT live tacho F2 / GPS.
ISSUE_NO_RPM = "Diagnostic: no engine RPM (7d)"
ISSUE_INCONSISTENT_RPM = "Inconsistent RPM (tacho)"
ISSUE_SPEED_SPIKE = "Possible speed spike"

ALL_ISSUES = [
    ISSUE_NON_DOWNLOADING,
    ISSUE_NO_SPEED,
    ISSUE_NO_GPS,
    ISSUE_NO_RPM,
    ISSUE_SPEED_SPIKE,
]

# Shown first in the Asset health issues table (event report columns before live tacho).
_HEALTH_TABLE_COLUMN_ORDER = [
    "AssetName",
    "Registration",
    "Make",
    "GroupName",
    "RpmFault7d",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "Issues",
    "IssueCount",
    "AgeHours",
    "EventTime",
    "SpeedKmh",
    "TachoRpmF2",
    "MaxTachoRpmF2",
    "TachoRpmStdDev",
    "MaxRecentSpeedKmh",
    "SpeedJumpKmh",
    "GpsSource",
    "Satellites",
    "Latitude",
    "Longitude",
    "AssetId",
    "GroupId",
    "LastUpdated",
]

_HEALTH_COLUMNS = [
    "AssetId",
    "AssetName",
    "Registration",
    "Make",
    "GroupId",
    "GroupName",
    "EventTime",
    "AgeHours",
    "SpeedKmh",
    "MaxRecentSpeedKmh",
    "SpeedJumpKmh",
    "GpsSource",
    "Satellites",
    "Latitude",
    "Longitude",
    "TachoRpmF2",
    "MaxTachoRpmF2",
    "TachoRpmStdDev",
    "RpmFault7d",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "Issues",
    "IssueCount",
    "LastUpdated",
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_truthy(name: str, default: str = "0") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def empty_health_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_HEALTH_COLUMNS)


def _fetch_group_assets(api_url: str, token: str, group_id: int) -> list[dict[str, Any]]:
    from mix_client import _api_headers, _mix_http

    url = f"{api_url.rstrip('/')}/api/assets/group/{group_id}"
    resp = _mix_http("get", url, headers=_api_headers(token), timeout=45)
    if resp.status_code != 200:
        log.warning("MiX assets/group/%s failed: %s", group_id, resp.status_code)
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def _valid_gps(lat: Any, lon: Any, source: str, sats: Any) -> bool:
    try:
        la = float(lat)
        lo = float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False
    if abs(la) < 0.0001 and abs(lo) < 0.0001:
        return False
    src = str(source or "").strip().lower()
    if src and src not in ("gps", "avl"):
        return False
    try:
        if int(float(sats)) <= 0:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _classify_row(
    *,
    age_h: float | None,
    has_speed_feed: bool,
    speed: float | None,
    max_recent_speed: float | None,
    speed_jump: float | None,
    lat: Any,
    lon: Any,
    source: str,
    sats: Any,
    has_rpm_feed: bool,
    rpm_fault_7d: bool,
    rpm_std: float | None,
    stale_h: float,
    spike_kmh: float,
    jump_kmh: float,
    rpm_std_threshold: float,
    deep: bool,
) -> list[str]:
    issues: list[str] = []

    if age_h is None or age_h > stale_h:
        issues.append(ISSUE_NON_DOWNLOADING)

    if not has_speed_feed:
        issues.append(ISSUE_NO_SPEED)

    if not _valid_gps(lat, lon, source, sats):
        issues.append(ISSUE_NO_GPS)

    if deep:
        if rpm_fault_7d:
            issues.append(ISSUE_NO_RPM)
        elif _env_truthy("MIX_RPM_INCONSISTENT", "0") and rpm_std is not None and rpm_std >= rpm_std_threshold:
            issues.append(ISSUE_INCONSISTENT_RPM)

        if max_recent_speed is not None and max_recent_speed >= spike_kmh:
            issues.append(ISSUE_SPEED_SPIKE)
        elif speed_jump is not None and speed_jump >= jump_kmh:
            issues.append(ISSUE_SPEED_SPIKE)

    return issues


def build_health_dataframe() -> pd.DataFrame:
    """Analyse DHL MiX assets for communication / GPS / speed / RPM issues."""
    from data import put_mix_asset_catalog

    api_url = api_base_url()
    token = ensure_bearer_token()
    targets = resolve_group_targets()
    group_name_by_id = {int(t["GroupId"]): t.get("Name", "") for t in targets}

    positions = fetch_latest_positions()
    pos_by_asset: dict[str, dict[str, Any]] = {}
    for row in positions:
        aid = _safe_get(row, "AssetId", "assetId")
        if aid:
            pos_by_asset[aid] = row

    asset_rows: list[dict[str, Any]] = []
    for gid in group_ids():
        asset_rows.extend(_fetch_group_assets(api_url, token, gid))

    if not asset_rows:
        log.warning("MiX health: no assets returned for groups %s", group_ids())
        return empty_health_dataframe()

    stale_h = _env_float("MIX_NON_DOWNLOADING_HOURS", 6.0)
    spike_kmh = _env_float("MIX_SPEED_SPIKE_KMH", 120.0)
    jump_kmh = _env_float("MIX_SPEED_JUMP_KMH", 45.0)
    rpm_std_threshold = _env_float("MIX_RPM_INCONSISTENT_STDDEV", 400.0)
    tacho_minutes = _env_int("MIX_TACHO_MINUTES", 59)
    deep = _env_truthy("MIX_HEALTH_DEEP", "1")

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out_rows: list[dict[str, Any]] = []

    asset_ids = [
        int(asset.get("AssetId", asset.get("assetId")))
        for asset in asset_rows
        if asset.get("AssetId", asset.get("assetId"))
    ]
    tacho_by_asset = fetch_tacho_for_assets(api_url, token, asset_ids, minutes=tacho_minutes)
    log.info("MiX health: tacho loaded for %s asset(s) (cached where recent)", len(tacho_by_asset))

    rpm_fault_by_asset: dict[str, dict[str, Any]] = {}
    rpm_event_type_ids = resolve_rpm_fault_event_type_ids()
    if rpm_event_type_ids:
        for ev in fetch_rpm_fault_events():
            etid = ev.get("EventTypeId")
            if etid is not None and int(etid) not in rpm_event_type_ids:
                continue
            aid = str(ev.get("AssetId", ""))
            if not aid:
                continue
            ts = ev.get("StartDateTime") or ev.get("EndDateTime") or ""
            row = rpm_fault_by_asset.get(aid)
            if row is None:
                rpm_fault_by_asset[aid] = {
                    "count": 1,
                    "last": ts,
                    "category": ev.get("EventCategory", ""),
                }
            else:
                row["count"] = int(row.get("count", 0)) + 1
                if str(ts) > str(row.get("last", "")):
                    row["last"] = ts
                    row["category"] = ev.get("EventCategory", row.get("category", ""))
        log.info(
            "MiX health: %s asset(s) with no-engine-RPM diagnostic event(s) in lookback window",
            len(rpm_fault_by_asset),
        )

    catalog_rows: list[dict[str, Any]] = []

    for asset in asset_rows:
        aid = str(asset.get("AssetId", asset.get("assetId", "")))
        if not aid:
            continue
        gid = asset.get("SiteId") or asset.get("GroupId") or group_ids()[0]
        pos = pos_by_asset.get(aid, {})

        event_time = _safe_get(pos, "Timestamp", "EventTime")
        age_h = _parse_event_age_hours(event_time) if event_time else None

        lat = pos.get("Latitude", "")
        lon = pos.get("Longitude", "")
        source = _safe_get(pos, "Source", "source")
        sats = pos.get("NumberOfSatellites", "")

        speed: float | None = None
        rpm: float | None = None
        max_recent_speed: float | None = None
        speed_jump: float | None = None
        max_rpm: float | None = None
        rpm_std: float | None = None

        tacho = tacho_by_asset.get(aid)
        tach = analyze_tacho(tacho)
        speed = tach["speed_kmh"]
        rpm = tach["rpm"]
        max_recent_speed = tach["max_speed_kmh"]
        speed_jump = tach["speed_jump_kmh"]
        max_rpm = tach["max_rpm"]
        rpm_std = tach["rpm_std"]
        fault = rpm_fault_by_asset.get(aid, {})
        rpm_fault_7d = bool(fault)
        rpm_fault_count = int(fault.get("count", 0)) if fault else 0
        last_rpm_fault = fault.get("last", "") if fault else ""

        issues = _classify_row(
            age_h=age_h,
            has_speed_feed=bool(tach["has_speed_feed"]),
            speed=speed,
            max_recent_speed=max_recent_speed,
            speed_jump=speed_jump,
            lat=lat,
            lon=lon,
            source=source,
            sats=sats,
            has_rpm_feed=bool(tach["has_rpm_feed"]),
            rpm_fault_7d=rpm_fault_7d,
            rpm_std=rpm_std,
            stale_h=stale_h,
            spike_kmh=spike_kmh,
            jump_kmh=jump_kmh,
            rpm_std_threshold=rpm_std_threshold,
            deep=deep,
        )

        asset_name = _safe_get(asset, "Description", "description")
        registration = _safe_get(asset, "RegistrationNumber", "registrationNumber")
        group_name = group_name_by_id.get(int(gid), str(gid))
        catalog_rows.append(
            {
                "AssetId": aid,
                "AssetName": asset_name,
                "Registration": registration,
                "GroupName": group_name,
                "Make": _safe_get(asset, "Make", "make"),
            }
        )

        out_rows.append(
            {
                "AssetId": aid,
                "AssetName": asset_name,
                "Registration": registration,
                "Make": _safe_get(asset, "Make", "make"),
                "GroupId": str(gid),
                "GroupName": group_name,
                "EventTime": event_time,
                "AgeHours": round(age_h, 2) if age_h is not None else None,
                "SpeedKmh": speed,
                "MaxRecentSpeedKmh": max_recent_speed,
                "SpeedJumpKmh": speed_jump,
                "GpsSource": source,
                "Satellites": sats,
                "Latitude": lat,
                "Longitude": lon,
                "TachoRpmF2": rpm,
                "MaxTachoRpmF2": max_rpm,
                "TachoRpmStdDev": rpm_std,
                "RpmFault7d": rpm_fault_7d,
                "RpmFaultCount7d": rpm_fault_count,
                "LastRpmFaultTime": last_rpm_fault,
                "Issues": "; ".join(issues),
                "IssueCount": len(issues),
                "LastUpdated": run_ts,
            }
        )

    if not out_rows:
        return empty_health_dataframe()

    df = pd.DataFrame(out_rows)
    put_mix_asset_catalog(pd.DataFrame(catalog_rows))
    flagged = int((df["IssueCount"] > 0).sum())
    log.info("MiX health: %s assets analysed, %s with issues", len(df), flagged)

    # Push names into the positions cache once health metadata is ready.
    try:
        from data import _apply_mix_asset_catalog, _backfill_mix_positions_metadata_from_health, cache_peek, cache_put

        pos = cache_peek("mix_positions")
        if isinstance(pos, pd.DataFrame) and not pos.empty:
            cache_put(
                "mix_positions",
                _backfill_mix_positions_metadata_from_health(_apply_mix_asset_catalog(pos.copy())),
            )
    except Exception as e:  # noqa: BLE001
        log.debug("MiX health: positions name sync skipped: %s", e)

    return df[_HEALTH_COLUMNS]


def order_health_table_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Prefer 7-day RPM fault report columns before live tacho RPM."""
    if df is None or df.empty:
        return df
    cols = [c for c in _HEALTH_TABLE_COLUMN_ORDER if c in df.columns]
    rest = [c for c in df.columns if c not in cols]
    return df[cols + rest]


_RPM_FAULT_LIST_COLUMNS = [
    "AssetName",
    "Registration",
    "Make",
    "GroupName",
    "RpmFaultCount7d",
    "LastRpmFaultTime",
    "AgeHours",
    "TachoRpmF2",
    "Issues",
    "AssetId",
]


def rpm_fault_assets_dataframe(df: pd.DataFrame | None) -> pd.DataFrame:
    """Assets with MiX 'Diagnostic: no engine RPM' event(s) in the 7-day window."""
    if df is None or df.empty or "RpmFault7d" not in df.columns:
        return pd.DataFrame(columns=_RPM_FAULT_LIST_COLUMNS)
    out = df[df["RpmFault7d"].fillna(False).astype(bool)].copy()
    if out.empty:
        return pd.DataFrame(columns=_RPM_FAULT_LIST_COLUMNS)
    out = out.sort_values(
        ["RpmFaultCount7d", "LastRpmFaultTime"],
        ascending=[False, False],
        na_position="last",
    )
    cols = [c for c in _RPM_FAULT_LIST_COLUMNS if c in out.columns]
    return out[cols]

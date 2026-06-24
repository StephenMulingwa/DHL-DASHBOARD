"""Build template context for each dashboard page."""

from __future__ import annotations

from html import escape
from typing import Any

import pandas as pd

import components as C
from data import (
    RT_VIDEO_LOST_CHANNEL_MAX,
    alarms_kpi_label,
    get_alarms_cached,
    get_dhl_devices_cached,
    get_mix_health_cached,
    get_realtime_cached,
    last_mix_error,
    mix_integration_enabled,
    parse_channels,
)
from vss_client import last_vss_error
from web.charts import figure_html

DHL_RED = C.DHL_RED
DHL_YELLOW = C.DHL_YELLOW


def kpi_dict(
    title: str,
    value: str | int | float,
    *,
    accent: str = DHL_RED,
    border_accent: str | None = None,
    sub: str | None = None,
) -> dict:
    border = border_accent or accent
    return {"title": title, "value": str(value), "accent": accent, "border_accent": border, "sub": sub or ""}


def df_to_table_html(
    df: pd.DataFrame | None,
    columns: list[str] | None = None,
    *,
    max_rows: int = 500,
    page_size: int = 10,
) -> str:
    if df is None:
        return '<p class="muted-msg">Loading from VSS — data will appear shortly.</p>'
    if df.empty:
        return '<p class="muted-msg">No rows match the current filters.</p>'
    out = df.copy()
    if columns:
        columns = [c for c in columns if c in out.columns]
        out = out[columns]
    if "AlarmTime" in out.columns:
        out = out.assign(AlarmTime=lambda d: pd.to_datetime(d["AlarmTime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S"))
    out = out.fillna("")
    display = out.head(max_rows)
    table = display.to_html(classes="data-table report-data-table", index=False, border=0, escape=True)
    shown = len(display)
    total = len(out)
    clipped = total > shown
    clipped_msg = f"Showing first {shown:,} of {total:,} rows." if clipped else f"{total:,} rows available."
    return f"""
<div class="report-table" data-page-size="{int(page_size)}">
  <div class="table-toolbar">
    <div class="table-search-wrap">
      <span class="table-search-icon" aria-hidden="true">⌕</span>
      <input type="search" class="table-search" placeholder="Filter rows..." aria-label="Filter table rows">
    </div>
    <div class="table-toolbar-actions">
      <label class="table-page-size-label">
        Rows
        <select class="table-page-size" aria-label="Rows per page">
          <option value="10" {"selected" if page_size == 10 else ""}>10</option>
          <option value="25" {"selected" if page_size == 25 else ""}>25</option>
          <option value="50" {"selected" if page_size == 50 else ""}>50</option>
        </select>
      </label>
    </div>
  </div>
  <div class="table-wrap">{table}</div>
  <div class="table-pagination">
    <span class="table-count">{escape(clipped_msg)}</span>
    <div class="table-page-controls">
      <button type="button" class="table-prev">Prev</button>
      <span class="table-page-label">Page 1 / 1</span>
      <button type="button" class="table-next">Next</button>
    </div>
  </div>
</div>
""".strip()


def _parse_multi(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _filter_realtime(
    df: pd.DataFrame | None,
    *,
    fleets: list[str],
    statuses: list[str],
    ignitions: list[str],
    ch_filter: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if fleets:
        out = out[out["Fleet"].astype(str).isin(fleets)]
    if statuses:
        out = out[out["StatusType"].astype(str).isin(statuses)]
    if ignitions and "Ignition" in out.columns:
        out = out[out["Ignition"].astype(str).isin(ignitions)]
    cf = (ch_filter or "all").strip().lower()
    if cf not in ("", "all", "none"):
        try:
            n = int(cf)
        except ValueError:
            n = 0
        if 1 <= n <= RT_VIDEO_LOST_CHANNEL_MAX:
            col = f"VideoLost_Ch{n}"
            if col in out.columns:
                out = out[out[col].astype(str) == "Not Working"]
    return out


def _filter_alarms(df: pd.DataFrame | None, *, fleets: list[str], alarm_types: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if fleets:
        out = out[out["Fleet"].astype(str).isin(fleets)]
    if alarm_types:
        out = out[out["AlarmName"].astype(str).isin(alarm_types)]
    return out


def _fleet_options(*, df: pd.DataFrame | None = None) -> list[str]:
    if df is not None and not df.empty and "Fleet" in df.columns:
        return sorted({str(x) for x in df["Fleet"].dropna() if str(x).strip()})
    devices = get_dhl_devices_cached()
    if devices is not None and not devices.empty and "Fleet" in devices.columns:
        return sorted({str(x) for x in devices["Fleet"].dropna() if str(x).strip()})
    return []


def _vss_unavailable_fig(context: str) -> str:
    err = last_vss_error()
    if err:
        msg = f"{context} — VSS session expired. Click Refresh data or restart the app."
    else:
        msg = context
    return figure_html(C.loading_fig(msg))


def nav_items(*, active: str, mix_enabled: bool) -> list[dict]:
    items = [
        {"id": "overview", "label": "Overview", "href": "/dashboard", "icon": "grid"},
        {"id": "realtime", "label": "Real-Time Status", "href": "/dashboard/realtime", "icon": "radio"},
        {"id": "alarms", "label": "Alarms (24h)", "href": "/dashboard/alarms", "icon": "bell"},
        {"id": "device", "label": "Device Drilldown", "href": "/dashboard/device", "icon": "search"},
    ]
    if mix_enabled:
        items.insert(1, {"id": "mix", "label": "MiX Health", "href": "/dashboard/mix", "icon": "satellite"})
    for item in items:
        item["active"] = item["id"] == active
    return items


def overview_context(*, age_hours: float = 6.0) -> dict[str, Any]:
    devices = get_dhl_devices_cached()
    rt = get_realtime_cached()
    alarms = get_alarms_cached()

    banners: list[str] = []
    vss_err = last_vss_error()
    if vss_err and rt is None:
        banners.append(f"VSS connection issue: {vss_err}")
    elif devices is None and rt is None and alarms is None:
        banners.append("Loading data from VSS — charts will appear when each dataset finishes.")
    elif alarms is None and (devices is not None or rt is not None):
        banners.append("24h alarms still loading.")
    elif rt is None and devices is not None:
        banners.append("Live status still loading — device list is ready.")

    total_devices = len(devices) if devices is not None else 0
    if rt is None or rt.empty:
        online = offline = unknown = 0
    else:
        age = pd.to_numeric(rt.get("AgeHours"), errors="coerce")
        online = int((age.notna() & (age <= age_hours)).sum())
        offline = int((age.notna() & (age > age_hours)).sum())
        unknown = int(age.isna().sum())

    devices_with_alarm = 0 if alarms is None or alarms.empty else int(alarms["DeviceID"].nunique())
    total_alarms = 0 if alarms is None or alarms.empty else int(len(alarms))

    kpis = [
        kpi_dict("Total devices (VSS)", f"{total_devices:,}", border_accent="#3B82F6"),
        kpi_dict("Online", f"{online:,}", accent="#2E8B57", border_accent="#2E8B57", sub=f"<= {age_hours:g}h since last status"),
        kpi_dict("Offline", f"{offline:,}", accent=DHL_RED, border_accent=DHL_RED, sub=f"> {age_hours:g}h or no signal"),
        kpi_dict("Status unknown", f"{unknown:,}", accent="#999", border_accent="#9CA3AF"),
        kpi_dict(alarms_kpi_label(), f"{total_alarms:,}", accent=DHL_YELLOW, border_accent=DHL_YELLOW),
        kpi_dict("Devices alarming", f"{devices_with_alarm:,}", accent=DHL_RED, border_accent="#DC2626"),
    ]

    if mix_integration_enabled():
        mix_df = get_mix_health_cached()
        if mix_df is None:
            kpis.append(kpi_dict("MiX assets", "…", accent="#F59E0B", sub="loading"))
        elif mix_df.empty:
            kpis.append(kpi_dict("MiX assets", "0", accent="#F59E0B"))
        else:
            flagged = int((mix_df["IssueCount"] > 0).sum()) if "IssueCount" in mix_df.columns else 0
            kpis.append(
                kpi_dict("MiX assets", f"{len(mix_df):,}", accent="#F59E0B", sub=f"{flagged:,} with issues")
            )

    charts: list[str] = []
    if rt is not None and not rt.empty:
        charts.append(figure_html(C.online_offline_pie(rt, age_hours)))
        charts.append(figure_html(C.status_type_donut(rt)))
    elif devices is not None:
        if vss_err and rt is None:
            charts.append(figure_html(C.loading_fig("Live status unavailable — use Refresh data")))
            charts.append(figure_html(C.loading_fig("Live status unavailable — use Refresh data")))
        else:
            charts.append(figure_html(C.loading_fig("Fetching live status…")))
            charts.append(figure_html(C.loading_fig("Fetching live status…")))
    else:
        charts.append(figure_html(C.EMPTY_FIG))
        charts.append(figure_html(C.EMPTY_FIG))

    if alarms is not None and not alarms.empty:
        charts.append(figure_html(C.top_devices_by_alarms(alarms, top_n=10)))
        charts.append(figure_html(C.alarm_type_pie(alarms)))
    elif devices is not None or rt is not None:
        if vss_err and alarms is None:
            charts.append(figure_html(C.loading_fig("Alarms unavailable — use Refresh data")))
            charts.append(figure_html(C.loading_fig("Alarms unavailable — use Refresh data")))
        else:
            charts.append(figure_html(C.loading_fig("Loading alarm history…")))
            charts.append(figure_html(C.loading_fig("Loading alarm history…")))
    else:
        charts.append(figure_html(C.EMPTY_FIG))
        charts.append(figure_html(C.EMPTY_FIG))

    return {
        "title": "Fleet Overview",
        "subtitle": "Live snapshot of DHL fleet health — devices, status, and alarms.",
        "banners": banners,
        "kpis": kpis,
        "charts": charts,
        "age_hours": age_hours,
    }


def realtime_context(
    *,
    age_hours: float = 6.0,
    fleets: list[str] | None = None,
    statuses: list[str] | None = None,
    ignitions: list[str] | None = None,
    ch_filter: str = "all",
    chart: str = "online_pie",
) -> dict[str, Any]:
    df = get_realtime_cached()
    fleets = _parse_multi(fleets)
    statuses = _parse_multi(statuses)
    ignitions = _parse_multi(ignitions)

    fleet_opts: list[str] = []
    status_opts: list[str] = []
    ignition_opts: list[str] = []
    if df is not None and not df.empty:
        fleet_opts = _fleet_options(df=df)
        status_opts = sorted({str(x) for x in df["StatusType"].dropna() if str(x).strip()})
        if "Ignition" in df.columns:
            ignition_opts = sorted({str(x) for x in df["Ignition"].dropna() if str(x).strip()})
    else:
        fleet_opts = _fleet_options()

    if df is None:
        devices = get_dhl_devices_cached()
        dev_count = len(devices) if devices is not None else 0
        kpis = [
            kpi_dict("Devices (cached)", f"{dev_count:,}", border_accent="#3B82F6"),
        ] if dev_count else []
        return {
            "title": "Real-Time Device Status",
            "subtitle": "Most recent reported state for every DHL device.",
            "loading": True,
            "kpis": kpis,
            "chart_html": _vss_unavailable_fig("Fetching live device status"),
            "table_html": df_to_table_html(None),
            "fleet_opts": fleet_opts,
            "status_opts": status_opts,
            "ignition_opts": ignition_opts,
            "fleets": fleets,
            "statuses": statuses,
            "ignitions": ignitions,
            "ch_filter": ch_filter,
            "chart": chart,
            "age_hours": age_hours,
        }

    f = _filter_realtime(df, fleets=fleets, statuses=statuses, ignitions=ignitions, ch_filter=ch_filter)
    age = pd.to_numeric(f.get("AgeHours"), errors="coerce") if not f.empty else pd.Series(dtype=float)
    total = len(f)
    online = int((age.notna() & (age <= age_hours)).sum()) if total else 0
    offline = int((age.notna() & (age > age_hours)).sum()) if total else 0
    unknown = int(age.isna().sum()) if total else 0
    video_lost = int((f.get("NotRecordingFlag", pd.Series(dtype=str)) == "Not Working").sum()) if total else 0

    kpis = [
        kpi_dict("Devices shown", f"{total:,}", border_accent="#3B82F6"),
        kpi_dict("Online", f"{online:,}", accent="#2E8B57", border_accent="#2E8B57"),
        kpi_dict("Offline", f"{offline:,}", accent=DHL_RED, border_accent=DHL_RED),
        kpi_dict("Video lost (ch)", f"{video_lost:,}", accent=DHL_YELLOW, border_accent=DHL_YELLOW),
        kpi_dict("Status unknown", f"{unknown:,}", accent="#999", border_accent="#9CA3AF"),
    ]

    chart_map = {
        "online_pie": lambda: C.online_offline_pie(f, age_hours),
        "status_donut": lambda: C.status_type_donut(f),
        "modules": lambda: C.module_health_bar(f),
        "channels": lambda: C.channel_health_bar(f),
        "age_hist": lambda: C.age_hours_histogram(f),
        "signal_box": lambda: C.signal_box_by_status(f),
    }
    fig = chart_map.get(chart, chart_map["online_pie"])()

    table_cols = [
        "DeviceName",
        "DeviceID",
        "Fleet",
        "StatusType",
        "AgeHours",
        "Ignition",
        "MobileNetwork",
        "GPSModule",
        "NotRecordingFlag",
        "devVoltage",
        "batVoltage",
        "MobileSignalStrength",
    ]
    table_cols = [c for c in table_cols if c in f.columns]

    return {
        "title": "Real-Time Device Status",
        "subtitle": "Most recent reported state for every DHL device.",
        "loading": False,
        "kpis": kpis,
        "chart_html": figure_html(fig),
        "table_html": df_to_table_html(f, table_cols),
        "fleet_opts": fleet_opts,
        "status_opts": status_opts,
        "ignition_opts": ignition_opts,
        "fleets": fleets,
        "statuses": statuses,
        "ignitions": ignitions,
        "ch_filter": ch_filter,
        "chart": chart,
        "age_hours": age_hours,
    }


def alarms_context(
    *,
    fleets: list[str] | None = None,
    alarm_types: list[str] | None = None,
    chart: str = "type_pie",
) -> dict[str, Any]:
    df = get_alarms_cached()
    fleets = _parse_multi(fleets)
    alarm_types = _parse_multi(alarm_types)

    fleet_opts: list[str] = []
    type_opts: list[str] = []
    if df is not None and not df.empty:
        fleet_opts = _fleet_options(df=df)
        type_opts = sorted({str(x) for x in df["AlarmName"].dropna() if str(x).strip()})
    else:
        fleet_opts = _fleet_options()

    if df is None:
        return {
            "title": "Alarms — Last 24 hours",
            "subtitle": "Every alarm event raised across the DHL fleet.",
            "loading": True,
            "kpis": [],
            "chart_html": _vss_unavailable_fig("Fetching alarms"),
            "table_html": df_to_table_html(None),
            "fleet_opts": fleet_opts,
            "type_opts": type_opts,
            "fleets": fleets,
            "alarm_types": alarm_types,
            "chart": chart,
        }

    f = _filter_alarms(df, fleets=fleets, alarm_types=alarm_types)
    total = int(len(f))
    devices_with_alarm = int(f["DeviceID"].nunique()) if total else 0
    distinct_types = int(f["AlarmName"].nunique()) if total else 0
    last_seen = "-"
    if total and "AlarmTime" in f.columns and pd.notna(f["AlarmTime"].max()):
        last_seen = f["AlarmTime"].max().strftime("%Y-%m-%d %H:%M:%S")

    kpis = [
        kpi_dict("Alarm events", f"{total:,}", border_accent="#3B82F6"),
        kpi_dict("Devices alarming", f"{devices_with_alarm:,}", accent=DHL_RED, border_accent=DHL_RED),
        kpi_dict("Distinct alarm types", f"{distinct_types:,}", accent=DHL_YELLOW, border_accent=DHL_YELLOW),
        kpi_dict("Most recent event", last_seen, accent="#3B3B3B", border_accent="#6B7280"),
    ]

    chart_map = {
        "type_pie": lambda: C.alarm_type_pie(f),
        "per_hour": lambda: C.alarms_per_hour_line(f),
        "top_devices": lambda: C.top_devices_by_alarms(f),
        "heatmap": lambda: C.fleet_alarm_heatmap(f),
        "map": lambda: C.alarm_map(f),
    }
    fig = chart_map.get(chart, chart_map["type_pie"])()

    table_cols = ["AlarmTime", "DeviceName", "DeviceID", "Fleet", "AlarmName", "Speed", "PlateNo", "Lat", "Lon"]
    return {
        "title": "Alarms — Last 24 hours",
        "subtitle": "Every alarm event raised across the DHL fleet.",
        "loading": False,
        "kpis": kpis,
        "chart_html": figure_html(fig),
        "table_html": df_to_table_html(f, table_cols),
        "fleet_opts": fleet_opts,
        "type_opts": type_opts,
        "fleets": fleets,
        "alarm_types": alarm_types,
        "chart": chart,
    }


def device_context(*, device_id: str | None = None) -> dict[str, Any]:
    rt_df = get_realtime_cached()
    devices = get_dhl_devices_cached()
    alarms = get_alarms_cached()

    options: list[dict] = []
    if rt_df is not None and not rt_df.empty:
        src = rt_df[["DeviceID", "DeviceName", "Fleet"]].fillna("").astype(str).drop_duplicates()
    elif devices is not None and not devices.empty:
        src = devices[["DeviceID", "DeviceName", "Fleet"]].fillna("").astype(str).drop_duplicates()
    else:
        src = pd.DataFrame(columns=["DeviceID", "DeviceName", "Fleet"])

    if not src.empty:
        src = src.sort_values(["Fleet", "DeviceName"])
        for _, row in src.iterrows():
            options.append(
                {
                    "id": row["DeviceID"],
                    "label": f"{row['DeviceName']} ({row['DeviceID']}) — {row['Fleet']}",
                }
            )

    rt_row = None
    a_dev = pd.DataFrame()
    kpis: list[dict] = []
    faults: list[dict] = []
    chart_html = figure_html(C.EMPTY_FIG)
    table_html = df_to_table_html(None)

    if device_id:
        if rt_df is not None and not rt_df.empty:
            match = rt_df[rt_df["DeviceID"].astype(str) == str(device_id)]
            if not match.empty:
                rt_row = match.iloc[0]
        if alarms is not None and not alarms.empty:
            a_dev = alarms[alarms["DeviceID"].astype(str) == str(device_id)].copy()

        dev_row = None
        if devices is not None and not devices.empty:
            dev_match = devices[devices["DeviceID"].astype(str) == str(device_id)]
            if not dev_match.empty:
                dev_row = dev_match.iloc[0]

        if rt_row is not None:
            kpis.append(kpi_dict("Status", str(rt_row.get("StatusType") or "Unknown")))
            kpis.append(kpi_dict("Fleet", str(rt_row.get("Fleet") or "-")))
            age = rt_row.get("AgeHours")
            if pd.notna(age):
                kpis.append(kpi_dict("Age (hours)", f"{float(age):.1f}"))
            faults = _build_faults(rt_row, a_dev)
        elif dev_row is not None:
            kpis.append(kpi_dict("Device", str(dev_row.get("DeviceName") or device_id)))
            kpis.append(kpi_dict("Fleet", str(dev_row.get("Fleet") or "-")))
            kpis.append(kpi_dict("Device ID", str(dev_row.get("DeviceID") or device_id)))
            vss_err = last_vss_error()
            if vss_err:
                faults.append({"label": "Live status unavailable (VSS session expired)", "ok": False})
            elif rt_df is None:
                faults.append({"label": "Live status still loading", "ok": True})
        elif not a_dev.empty:
            kpis.append(kpi_dict("Alarms (24h)", f"{len(a_dev):,}", accent=DHL_RED))

        if not a_dev.empty:
            chart_html = figure_html(
                C.top_devices_by_alarms(
                    a_dev.assign(DeviceName=a_dev.get("DeviceName", device_id)),
                    top_n=min(10, len(a_dev)),
                )
            )
            table_cols = ["AlarmTime", "AlarmName", "Speed", "Lat", "Lon"]
            table_html = df_to_table_html(a_dev, [c for c in table_cols if c in a_dev.columns])
        elif dev_row is not None and rt_row is None:
            vss_err = last_vss_error()
            if vss_err:
                table_html = '<p class="muted-msg">Alarm history unavailable — VSS session expired. Click Refresh data.</p>'
            elif alarms is None:
                table_html = '<p class="muted-msg">Loading alarm history from VSS…</p>'
            else:
                table_html = '<p class="muted-msg">No alarms in the last 24 hours for this device.</p>'

    return {
        "title": "Device Drilldown",
        "subtitle": "Pick a vehicle to see current faults and 24h alarm history.",
        "device_id": device_id or "",
        "device_options": options,
        "kpis": kpis,
        "faults": faults,
        "chart_html": chart_html,
        "table_html": table_html,
    }


def _build_faults(rt_row: pd.Series, a_dev: pd.DataFrame) -> list[dict]:
    faults: list[dict] = []
    modules = [
        ("Mobile network", rt_row.get("MobileNetwork") == "Working"),
        ("GPS", rt_row.get("GPSModule") == "Working"),
        ("G-Sensor", rt_row.get("GsensorModule") == "Working"),
        ("Wi-Fi", rt_row.get("WifiModule") == "Working"),
        ("Video lost (ch)", rt_row.get("NotRecordingFlag") == "Working"),
    ]
    for label, ok in modules:
        faults.append({"label": f"{label}: {'OK' if ok else 'FAULT'}", "ok": ok})

    video_lost = parse_channels(str(rt_row.get("videoloststateFormatter") or ""))
    if video_lost:
        faults.append(
            {
                "label": f"Video lost on {', '.join(f'CH{c}' for c in sorted(set(video_lost)))}",
                "ok": False,
            }
        )

    status = str(rt_row.get("StatusType") or "")
    if status and status != "Normal":
        faults.append({"label": f"Status: {status}", "ok": False})

    if a_dev is not None and not a_dev.empty:
        top = a_dev["AlarmName"].value_counts().head(3)
        for name, count in top.items():
            faults.append({"label": f"Alarm: {name} ({count}x)", "ok": False})

    return faults


def mix_context(*, issues: list[str] | None = None) -> dict[str, Any]:
    issues = _parse_multi(issues)
    if not mix_integration_enabled():
        return {
            "title": "MiX Telematics",
            "subtitle": "MiX integration is disabled.",
            "enabled": False,
            "notice": "Set MIX_ENABLED=1 and add accounts.json, then restart the dashboard.",
            "kpis": [],
            "chart_html": "",
            "table_html": "",
            "issue_opts": [],
            "issues": issues,
        }

    df = get_mix_health_cached()
    mix_err = last_mix_error()
    if df is None:
        return {
            "title": "MiX Telematics",
            "subtitle": "DHL assets on MiX — health flags and diagnostics.",
            "enabled": True,
            "notice": mix_err or "",
            "loading": not mix_err,
            "kpis": [kpi_dict("MiX assets", "…", accent="#F59E0B", border_accent="#F59E0B", sub="loading")] if not mix_err else [],
            "chart_html": figure_html(C.loading_fig("Loading MiX health…")) if not mix_err else "",
            "table_html": df_to_table_html(None) if not mix_err else "",
            "issue_opts": [],
            "issues": issues,
        }

    f = df.copy()
    if issues and "Issues" in f.columns:
        mask = f["Issues"].astype(str).apply(lambda s: any(i in s for i in issues))
        f = f[mask]

    flagged = int((f["IssueCount"] > 0).sum()) if "IssueCount" in f.columns and not f.empty else 0
    kpis = [
        kpi_dict("Assets shown", f"{len(f):,}", border_accent="#3B82F6"),
        kpi_dict("With issues", f"{flagged:,}", accent=DHL_RED if flagged else "#2E8B57", border_accent=DHL_RED if flagged else "#2E8B57"),
    ]

    issue_opts: list[str] = []
    if "Issues" in df.columns:
        for raw in df["Issues"].dropna().astype(str):
            for part in raw.split(";"):
                part = part.strip()
                if part and part not in issue_opts:
                    issue_opts.append(part)
        issue_opts.sort()

    chart_html = figure_html(C.EMPTY_FIG)
    if not f.empty and "IssueCount" in f.columns:
        import plotly.express as px

        if "Issues" in f.columns:
            rows = []
            for _, row in f.iterrows():
                for issue in str(row.get("Issues") or "").split(";"):
                    issue = issue.strip()
                    if issue:
                        rows.append({"Issue": issue})
            if rows:
                counts = pd.DataFrame(rows)["Issue"].value_counts().reset_index()
                counts.columns = ["Issue", "Count"]
                fig = px.bar(counts, x="Issue", y="Count", color_discrete_sequence=[DHL_RED])
                fig.update_layout(margin=dict(l=20, r=20, t=40, b=80))
                chart_html = figure_html(fig)

    show_cols = [c for c in ["AssetName", "Registration", "IssueCount", "Issues", "LastSeen"] if c in f.columns]
    return {
        "title": "MiX Telematics",
        "subtitle": "DHL assets on MiX — health flags and diagnostics.",
        "enabled": True,
        "notice": "",
        "loading": False,
        "kpis": kpis,
        "chart_html": chart_html,
        "table_html": df_to_table_html(f, show_cols),
        "issue_opts": issue_opts,
        "issues": issues,
    }

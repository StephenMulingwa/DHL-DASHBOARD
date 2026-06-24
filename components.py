"""Reusable Plotly figure builders + small UI helpers for the DHL dashboard."""

from __future__ import annotations

from typing import Iterable

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from data import RT_VIDEO_LOST_CHANNEL_MAX, parse_channels

DHL_RED = "#D40511"
DHL_YELLOW = "#FFCC00"
DHL_GRAY = "#3B3B3B"
CHART_BLUE = "#2563EB"
CHART_GREEN = "#2E8B57"
CHART_ORANGE = "#F97316"
CHART_PURPLE = "#8B5CF6"
CHART_CYAN = "#06B6D4"
CHART_SLATE = "#64748B"
COLORWAY = [CHART_BLUE, DHL_RED, CHART_GREEN, DHL_YELLOW, CHART_PURPLE, CHART_ORANGE, CHART_CYAN, CHART_SLATE]

DEFAULT_LAYOUT = dict(
    margin=dict(l=42, r=26, t=68, b=52),
    paper_bgcolor="#FFFFFF",
    plot_bgcolor="#FFFFFF",
    colorway=COLORWAY,
    font=dict(family="Inter, Segoe UI, Arial, sans-serif", size=12, color="#334155"),
    title_font=dict(size=17, color="#0F172A"),
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.04,
        xanchor="right",
        x=1,
        font=dict(size=11),
        itemwidth=30,
    ),
    hoverlabel=dict(bgcolor="#0F172A", font=dict(color="#FFFFFF", size=12)),
)

EMPTY_FIG = go.Figure().update_layout(
    **DEFAULT_LAYOUT,
    annotations=[dict(text="No data", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font=dict(size=18, color="#999"))],
    xaxis=dict(visible=False),
    yaxis=dict(visible=False),
)


def loading_fig(message: str) -> go.Figure:
    """Placeholder chart while VSS data is still being fetched."""
    return go.Figure().update_layout(
        **DEFAULT_LAYOUT,
        title=dict(text="", font=dict(size=1)),
        annotations=[
            dict(
                text=message,
                x=0.5,
                y=0.5,
                xref="paper",
                yref="paper",
                showarrow=False,
                font=dict(size=15, color="#6B7280"),
            )
        ],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )


# Online / offline

def online_offline_pie(rt_df: pd.DataFrame, age_hours_threshold: float) -> go.Figure:
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG

    age = pd.to_numeric(rt_df.get("AgeHours"), errors="coerce")
    online_mask = age.notna() & (age <= age_hours_threshold)
    offline_mask = age.notna() & (age > age_hours_threshold)
    unknown_mask = age.isna()

    counts = pd.Series(
        {
            "Online": int(online_mask.sum()),
            "Offline": int(offline_mask.sum()),
            "Status Unknown": int(unknown_mask.sum()),
        }
    )
    counts = counts[counts > 0]
    if counts.empty:
        return EMPTY_FIG

    fig = px.pie(
        names=counts.index,
        values=counts.values,
        hole=0.55,
        color=counts.index,
        color_discrete_map={"Online": "#2E8B57", "Offline": DHL_RED, "Status Unknown": "#999"},
    )
    fig.update_traces(textposition="inside", textinfo="percent+label", marker=dict(line=dict(color="#FFFFFF", width=2)))
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title=f"Online vs Offline (Online = last seen <= {age_hours_threshold}h)",
    )
    return fig


def status_type_donut(rt_df: pd.DataFrame) -> go.Figure:
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG
    counts = rt_df["StatusType"].fillna("Status Unknown").value_counts()
    fig = px.pie(names=counts.index, values=counts.values, hole=0.6)
    fig.update_traces(textposition="inside", textinfo="percent+label", marker=dict(line=dict(color="#FFFFFF", width=2)))
    fig.update_layout(**DEFAULT_LAYOUT, title="Detailed StatusType distribution")
    return fig


# Module health

MODULE_COLS = ["MobileNetwork", "GPSModule", "GsensorModule", "WifiModule", "NotRecordingFlag"]
MODULE_LABELS = {
    "MobileNetwork": "Mobile",
    "GPSModule": "GPS",
    "GsensorModule": "G-Sensor",
    "WifiModule": "Wi-Fi",
    "NotRecordingFlag": "Video lost (ch)",
}


def module_health_bar(rt_df: pd.DataFrame) -> go.Figure:
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG

    rows: list[dict] = []
    for col in MODULE_COLS:
        if col not in rt_df.columns:
            continue
        s = rt_df[col].fillna("Unknown")
        for state, n in s.value_counts().items():
            rows.append({"Module": MODULE_LABELS[col], "State": state or "Unknown", "Count": int(n)})

    if not rows:
        return EMPTY_FIG

    df = pd.DataFrame(rows)
    fig = px.bar(
        df,
        x="Module",
        y="Count",
        color="State",
        barmode="stack",
        color_discrete_map={"Working": "#2E8B57", "Not Working": DHL_RED, "Unknown": "#999"},
    )
    fig.update_layout(**DEFAULT_LAYOUT, title="Module health (Working vs Not Working)")
    return fig


# Camera channels

def channel_health_bar(
    rt_df: pd.DataFrame, *, channels: Iterable[int] | None = None
) -> go.Figure:
    """Per channel CH1..CH4: Working / Video lost (``videoloststateFormatter``) / Camera covered (mask)."""
    if channels is None:
        channels = tuple(range(1, RT_VIDEO_LOST_CHANNEL_MAX + 1))
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG

    if "videoloststateFormatter" in rt_df.columns:
        _vl = rt_df["videoloststateFormatter"].astype(str)
    else:
        _vl = pd.Series([""] * len(rt_df), index=rt_df.index)
    if "videomaskstateFormatter" in rt_df.columns:
        _mk = rt_df["videomaskstateFormatter"].astype(str)
    else:
        _mk = pd.Series([""] * len(rt_df), index=rt_df.index)
    video_lost_lists = _vl.apply(parse_channels)
    masked_lists = _mk.apply(parse_channels)

    rows = []
    for ch in channels:
        video_lost = sum(ch in s for s in video_lost_lists)
        masked = sum(ch in s for s in masked_lists)
        total = len(rt_df)
        any_problem = sum((ch in vl) or (ch in mk) for vl, mk in zip(video_lost_lists, masked_lists))
        working = max(0, total - any_problem)
        rows.append({"Channel": f"CH{ch}", "State": "Working", "Count": working})
        rows.append({"Channel": f"CH{ch}", "State": "Video lost", "Count": video_lost})
        rows.append({"Channel": f"CH{ch}", "State": "Camera covered", "Count": masked})

    df = pd.DataFrame(rows)
    df = df[df["Count"] > 0]
    if df.empty:
        return EMPTY_FIG

    fig = px.bar(
        df,
        x="Channel",
        y="Count",
        color="State",
        barmode="stack",
        color_discrete_map={
            "Working": "#2E8B57",
            "Video lost": "#FF8C00",
            "Camera covered": DHL_YELLOW,
        },
    )
    fig.update_layout(**DEFAULT_LAYOUT, title="Camera channel health (video lost vs covered, per device)")
    return fig


def age_hours_histogram(rt_df: pd.DataFrame) -> go.Figure:
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG
    s = pd.to_numeric(rt_df.get("AgeHours"), errors="coerce").dropna()
    if s.empty:
        return EMPTY_FIG
    fig = px.histogram(s, nbins=40, color_discrete_sequence=[DHL_RED])
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title="How stale is the latest status? (hours since last report)",
        xaxis_title="AgeHours",
        yaxis_title="Devices",
        showlegend=False,
    )
    return fig


def signal_box_by_status(rt_df: pd.DataFrame) -> go.Figure:
    if rt_df is None or rt_df.empty or "signalValue" not in rt_df.columns:
        return EMPTY_FIG
    df = rt_df.dropna(subset=["signalValue"]).copy()
    df["signalValue"] = pd.to_numeric(df["signalValue"], errors="coerce")
    df = df.dropna(subset=["signalValue"])
    if df.empty:
        return EMPTY_FIG
    fig = px.box(df, x="StatusType", y="signalValue", color="StatusType", points="suspectedoutliers")
    fig.update_layout(**DEFAULT_LAYOUT, title="Mobile signal by StatusType", showlegend=False)
    return fig


def top_fleets_by_faults(rt_df: pd.DataFrame, *, age_hours_threshold: float, top_n: int = 10) -> go.Figure:
    if rt_df is None or rt_df.empty:
        return EMPTY_FIG
    age = pd.to_numeric(rt_df.get("AgeHours"), errors="coerce")
    faulty = rt_df[(rt_df["StatusType"].fillna("") != "Normal") | (age > age_hours_threshold)].copy()
    if faulty.empty:
        return EMPTY_FIG
    counts = faulty["Fleet"].fillna("Unknown").value_counts().head(top_n)
    fig = px.bar(x=counts.values, y=counts.index, orientation="h", color_discrete_sequence=[DHL_RED])
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title=f"Top {top_n} fleets by faulty devices",
        xaxis_title="Devices with fault",
        yaxis_title="",
        yaxis=dict(autorange="reversed"),
    )
    return fig


# Alarms

def alarm_type_pie(alarms_df: pd.DataFrame) -> go.Figure:
    if alarms_df is None or alarms_df.empty:
        return EMPTY_FIG
    counts = alarms_df["AlarmName"].fillna("Unknown").value_counts()
    fig = px.pie(names=counts.index, values=counts.values, hole=0.45)
    fig.update_traces(textposition="inside", textinfo="percent+label", marker=dict(line=dict(color="#FFFFFF", width=2)))
    fig.update_layout(**DEFAULT_LAYOUT, title="Alarms by type (last 24h)")
    return fig


def alarms_per_hour_line(alarms_df: pd.DataFrame) -> go.Figure:
    if alarms_df is None or alarms_df.empty:
        return EMPTY_FIG
    df = alarms_df.dropna(subset=["AlarmTime"]).copy()
    if df.empty:
        return EMPTY_FIG
    df["Hour"] = df["AlarmTime"].dt.floor("h")
    grouped = df.groupby(["Hour", "AlarmName"]).size().reset_index(name="Count")
    fig = px.area(grouped, x="Hour", y="Count", color="AlarmName")
    fig.update_layout(**DEFAULT_LAYOUT, title="Alarms per hour (last 24h)", xaxis_title="", yaxis_title="Alarms")
    return fig


def top_devices_by_alarms(alarms_df: pd.DataFrame, *, top_n: int = 20) -> go.Figure:
    if alarms_df is None or alarms_df.empty:
        return EMPTY_FIG
    counts = (
        alarms_df.groupby(["DeviceName", "DeviceID"]).size().reset_index(name="Alarms")
        .sort_values("Alarms", ascending=False).head(top_n)
    )
    if counts.empty:
        return EMPTY_FIG
    counts["Label"] = counts["DeviceName"].fillna("") + "  (" + counts["DeviceID"].astype(str) + ")"
    fig = px.bar(counts, x="Alarms", y="Label", orientation="h", color_discrete_sequence=[DHL_RED])
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title=f"Top {top_n} devices by alarm count",
        xaxis_title="Alarms",
        yaxis_title="",
        yaxis=dict(autorange="reversed"),
    )
    return fig


def fleet_alarm_heatmap(alarms_df: pd.DataFrame) -> go.Figure:
    if alarms_df is None or alarms_df.empty:
        return EMPTY_FIG
    pivot = (
        alarms_df.groupby(["Fleet", "AlarmName"]).size().reset_index(name="Count")
        .pivot(index="Fleet", columns="AlarmName", values="Count").fillna(0)
    )
    if pivot.empty:
        return EMPTY_FIG
    pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).head(25).index]
    fig = px.imshow(
        pivot.values,
        x=list(pivot.columns),
        y=list(pivot.index),
        color_continuous_scale="Reds",
        aspect="auto",
        text_auto=True,
    )
    fig.update_layout(**DEFAULT_LAYOUT, title="Fleet x Alarm Type (heatmap)", xaxis_title="", yaxis_title="")
    return fig


def mix_positions_map(pos_df: pd.DataFrame) -> go.Figure:
    if pos_df is None or pos_df.empty:
        return EMPTY_FIG
    df = pos_df.copy()
    df["Lat"] = pd.to_numeric(df.get("Latitude"), errors="coerce")
    df["Lon"] = pd.to_numeric(df.get("Longitude"), errors="coerce")
    df["SpeedKmhNum"] = pd.to_numeric(df.get("SpeedKmh"), errors="coerce")
    df = df.dropna(subset=["Lat", "Lon"])
    df = df[(df["Lat"].between(-90, 90)) & (df["Lon"].between(-180, 180))]
    if df.empty:
        return EMPTY_FIG

    label = df.get("AssetName", pd.Series(dtype=str)).astype(str)
    reg = df.get("Registration", pd.Series(dtype=str)).astype(str)
    df["MapLabel"] = label.where(label.str.strip().ne(""), reg)

    fig = px.scatter_map(
        df,
        lat="Lat",
        lon="Lon",
        color="SpeedKmhNum",
        hover_name="MapLabel",
        hover_data={
            "Registration": True,
            "Address": True,
            "SpeedKmh": True,
            "Rpm": True,
            "EventTime": True,
            "Lat": False,
            "Lon": False,
            "MapLabel": False,
            "SpeedKmhNum": False,
        },
        zoom=5,
    ) if hasattr(px, "scatter_map") else px.scatter_mapbox(
        df,
        lat="Lat",
        lon="Lon",
        color="SpeedKmhNum",
        hover_name="MapLabel",
        hover_data={
            "Registration": True,
            "Address": True,
            "SpeedKmh": True,
            "Rpm": True,
            "EventTime": True,
            "Lat": False,
            "Lon": False,
            "MapLabel": False,
            "SpeedKmhNum": False,
        },
        zoom=5,
    )
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title="MiX asset locations (tacho speed)",
        map_style="open-street-map" if hasattr(px, "scatter_map") else None,
        mapbox_style="open-street-map" if not hasattr(px, "scatter_map") else None,
        height=600,
    )
    return fig


def alarm_map(alarms_df: pd.DataFrame) -> go.Figure:
    if alarms_df is None or alarms_df.empty:
        return EMPTY_FIG
    df = alarms_df.dropna(subset=["Lat", "Lon"]).copy()
    df = df[(df["Lat"].between(-90, 90)) & (df["Lon"].between(-180, 180))]
    if df.empty:
        return EMPTY_FIG

    fig = px.scatter_map(
        df,
        lat="Lat",
        lon="Lon",
        color="AlarmName",
        hover_name="DeviceName",
        hover_data={"AlarmTime": True, "Fleet": True, "Speed": True, "Lat": False, "Lon": False},
        zoom=5,
    ) if hasattr(px, "scatter_map") else px.scatter_mapbox(
        df,
        lat="Lat",
        lon="Lon",
        color="AlarmName",
        hover_name="DeviceName",
        hover_data={"AlarmTime": True, "Fleet": True, "Speed": True, "Lat": False, "Lon": False},
        zoom=5,
    )
    fig.update_layout(
        **DEFAULT_LAYOUT,
        title="Alarm locations (last 24h)",
        map_style="open-street-map" if hasattr(px, "scatter_map") else None,
        mapbox_style="open-street-map" if not hasattr(px, "scatter_map") else None,
        height=600,
    )
    return fig

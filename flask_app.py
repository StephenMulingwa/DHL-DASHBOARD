"""DHL Fleet Health Dashboard — Flask application."""

from __future__ import annotations

import logging
import os

import neon_meta_store
import operation_log
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for

APP_BUILD = "flask-dhl-2026-06"
DEFAULT_PORT = 8050


def _load_env_file(path: str) -> None:
    env_log = logging.getLogger("dhl-flask")
    if not os.path.isfile(path):
        env_log.warning("No .env file at %s — using process environment / defaults only", path)
        return
    if os.path.getsize(path) == 0:
        env_log.warning(".env exists but is empty (0 bytes) — save your credentials in the editor and restart")
        return
    loaded = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            os.environ[key] = val
            loaded += 1
    env_log.info("Loaded %s variables from .env", loaded)


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dhl-flask")

for _target, _source in (
    ("VSS_BASE_URL", "BASE_URL"),
    ("VSS_USERNAME", "USERNAME"),
    ("VSS_PASSWORD", "PASSWORD"),
):
    if not os.environ.get(_target) and os.environ.get(_source):
        os.environ[_target] = os.environ[_source]

from data import (  # noqa: E402
    bust_cache_for_refresh,
    cache_freshness,
    cache_get,
    cache_latest_data_iso,
    cache_needs_hydration,
    hydrate_cache_from_neon,
    last_bust_cache_iso,
    last_saved_refresh_display,
    mix_integration_enabled,
)
from vss_client import (  # noqa: E402
    active_base_url,
    last_vss_error,
    last_vss_profile,
    last_vss_token_source,
)
from web.auth import login_required, verify_login  # noqa: E402
from web.prewarm import prewarm_cache_sync, start_background_workers  # noqa: E402
from web.views import (  # noqa: E402
    alarms_context,
    device_context,
    logs_context,
    mix_context,
    nav_items,
    overview_context,
    realtime_context,
)

if os.environ.get("DHL_DASH_ACCESS_LOG", "0").strip().lower() not in ("1", "true", "yes", "on"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder="assets", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dhl-dev-change-me-in-production")


@app.before_request
def enforce_canonical_host():
    if request.host.split(":", 1)[0].lower() == "dhl-dashboard-mauve.vercel.app":
        return redirect(f"https://vss-mix.vercel.app{request.full_path.rstrip('?')}", code=308)
    return None


@app.before_request
def hydrate_dashboard_cache():
    if not session.get("logged_in"):
        return None
    path = request.path or ""
    if not path.startswith("/dashboard") and not path.startswith("/api/"):
        return None
    if path.startswith("/api/logs") or path in ("/api/logout", "/api/refresh"):
        return None
    if cache_needs_hydration():
        try:
            hydrate_cache_from_neon()
        except Exception as exc:  # noqa: BLE001
            log.warning("hydrate from Neon failed: %s", exc)
    return None


@app.route("/google9da283bb173f5d68.html")
def google_site_verification():
    return send_from_directory(os.path.dirname(__file__), "google9da283bb173f5d68.html")


def _mix_enabled() -> bool:
    return mix_integration_enabled()


def _layout_context(*, active: str, **extra):
    ctx = {
        "active_tab": active,
        "nav_items": nav_items(active=active, mix_enabled=_mix_enabled()),
        "mix_enabled": _mix_enabled(),
        "app_build": APP_BUILD,
        "username": session.get("username", ""),
        "ui_poll_ms": max(250, int(os.environ.get("DHL_UI_POLL_MS", "5000") or "5000")),
        "vss_error": last_vss_error(),
        "vss_profile": last_vss_profile(),
        "vss_base_url": active_base_url(),
        "awaiting_vss": cache_get("realtime_status") is None,
        "last_refresh_display": last_saved_refresh_display(),
    }
    ctx.update(extra)
    return ctx


@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard_overview"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_login(username, password):
            session["logged_in"] = True
            session["username"] = username.strip()
            log_sid = operation_log.start_session("login", username=username.strip())
            session["log_session_id"] = log_sid
            operation_log.end_session(log_sid, "ok", message="Signed in")
            nxt = request.args.get("next") or url_for("dashboard_overview")
            return redirect(nxt)
        error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error,
        logo_exists=True,
        hero_exists=True,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True, "redirect": url_for("login")})


@app.route("/dashboard")
@login_required
def dashboard_overview():
    age = request.args.get("age_hours", "6")
    try:
        age_hours = float(age)
    except ValueError:
        age_hours = 6.0
    ctx = overview_context(age_hours=age_hours)
    return render_template("pages/overview.html", **_layout_context(active="overview", **ctx))


@app.route("/dashboard/realtime")
@login_required
def dashboard_realtime():
    age = request.args.get("age_hours", "6")
    try:
        age_hours = float(age)
    except ValueError:
        age_hours = 6.0
    ctx = realtime_context(
        age_hours=age_hours,
        fleets=request.args.getlist("fleet"),
        statuses=request.args.getlist("status"),
        ignitions=request.args.getlist("ignition"),
        ch_filter=request.args.get("ch_filter", "all"),
        chart=request.args.get("chart", "online_pie"),
    )
    return render_template("pages/realtime.html", **_layout_context(active="realtime", **ctx))


@app.route("/dashboard/alarms")
@login_required
def dashboard_alarms():
    ctx = alarms_context(
        fleets=request.args.getlist("fleet"),
        alarm_types=request.args.getlist("alarm_type"),
        chart=request.args.get("chart", "type_pie"),
    )
    return render_template("pages/alarms.html", **_layout_context(active="alarms", **ctx))


@app.route("/dashboard/device")
@login_required
def dashboard_device():
    device_id = request.args.get("device_id", "").strip() or None
    ctx = device_context(device_id=device_id)
    return render_template("pages/device.html", **_layout_context(active="device", **ctx))


@app.route("/dashboard/mix")
@login_required
def dashboard_mix():
    if not _mix_enabled():
        return redirect(url_for("dashboard_overview"))
    ctx = mix_context(issues=request.args.getlist("issue"))
    return render_template("pages/mix.html", **_layout_context(active="mix", **ctx))


@app.route("/dashboard/logs")
@login_required
def dashboard_logs():
    ctx = logs_context()
    return render_template("pages/logs.html", **_layout_context(active="logs", **ctx))


@app.route("/api/logs")
@login_required
def api_logs():
    since = request.args.get("since", "0")
    try:
        since_id = max(0, int(since))
    except ValueError:
        since_id = 0
    limit = request.args.get("limit", "100")
    try:
        limit_n = max(1, min(500, int(limit)))
    except ValueError:
        limit_n = 100
    events = operation_log.fetch_events(since_id=since_id, limit=limit_n)
    return jsonify({
        "events": events,
        "latest_id": operation_log.latest_event_id(),
        "sessions": operation_log.fetch_sessions(limit=15),
    })


@app.route("/api/logs/sessions")
@login_required
def api_logs_sessions():
    return jsonify({"sessions": operation_log.fetch_sessions(limit=20)})


@app.route("/api/cache/status")
@login_required
def api_cache_status():
    def _count(key: str) -> int | None:
        value = cache_get(key)
        try:
            return int(len(value)) if value is not None else None
        except TypeError:
            return None

    return jsonify(
        {
            "freshness": cache_freshness(),
            "counts": {
                "dhl_devices": _count("dhl_devices"),
                "realtime_status": _count("realtime_status"),
                "alarms_24h": _count("alarms_24h"),
                "mix_health": _count("mix_health"),
            },
            "latest_data": cache_latest_data_iso(),
            "last_refresh": last_saved_refresh_display(),
            "last_bust": last_bust_cache_iso(),
            "vss_token_source": last_vss_token_source(),
            "vss_profile": last_vss_profile(),
            "vss_base_url": active_base_url(),
            "vss_error": last_vss_error(),
        }
    )


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    bust_cache_for_refresh(keep_devices=False)
    username = session.get("username", "")
    log_sid = operation_log.start_session("refresh", username=username)
    session["log_session_id"] = log_sid
    operation_log.set_current_session(log_sid)
    try:
        prewarm_cache_sync(stale_while_revalidate=False)
        counts = hydrate_cache_from_neon()
        neon_meta_store.record_last_refresh(
            counts=counts,
            username=username,
            session_id=log_sid,
        )
        from data import last_mix_error

        mix_err = last_mix_error()
        end_msg = f"Refresh complete — {counts}"
        end_status = "ok"
        if mix_err:
            end_msg = f"VSS data saved. MiX failed: {mix_err[:180]}"
        operation_log.end_session(log_sid, end_status, message=end_msg)
        return jsonify({
            "ok": True,
            "message": end_msg,
            "counts": counts,
            "last_refresh": neon_meta_store.last_refresh_display(),
            "session_id": log_sid,
            "mix_error": mix_err or None,
        })
    except Exception as exc:  # noqa: BLE001
        operation_log.end_session(log_sid, "error", message=f"Refresh failed: {exc}")
        return jsonify({"ok": False, "error": str(exc), "session_id": log_sid}), 500
    finally:
        operation_log.set_current_session(None)


start_background_workers()


if __name__ == "__main__":
    host = os.environ.get("DHL_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DHL_DASH_PORT", str(DEFAULT_PORT)))
    debug = os.environ.get("DHL_DASH_DEBUG", "0") == "1"
    log.info("DHL Fleet Health (Flask) — http://%s:%s/login", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)

"""DHL Fleet Health Dashboard — Flask application."""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

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
    last_bust_cache_iso,
    mix_integration_enabled,
)
from vss_client import (  # noqa: E402
    active_base_url,
    ensure_token,
    last_vss_error,
    last_vss_profile,
    last_vss_token_source,
    try_token_without_login,
)
from web.auth import login_required, verify_login  # noqa: E402
from web.prewarm import start_background_workers  # noqa: E402
from web.views import (  # noqa: E402
    alarms_context,
    device_context,
    mix_context,
    nav_items,
    overview_context,
    realtime_context,
)

if os.environ.get("DHL_DASH_ACCESS_LOG", "0").strip().lower() not in ("1", "true", "yes", "on"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__, static_folder="assets", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dhl-dev-change-me-in-production")


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
            try:
                ensure_token(login_max_wait_seconds=45)
            except Exception as exc:  # noqa: BLE001
                if not try_token_without_login():
                    log.warning("login: VSS token warm-up failed: %s", exc)
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


@app.route("/api/cache/status")
@login_required
def api_cache_status():
    return jsonify(
        {
            "freshness": cache_freshness(),
            "latest_data": cache_latest_data_iso(),
            "last_refresh": last_bust_cache_iso(),
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
    from web.prewarm import prewarm_cache

    prewarm_cache(stale_while_revalidate=True)
    return jsonify({"ok": True, "message": "Refresh started in background."})


start_background_workers()


if __name__ == "__main__":
    host = os.environ.get("DHL_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DHL_DASH_PORT", str(DEFAULT_PORT)))
    debug = os.environ.get("DHL_DASH_DEBUG", "0") == "1"
    log.info("DHL Fleet Health (Flask) — http://%s:%s/login", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)

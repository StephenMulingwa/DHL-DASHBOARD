"""Flask session authentication for the dashboard gate."""

from __future__ import annotations

import os
from functools import wraps

from flask import redirect, request, session, url_for


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


def dashboard_username() -> str:
    return _env("DHL_DASH_USERNAME", "admin")


def dashboard_password() -> str:
    return _env("DHL_DASH_PASSWORD", "dhl")


def verify_login(username: str, password: str) -> bool:
    return username.strip() == dashboard_username() and password == dashboard_password()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            nxt = request.path
            if request.query_string:
                nxt = f"{nxt}?{request.query_string.decode('utf-8', errors='replace')}"
            return redirect(url_for("login", next=nxt))
        return view(*args, **kwargs)

    return wrapped

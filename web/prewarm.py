"""Background VSS/MiX cache prewarm (ported from app.py)."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import nullcontext

from data import (
    cache_get,
    load_alarms_last_24h,
    load_dhl_devices,
    load_mix_health,
    mix_integration_enabled,
    refresh_alarms_last_24h,
    refresh_dhl_devices,
    refresh_mix_health,
    refresh_realtime_status,
    load_realtime_status,
)
from vss_client import (
    active_base_url,
    ensure_token,
    get_current_token,
    keepalive_ping,
    last_vss_error,
    last_vss_profile,
    refresh_token_if_expired,
    try_token_without_login,
    validate_or_renew_token,
    vss_no_login_mode,
    _has_persisted_token,
)

log = logging.getLogger("dhl-flask")

try:
    REFRESH_MINUTES = max(1, int(os.environ.get("DHL_AUTO_REFRESH_MINUTES", "60").strip() or "60"))
except ValueError:
    REFRESH_MINUTES = 60

KEEPALIVE_MINUTES = 20
TOKEN_CHECK_MINUTES = 30

_prewarm_mix_lock = threading.Lock()
_prewarm_mix_running = False
_prewarm_mix_pending = False
_prewarm_vss_lock = threading.Lock()
_prewarm_vss_running = False
_prewarm_vss_pending = False


def _prewarm_mix(*, stale_while_revalidate: bool) -> None:
    if not mix_integration_enabled():
        return
    load_fn = refresh_mix_health if stale_while_revalidate else load_mix_health
    label = "background refresh" if stale_while_revalidate else "startup"
    try:
        log.info("prewarm: mix_health starting (%s)", label)
        load_fn()
        log.info("prewarm: mix_health done")
    except Exception as e:  # noqa: BLE001
        log.warning("prewarm: mix_health failed: %s", e)


def _prewarm_vss(*, keep_devices: bool, stale_while_revalidate: bool) -> None:
    token_ctx = vss_no_login_mode() if stale_while_revalidate else nullcontext()
    with token_ctx:
        if stale_while_revalidate:
            cached = get_current_token() or try_token_without_login()
            if not cached:
                log.warning("prewarm: refresh — no in-memory VSS token; skipping VSS reload")
                return
            token, _pid = cached
            log.info("prewarm: VSS token %s... (reuse in-memory)", token[:12])
        else:
            try:
                cached = try_token_without_login()
                if cached:
                    token, _pid = cached
                    log.info(
                        "prewarm: VSS token %s... (file/env) profile=%s base=%s",
                        token[:12],
                        last_vss_profile() or "?",
                        active_base_url(),
                    )
                elif not _has_persisted_token():
                    token, _pid = ensure_token(login_max_wait_seconds=120, allow_10082_retry=True)
                    log.info(
                        "prewarm: VSS login OK profile=%s base=%s token=%s...",
                        last_vss_profile() or "?",
                        active_base_url(),
                        token[:12],
                    )
                else:
                    log.warning(
                        "prewarm: .vss_token.json exists but unreadable — fix token file or set VSS_ALLOW_API_LOGIN=1"
                    )
                    return
            except Exception as e:  # noqa: BLE001
                err = last_vss_error() or str(e)
                log.warning("prewarm: VSS token unavailable: %s", err)
                return

        ok, msg = validate_or_renew_token(allow_reauth=not stale_while_revalidate)
        if not ok:
            log.warning("prewarm: VSS token check: %s", msg)
            return
        else:
            log.info("prewarm: VSS token validated")

        load_devices = refresh_dhl_devices if stale_while_revalidate else load_dhl_devices
        load_realtime = refresh_realtime_status if stale_while_revalidate else load_realtime_status
        load_alarms = refresh_alarms_last_24h if stale_while_revalidate else load_alarms_last_24h

        need_devices = cache_get("dhl_devices") is None or (stale_while_revalidate and not keep_devices)
        if need_devices:
            try:
                log.info("prewarm: dhl_devices starting")
                load_devices()
                log.info("prewarm: dhl_devices done")
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: dhl_devices failed: %s", e)
        else:
            log.info("prewarm: dhl_devices already cached")

        for name, fn in (("realtime_status", load_realtime), ("alarms_last_24h", load_alarms)):
            try:
                log.info("prewarm: %s starting", name)
                fn()
                log.info("prewarm: %s done", name)
            except Exception as e:  # noqa: BLE001
                log.warning("prewarm: %s failed: %s", name, e)


def _kick_mix_prewarm(*, stale_while_revalidate: bool = False) -> None:
    global _prewarm_mix_running, _prewarm_mix_pending

    with _prewarm_mix_lock:
        if _prewarm_mix_running:
            _prewarm_mix_pending = True
            return
        _prewarm_mix_running = True

    def _runner() -> None:
        global _prewarm_mix_running, _prewarm_mix_pending
        pending = False
        swr = stale_while_revalidate
        try:
            _prewarm_mix(stale_while_revalidate=swr)
        finally:
            with _prewarm_mix_lock:
                _prewarm_mix_running = False
                pending = _prewarm_mix_pending
                _prewarm_mix_pending = False
            if pending:
                _kick_mix_prewarm(stale_while_revalidate=swr)

    threading.Thread(target=_runner, daemon=True, name="dhl-prewarm-mix").start()


def _kick_vss_prewarm(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    global _prewarm_vss_running, _prewarm_vss_pending

    with _prewarm_vss_lock:
        if _prewarm_vss_running:
            _prewarm_vss_pending = True
            return
        _prewarm_vss_running = True

    def _runner() -> None:
        global _prewarm_vss_running, _prewarm_vss_pending
        pending = False
        kd = keep_devices
        swr = stale_while_revalidate
        try:
            _prewarm_vss(keep_devices=kd, stale_while_revalidate=swr)
        finally:
            with _prewarm_vss_lock:
                _prewarm_vss_running = False
                pending = _prewarm_vss_pending
                _prewarm_vss_pending = False
            if pending:
                _kick_vss_prewarm(keep_devices=kd, stale_while_revalidate=swr)

    threading.Thread(target=_runner, daemon=True, name="dhl-prewarm-vss").start()


def prewarm_cache(*, keep_devices: bool = False, stale_while_revalidate: bool = False) -> None:
    _kick_mix_prewarm(stale_while_revalidate=stale_while_revalidate)
    _kick_vss_prewarm(keep_devices=keep_devices, stale_while_revalidate=stale_while_revalidate)


def _keepalive_loop() -> None:
    interval = max(60, KEEPALIVE_MINUTES * 60)
    while True:
        try:
            time.sleep(interval)
            ok = keepalive_ping(allow_reauth=False)
            tok = get_current_token()
            tok_str = (tok[0][:12] + "...") if tok else "(no token)"
            log.info("keepalive: token %s alive=%s", tok_str, ok)
        except Exception as e:  # noqa: BLE001
            log.warning("keepalive: %s", e)


def _token_refresh_loop() -> None:
    interval = max(300, TOKEN_CHECK_MINUTES * 60)
    while True:
        try:
            time.sleep(interval)
            if refresh_token_if_expired():
                log.info("token scheduler: proactive refresh completed")
        except Exception as e:  # noqa: BLE001
            log.warning("token scheduler: %s", e)


def _auto_refresh_loop() -> None:
    interval = max(60, REFRESH_MINUTES * 60)
    while True:
        try:
            time.sleep(interval)
            log.info("auto-refresh: busting caches (every %s min)", REFRESH_MINUTES)
            from data import bust_cache_for_refresh

            bust_cache_for_refresh(keep_devices=True)
            prewarm_cache(keep_devices=True, stale_while_revalidate=True)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-refresh: %s", e)


def start_background_workers() -> None:
    prewarm_cache()
    threading.Thread(target=_keepalive_loop, daemon=True, name="dhl-keepalive").start()
    threading.Thread(target=_token_refresh_loop, daemon=True, name="dhl-token-refresh").start()
    threading.Thread(target=_auto_refresh_loop, daemon=True, name="dhl-auto-refresh").start()

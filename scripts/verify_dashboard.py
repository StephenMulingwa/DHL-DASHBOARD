"""End-to-end verify: VSS token, devices, realtime, MiX health."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _load_env() -> None:
    for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()


def main() -> int:
    _load_env()
    from vss_client import (
        _token_is_live,
        active_base_url,
        ensure_token,
        last_vss_error,
        last_vss_profile,
    )

    print("=== VSS token check ===")
    try:
        token, _pid = ensure_token(force=True)
    except Exception as exc:
        print("ensure_token(file) failed:", exc)
        token = None

    if token and not _token_is_live(token):
        print("Stored token not live — apiLogin via configured profile order (up to 10 min)")
        try:
            token, _pid = ensure_token(
                force=True,
                skip_file=True,
                login_max_wait_seconds=600,
                allow_10082_retry=True,
            )
        except Exception as exc:
            print("apiLogin failed:", last_vss_error() or exc)
            return 1

    if not token or not _token_is_live(token):
        print("VSS token still not live:", last_vss_error())
        return 1

    print(
        "VSS OK profile=%s base=%s live=1"
        % (last_vss_profile(), active_base_url())
    )

    print("\n=== VSS data load ===")
    from data import bust_cache_for_refresh, load_dhl_devices, load_mix_health, load_realtime_status

    bust_cache_for_refresh(keep_devices=False)
    t0 = time.time()
    dev = load_dhl_devices()
    print("devices:", len(dev), "in %.0fs" % (time.time() - t0))
    if len(dev) < 100:
        print("WARN: expected ~350 devices")
        return 1

    t0 = time.time()
    rt = load_realtime_status()
    print("realtime:", len(rt), "in %.0fs" % (time.time() - t0))
    if len(rt) and "StatusType" in rt.columns:
        print("status:", rt["StatusType"].value_counts().head(5).to_dict())
    unknown = int((rt.get("StatusType", []) == "Status Unknown").sum()) if len(rt) else 0
    if unknown == len(rt) and len(rt) > 0:
        print("FAIL: all status unknown — no live VSS realtime")
        return 1

    print("\n=== MiX health ===")
    try:
        mix = load_mix_health()
        print("mix assets:", len(mix))
        if len(mix) == 0:
            print("WARN: no MiX assets")
            return 1
    except Exception as exc:
        print("MiX failed:", exc)
        return 1

    print("\n=== ALL OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

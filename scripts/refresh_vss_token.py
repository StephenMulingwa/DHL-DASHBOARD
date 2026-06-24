"""Wait for VSS 10082 cooldown, apiLogin once, then verify data loads."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    os.environ[k.strip()] = v.strip()

WAIT_SEC = int(os.environ.get("VSS_REFRESH_WAIT_SEC", "600") or "600")


def main() -> int:
    print(f"Waiting {WAIT_SEC}s with no login attempts (VSS 10082 cooldown)...")
    time.sleep(WAIT_SEC)

    from vss_client import (
        _api_login,
        _credential_profiles,
        _mark_10082,
        _save_token_to_file,
        _set_active_profile,
        _token_is_live,
    )

    profiles = _credential_profiles()
    for profile in profiles:
        print(f"Trying apiLogin: {profile.name} @ {profile.base_url}")
        try:
            j = _api_login(profile)
        except Exception as exc:
            print("  connection error:", exc)
            continue
        status = j.get("status")
        print("  status:", status, j.get("msg"))
        if status == 10082:
            _mark_10082()
            continue
        if status == 10000 and isinstance(j.get("data"), dict):
            data = j["data"]
            tok = str(data.get("token") or "")
            pid = str(data.get("pid") or "")
            if not tok:
                continue
            _set_active_profile(profile)
            _save_token_to_file(tok, pid, base_url=profile.base_url, profile=profile.name)
            print("  saved token live=", _token_is_live(tok))
            if _token_is_live(tok):
                return 0
        print("  failed")

    print("All profiles failed — paste token+pid into .vss_token.json manually")
    return 1


if __name__ == "__main__":
    sys.exit(main())

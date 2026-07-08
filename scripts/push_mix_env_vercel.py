"""Push MIX_ACCOUNTS_JSON from local .env to Vercel production."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

sys.path.insert(0, str(ROOT / "scripts"))
from push_vss_token_vercel import _load_dotenv, upsert_vercel_env  # noqa: E402


def main() -> int:
    _load_dotenv(ROOT / ".env")
    raw = os.environ.get("MIX_ACCOUNTS_JSON", "").strip()
    if not raw:
        print("ERROR: MIX_ACCOUNTS_JSON is not set in .env", file=sys.stderr)
        return 1
    upsert_vercel_env(key="MIX_ACCOUNTS_JSON", value=raw)
    for key in ("MIX_ENABLED", "MIX_SERVER_KEY", "MIX_GROUP_IDS", "MIX_GROUP_NAME_CONTAINS"):
        val = os.environ.get(key, "").strip()
        if val:
            upsert_vercel_env(key=key, value=val)
    print("Done — MiX env vars pushed to Vercel production")
    return 0


if __name__ == "__main__":
    sys.exit(main())

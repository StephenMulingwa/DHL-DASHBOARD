# DHL Fleet Health Dashboard

Flask web dashboard that pulls **live** data from the VSS API and shows the
real-time health and alarms for the DHL fleet.

Pages:

- **Overview** — KPIs, online vs offline pie, status breakdown, top fleets by faults, alarm types.
- **Real-Time Status** — per-device live state, module health, camera-channel health, voltages, signal.
- **Alarms (24h)** — alarm-type pie, per-hour trend, top devices, fleet-by-type heatmap, locations on a map.
- **Device Drilldown** — pick one device to see its current state and last 24h alarm history.
- **MiX Health** (optional) — MiX telematics asset health when `MIX_ENABLED=1`.

## Quick start

1. Create a virtualenv and install requirements:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set credentials:

   | Variable | Purpose |
   |----------|---------|
   | `VSS_BASE_URL`, `VSS_USERNAME`, `VSS_PASSWORD` | Primary VSS server login |
   | `VSS_BASE_URL_N`, `VSS_USERNAME_N`, `VSS_PASSWORD_N` | Secondary profile (tried if primary fails) |
   | `DHL_DASH_USERNAME`, `DHL_DASH_PASSWORD` | Dashboard sign-in (default `admin` / `dhl`) |
   | `FLASK_SECRET_KEY` | Session secret (required in production) |

   The app auto-discovers which VSS profile works (same pattern as `howen_vss_api.ipynb`) and stores the token for 22 hours.

   For shared deployments, set `NEON_DB_URL`. The app creates the `vss_tokens` table automatically on startup, or you can run `scripts/neon_vss_tokens.sql` once in Neon. The active VSS token/PID is stored in Neon; local JSON token fallback is disabled when `NEON_DB_URL` is configured.

3. Run:

   ```powershell
   python flask_app.py
   ```

   Open **http://127.0.0.1:8050/login**

## VSS token caching

- Successful VSS login writes the active `token`, `pid`, `issued_at`, `base_url`, and `profile` to Neon when `NEON_DB_URL` is configured.
- Reused for all API calls until **22 hours** (`VSS_TOKEN_TTL_HOURS`) or session expiry.
- On dashboard login/startup, if the stored token is older than the TTL, the app generates a fresh VSS token, overwrites the Neon row, and uses that token for data loading.
- Optional without Neon only: set `VSS_TOKEN` / `VSS_PID` in `.env` to skip `apiLogin`.
- HTTPS controltech hosts use `verify=False` by default (set `VSS_SSL_VERIFY=1` to enable certificate verification).

## Project layout

```
DHL-DASHBOARD/
  flask_app.py          # Flask app (main entry)
  vss_client.py         # Multi-profile VSS client + token store
  data.py               # Cached DataFrame loaders
  components.py         # Plotly figure builders
  web/                  # Auth, views, background prewarm
  templates/            # Login + dashboard pages
  assets/               # DHL branding + flask-theme.css
  howen_vss_api.ipynb   # VSS API reference notebook
```

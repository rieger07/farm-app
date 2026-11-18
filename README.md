# Ironclad Farm & Ledger Suite (`farm-app`)

A three-container platform built by **Ironclad Systems LLC** on a shared SQLite database (`FARM_DB_PATH`):

1. **Homestead Planner (`app.py`, port `8501`)** — FastAPI + Jinja2 livestock batch, phased feed, harvest, and $/lb economics planner.
2. **Iron Ledger (`inventory/`, port `8600`)** — Multi-tenant inventory ledger, automatic Zettle POS sync, offline-capable Quick Sale, per-unit & per-pound selling, and Stripe SaaS billing.
3. **Ironclad Systems Site (`site/`, port `8080`)** — Lightweight `nginx:alpine` static website for `www.ironcladsystems.dev` (services, products, contact, and SaaS legal/policy pages).

---

## 1. Homestead Planner (`app.py`)

FastAPI + Jinja2 web app (zero Streamlit/pandas runtime) for managing livestock batches from arrival/incubation through processing and freezer reconciliation:

- **Radar (`/`)**: Active batch summary cards, 14-day upcoming milestone checklist, and **1-tap quick-log modals** for **Loss**, **Harvest**, and **Actual Feed**.
- **Batches (`/batches`, `/batches/{id}`)**: Create/edit/archive/delete batches, log dated losses, harvests, customer sales, home freezer allocations, and export `.ics` / Google Calendar events.
- **Feed (`/feed`)**: Configurable barrel capacity & bag weight, next-buy alert, purchasing schedule, and weekly/monthly feed demand tables.
- **Financials (`/financials`)**: Cost vs. estimated revenue bars, unified batch economics & effective freezer `$/lb`, and ROI breakdown.
- **Settings (`/settings`)**: Custom species presets, JSON backup export/restore, password management, and admin user/registration controls.

---

## 2. Iron Ledger (`inventory/`)

Multi-tenant farm & market-stand POS and inventory system (`ledger.ironcladsystems.dev`):

- **Inventory (`/inventory`)**:
  - Mobile-first card layout + desktop table with search, category, status, and **Needs attention** filter.
  - **Update modal**: Set exact count, restock, relative adjustment, low-stock threshold, recent ledger activity history, and **Show on Quick Sale screen** visibility toggle.
  - **Three selling & stocking modes**:
    - `unit` — Priced and stocked per unit (jars, candles, etc.).
    - `lb_stock_unit` — Priced `$/lb`, stocked by whole units (e.g., whole chickens/turkeys: each weighed bird decrements `1` unit from stock).
    - `lb_stock_lb` — Priced `$/lb`, stocked in `lb` (e.g., bulk meat/produce: decimal counts & decrements).
- **Quick Sale (`/quick-sale`)**:
  - Offline-capable cash register (`localStorage` queue + service worker) with live `X left` / `X lb left` stock hints.
  - Additive weight modal (`+0.5`, `+1`, `+1.5`, `+2 lb` + decimal input) for per-pound items.
- **Sales & CSV (`/sales`, `/sales/export.csv`)**: Unified Zettle + Cash sales, daily/weekly/custom charts, and formula-safe CSV export (`payment_type` set to `cash` for Quick Sale cash orders).
- **Zettle Sync (`/settings`, `/zettle/*`)**: Background polling every `ZETTLE_POLL_SECONDS` (default `300s`), manual sync, and OAuth tokens encrypted at rest (`enc1:` HMAC-SHA256 + SHA-256 CTR via `INVENTORY_SECRET_KEY` or `.inventory_key`).
- **Onboarding & Multi-Tenancy (`/admin`, `/signup`, `/invite/*`, `/billing`)**:
  - Interactive 6-step **`? Tour`** walkthrough across Inventory, Sales, Quick Sale, and Settings.
  - Role-based access (`owner`, `manager`, `staff`), one-time invite links, and Stripe subscription billing (supporting Stripe `2025+` / `2026-10-28.endive` webhook payloads).

---

## 3. Ironclad Systems Website (`site/`)

Static `nginx:alpine` container serving `www.ironcladsystems.dev` with clean extensionless routes:

- `/` (Home), `/services` (B2B/C2C consulting & contracting), `/products` (Iron Ledger & Homestead Planner), `/contact` (`steve.rieger.dev@gmail.com` + copy/mailto/optional form endpoint), `/privacy`, `/terms`, `/refunds`, and `/healthz`.

---

## Project Structure

```text
├── app.py                        # Homestead Planner FastAPI entrypoint (port 8501)
├── Dockerfile                    # Alpine Python 3.11 + uvicorn for Homestead Planner
├── Dockerfile.inventory          # Alpine Python 3.11 + uvicorn for Iron Ledger (port 8600)
├── Dockerfile.site               # nginx:alpine for www.ironcladsystems.dev (port 8080)
├── docker-compose.yml            # Local multi-container compose setup
├── requirements-inventory.txt    # Shared FastAPI/uvicorn/Jinja2/httpx dependencies
├── homestead/                    # Homestead domain, pure-Python scheduling/financials & templates
├── inventory/                    # Iron Ledger routes, crypto, Zettle sync, Stripe billing & UI
├── site/                         # Static HTML/CSS/JS + nginx.conf for Ironclad Systems LLC
└── tests/                        # Unit & integration tests (unittest + FastAPI TestClient)
```

---

## Running Locally & Testing

```bash
# Install runtime dependencies
pip install -r requirements-inventory.txt

# Run Homestead Planner (http://localhost:8501)
FARM_COOKIE_SECURE=0 uvicorn app:app --host 0.0.0.0 --port 8501

# Run Iron Ledger (http://localhost:8600)
INVENTORY_COOKIE_SECURE=0 uvicorn inventory.app:app --host 0.0.0.0 --port 8600

# Run full test suite
env PYTHONPATH=.deps python3 -W ignore -m unittest discover -s tests -t .
```

## Environment Variables (`farm-inventory`)

| Variable | Purpose |
|---|---|
| `FARM_DB_PATH` | SQLite database path (default `/app/data/homestead.db`) |
| `INVENTORY_TZ` | Display timezone (default `America/New_York`) |
| `INVENTORY_COOKIE_SECURE` | `1` behind HTTPS reverse proxy, `0` for local HTTP |
| `INVENTORY_SECRET_KEY` | Optional key for encrypting Zettle OAuth tokens at rest |
| `ZETTLE_CLIENT_ID` / `ZETTLE_CLIENT_SECRET` / `ZETTLE_REDIRECT_URI` | Zettle OAuth credentials |
| `ZETTLE_POLL_SECONDS` | Background Zettle sync interval in seconds (default `300`) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe API & `/webhooks/stripe` signing secret |
| `STRIPE_PRICE_MONTHLY` / `STRIPE_PRICE_ANNUAL` | Stripe `price_...` IDs for subscription plans |
| `STRIPE_DISPLAY_MONTHLY` / `STRIPE_DISPLAY_ANNUAL` | Display labels on `/signup` (e.g. `$15 / month`) |
| `STRIPE_TRIAL_DAYS` | Optional free trial length in days |

## Docker & CI Releases

Publishing a GitHub Release triggers [.github/workflows/docker-image.yml](.github/workflows/docker-image.yml), building and pushing:
- `asralis/farm-app:latest` (`Dockerfile`)
- `asralis/farm-inventory:latest` (`Dockerfile.inventory`)
- `asralis/ironclad-site:latest` (`Dockerfile.site`)

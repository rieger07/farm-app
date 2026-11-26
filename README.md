# Ironclad Farm & Ledger Suite (`farm-app`)

A three-container platform built by **Ironclad Systems LLC** on a shared SQLite database (`FARM_DB_PATH`):

1. **Homestead Planner (`app.py`, port `8501`)** — FastAPI + Jinja2 livestock batch, phased feed, live Harvest Tally, Google Drive receipt uploads, and `$/lb` economics planner.
2. **Iron Ledger (`inventory/`, port `8600`)** — Multi-tenant farmers-market inventory ledger, automatic Zettle POS sync, offline-capable Quick Sale, Marketplaces & Insights analytics, and Stripe SaaS billing.
3. **Ironclad Systems Site (`site/`, port `8080`)** — Lightweight `nginx:alpine` static website for `www.ironcladsystems.dev` (services, products, Resend-backed contact form, and legal/policy pages).

---

## 1. Homestead Planner (`app.py`)

FastAPI + Jinja2 web app (zero Streamlit/pandas runtime) for managing livestock batches from arrival, incubation, or breeding through harvest and freezer reconciliation:

- **Public OAuth Pages (`/`, `/home`, `/privacy`, `/terms`)**: Unauthenticated landing page, privacy policy (with Google API Limited Use disclosure), and terms of service hosted on the verified app domain; logged-in users land directly on Radar (`/`).
- **Radar (`/`)**:
  - Live **Active batches** vs. **On-farm head** KPIs (automatically excluding future **Planned** batches and already-harvested head).
  - **Batch lifecycle timeline** (CSS swimlanes with aligned month ticks and `Today` marker).
  - 14-day upcoming milestone checklist with **`✓ Done`** acknowledgment (`batch_milestones_ack`).
  - Active batch cards with 1-tap **Loss**, **Harvest tally**, and **+ Feed** actions, plus a separate **Planned batches** section for future-dated batches with a **`Start now`** button.
- **Harvest Tally (`/batches/{id}/harvest`, `/batches/{id}/harvest.csv`)**:
  - Dedicated bird-by-bird scale entry screen with a pinned live running tally bar (`Harvested X / Y` + progress bar, `Total dressed lbs`, `Avg / Min–Max lb`, `Est. value`).
  - Instant AJAX logging with **Decimal `lb`** and **`lb + oz`** toggle, 1-tap **Undo last bird**, **Copy list**, and **Export CSV**.
- **Batches (`/batches`, `/batches/{id}`)**:
  - Full lifecycle support (`direct_arrival`, `poultry_incubation`, `mammal_breeding` with `days_to_birth`), **Duplicate Batch**, customizable feed/timeline overrides, and **`GCal: Arrival/Incubation/Breeding`** + **`GCal: Process`** + `.ics` calendar links.
  - Dated **Losses**, **Harvests & Processing** (with running totals footer), **Batch Sales & Home Freezer**, and **Feed Purchase & Usage Log** (`batch_feed_logs` with bag auto-calculation).
- **Google Drive Receipts & Schedule F Expenses (`/financials`, `/financials/expenses.csv`, `/settings`)**:
  - Optional Google Drive OAuth (`drive.file` + `userinfo.email` scopes) with encrypted tokens and configurable Drive folder (`Homestead Planner Receipts`).
  - Camera/PDF receipt upload (or manual URL fallback) on Feed Logs, Harvests, and **Financials → Receipts & Expenses (Schedule F)**, plus 1-click **Schedule F CSV** export.
- **Feed (`/feed`) & Financials (`/financials`)**:
  - Configurable barrel capacity & bag weight, purchasing schedule, and weekly/monthly demand.
  - Cost vs. estimated revenue bars, **Include planned / Active only** toggle, unified batch economics, effective freezer `$/lb`, and ROI breakdown.
- **Settings (`/settings`)**: Custom species presets, Google Drive Receipts connection, JSON backup/restore, password management, and admin user/registration controls.

---

## 2. Iron Ledger (`inventory/`)

Multi-tenant farm & farmers-market POS, inventory, and analytics system (`ledger.ironcladsystems.dev`):

- **Inventory (`/inventory`)**:
  - Mobile-first card layout + desktop table with search, category, status, and **Needs attention** filter.
  - **Update modal**: Set exact count, restock, relative adjustment, low-stock threshold, recent ledger history, and **Show on Quick Sale screen** toggle.
  - **Three selling & stocking modes**: `unit`, `lb_stock_unit` (priced `$/lb`, stocked per whole unit), and `lb_stock_lb` (priced & stocked in `lb`).
  - **Non-Inventory / Fee Item Classification**: Mark surcharges/fees (e.g., `Card charge`) in **Settings → Non-Inventory & Fee Items** to exclude them from Inventory, Quick Sale, stock deductions, and transaction unit counts while keeping receipts reconciled.
- **Quick Sale (`/quick-sale`)**:
  - Offline-capable cash register (`localStorage` queue + service worker) with live `X left` / `X lb left` hints, **Hide out of stock** toggle (and bottom-sorted `0 left` items), additive weight modal for per-lb items, fast cash denomination buttons (`Exact`, `$10`, `$20`, `$50`), and a pinned **Sticky Cart Bar**.
- **Sales & CSV (`/sales`, `/sales/export.csv`)**:
  - Compact **2×2 KPI grid** on mobile, **Card vs. Cash** payment breakdown (`Cash` = `Zettle Cash + Quick sale`), stacked daily/weekly chart, `Card`/`Cash` badges on collapsed transaction rows, and Cash/Card/Zettle/Quick sale filtering.
  - **Market day mini-calendar picker** highlighting every date with sales activity (with hover totals & 1-tap **Latest sale day** jump), **Marketplace** filter, and 1-click **Market Day** assignment banner.
- **Insights (`/insights`)**:
  - `Today`, `Yesterday`, `7d`, `30d`, `90d`, `All time`, `Custom`, `Market day` calendar picker, and **Marketplace** filter.
  - **Recommended Cash Drawer**: Per-marketplace starting float & bill/coin breakdown from historical cash sales and product prices (with a 30% morning-rush buffer).
  - Collapsible tables for **Suggested Pack List** (`avg/day × 1.25` or `1.5×` if sold out, stock shortage flags, browser-saved checkboxes, and **Copy pack list**), **Marketplace Comparison** (net after booth + travel costs, avg/day, top item), **Untagged Sale Days**, **Past Market Days**, **Top Products** (volume & revenue), **Early Sell-Out Flags**, and **Frequently Bought Together** pairs.
- **Zettle Sync & Settings (`/settings`, `/zettle/*`)**:
  - Background polling every `ZETTLE_POLL_SECONDS` (default `300s`), manual sync, human-readable relative + local `Last synced` timestamp, dynamic redirect URI, encrypted OAuth tokens (`enc1:`), **Marketplaces** CRUD (name, location, booth fee, travel cost), and **Non-Inventory & Fee Items** management.
- **Onboarding & Multi-Tenancy (`/admin`, `/signup`, `/invite/*`, `/billing`)**:
  - Interactive **`? Tour`** walkthrough, role-based access (`owner`, `manager`, `staff`), one-time invite links, and Stripe subscription billing (`2025+` / `2026-10-28.endive` webhook support).

---

## 3. Ironclad Systems Website (`site/`)

Static `nginx:alpine` container serving `www.ironcladsystems.dev` with brand logos/favicons, `Cache-Control: no-cache` on `.js`/`.css`, and clean extensionless routes:

- `/` (Home), `/services`, `/products` (Iron Ledger & Homestead Planner), `/contact` (Resend-backed `/api/contact` proxied to `farm-inventory:8600/api/contact` with honeypot spam protection), `/privacy`, `/terms`, `/refunds`, and `/healthz`.

---

## Project Structure

```text
├── app.py                        # Homestead Planner FastAPI entrypoint (port 8501)
├── Dockerfile                    # python:3.15-slim + uvicorn for Homestead Planner (port 8501)
├── Dockerfile.inventory          # python:3.15-slim + uvicorn for Iron Ledger (port 8600)
├── Dockerfile.site               # nginx:alpine for www.ironcladsystems.dev (port 8080)
├── docker-compose.yml            # Multi-container compose setup
├── requirements.txt              # Shared FastAPI/uvicorn/Jinja2/httpx dependencies
├── shared/                       # Shared DB connection, migrations, auth, crypto, workspaces & theme
├── homestead/                    # Homestead DB helpers, Google Drive receipts, scheduling, financials & UI
├── inventory/                    # Iron Ledger routes, markets/insights, Zettle sync, Stripe billing & UI
├── site/                         # Static HTML/CSS/JS, brand assets & nginx.conf for Ironclad Systems LLC
└── tests/                        # Unit & integration tests (unittest + FastAPI TestClient)
```

---

## Running Locally & Testing

```bash
# Install runtime dependencies
pip install -r requirements.txt

# Run Homestead Planner (http://localhost:8501)
FARM_COOKIE_SECURE=0 uvicorn app:app --host 0.0.0.0 --port 8501

# Run Iron Ledger (http://localhost:8600)
INVENTORY_COOKIE_SECURE=0 uvicorn inventory.app:app --host 0.0.0.0 --port 8600

# Run full test suite
env PYTHONPATH=.deps python3 -W ignore -m unittest discover -s tests -t .
```

## Environment Variables

### `farm-app` (Homestead Planner)

| Variable | Purpose |
|---|---|
| `FARM_DB_PATH` | SQLite database path (default `/app/data/homestead.db`) |
| `FARM_COOKIE_SECURE` | `1` behind HTTPS reverse proxy, `0` for local HTTP |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Optional Google OAuth client for `drive.file` receipt uploads |
| `GOOGLE_REDIRECT_URI` | Optional explicit callback URL (`https://<farm-domain>/settings/google-drive/callback`) |

### `farm-inventory` (Iron Ledger)

| Variable | Purpose |
|---|---|
| `FARM_DB_PATH` | SQLite database path (default `/app/data/homestead.db`) |
| `INVENTORY_TZ` | Display timezone (default `America/New_York`) |
| `INVENTORY_COOKIE_SECURE` | `1` behind HTTPS reverse proxy, `0` for local HTTP |
| `INVENTORY_SECRET_KEY` | Optional key for encrypting OAuth tokens at rest |
| `APP_BASE_URL` | Public base URL (e.g., `https://ledger.ironcladsystems.dev`) for callback & billing links |
| `ZETTLE_CLIENT_ID` / `ZETTLE_CLIENT_SECRET` / `ZETTLE_REDIRECT_URI` | Zettle OAuth credentials |
| `ZETTLE_POLL_SECONDS` | Background Zettle sync interval in seconds (default `300`) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe API & `/webhooks/stripe` signing secret |
| `STRIPE_PRICE_MONTHLY` / `STRIPE_PRICE_ANNUAL` | Stripe `price_...` IDs for subscription plans |
| `STRIPE_DISPLAY_MONTHLY` / `STRIPE_DISPLAY_ANNUAL` | Display labels on `/signup` (e.g., `$15 / month`) |
| `STRIPE_TRIAL_DAYS` | Optional free trial length in days |
| `RESEND_API_KEY` / `CONTACT_FROM_EMAIL` / `CONTACT_TO_EMAIL` | Resend settings powering `www.ironcladsystems.dev/contact` (`/api/contact`) |

## Docker, CI & Auto-Deploy

Publishing a GitHub Release (strict semver, e.g., `0.27.1`) triggers [.github/workflows/docker-image.yml](.github/workflows/docker-image.yml), which:
1. Builds and pushes all three Docker Hub images (`asralis/farm-app`, `asralis/farm-inventory`, `asralis/ironclad-site`).
2. Connects to the VPS to pull the updated images and restart the container stack.

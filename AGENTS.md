# Repository Guidelines for AI Agents

This repository houses two FastAPI SaaS applications backed by a **single shared SQLite database** (`FARM_DB_PATH=/app/data/homestead.db`, WAL mode) and deployed together via `docker-compose.yml`:

- **Homestead Planner** (`app.py`, `homestead/`) — livestock batch planning, Harvest Tally, Pre-Orders, `/pick/<slug>`, Pickup Day checkout, Daily Eggs & Laying Flock, Feed, Financials.
- **Iron Ledger** (`inventory/`) — Zettle POS sync, Quick Sale (Cash/Venmo/PayPal), Sales analytics, Insights, Market Day Mode.
- **Shared Core** (`shared/`) — canonical database connection, ordered schema migrations, and cross-app bridges.
- **Marketing Site** (`site/`).

---

## Database & Schema Rules (Strict)

1. **Single source of truth for schema (`shared/db/`):**
   - Never add `CREATE TABLE`, `CREATE INDEX`, or `ALTER TABLE` anywhere in `app.py`, `homestead/`, or `inventory/`.
   - All schema definitions and changes live exclusively in `shared/db/migrations/`. This is mechanically enforced by `tests/test_schema_guardrails.py`.
2. **Append-only numbered migrations:**
   - Every schema or data-backfill change must be a new file `shared/db/migrations/XXXX_<short_name>.py` with sequential `VERSION: int`, `NAME: str`, and `upgrade(conn: sqlite3.Connection) -> None`.
   - Never modify an already-shipped migration file; create a new migration instead.
   - Never call `conn.executescript(...)` inside `upgrade(conn)` — use individual `conn.execute(...)` calls so the `BEGIN IMMEDIATE` migration lock stays atomic.
   - Always make migrations safe for existing live production databases (guard `ALTER TABLE` with `PRAGMA table_info(...)` when needed and preserve existing customer/slug data).
3. **Respect table ownership boundaries:**
   - **Core Shared:** `users`, `user_sessions`, `merchants`, `merchant_members`, `merchant_invites`
   - **Homestead Planner:** `user_settings`, `species_presets`, `batches`, `batch_losses`, `batch_harvests`, `batch_sales`, `settings`, `telemetry_events`, `batch_milestones_ack`, `batch_feed_logs`, `google_drive_tokens`, `farm_expenses`, `customers`, `batch_preorders`, `layer_flock_events`, `egg_logs`, `egg_pack_logs`, `layer_feed_logs`
   - **Iron Ledger:** `zettle_connections`, `skus`, `sales`, `sale_items`, `stock_ledger`, `sync_state`, `merchant_subscriptions`, `billing_checkout_invites`, `marketplaces`, `market_days`, `market_day_items`
4. **No raw cross-app writes:**
   - Do not write raw `INSERT`, `UPDATE`, or `DELETE` SQL from `homestead/` against Iron Ledger tables (or from `inventory/` against Homestead tables). Expose or reuse clean helpers in `shared/bridge.py`.

---

## Docker, Production & Release Guardrails

- **Dockerfile parity:** `Dockerfile.inventory` explicitly copies directories (`shared/`, `homestead/`, `inventory/`). Any new top-level runtime package must also be copied there.
- **Protect live public links:** Never alter existing `batches.picking_slug` values or change defaults (`picking_preorder_only=0`, `preorder_enabled=0`) in a way that locks out active public `/pick/<slug>` sales.
- **Releases & versioning:** Merge tested PRs to `main` and tag GitHub releases with bare semver (e.g. `0.41.0`, never `v0.41.0`). Publishing a GitHub release triggers `.github/workflows/docker-image.yml` and auto-deploys to the VPS.
- **Clean commits:** Always `git add` explicit paths; never stage untracked scratch scripts (`fix_inventory.py`, `parse_check.py`) or local DB files.

---

## Verification Before Committing

Run the full test suite (all Homestead, Iron Ledger, billing, sync, and schema guardrail tests):

```bash
PYTHONPATH=.deps python3 -W ignore -m unittest discover -s tests -t .
```

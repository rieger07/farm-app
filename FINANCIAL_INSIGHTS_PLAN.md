# Financial & Operational Insights Roadmap

All farm receipts, feed logs, harvests, egg counts, perennial herds, shearings, mill lots, pre-orders, and Iron Ledger POS sales live in one shared SQLite database (`homestead.db`). This plan outlines how to turn that existing ledger into actionable farm intelligence in three focused phases — without requiring extra data entry.

---

## Connected Data Sources Already Captured

- **Feed & Hay Line Items:** `batch_feed_logs`, `layer_feed_logs`, `herd_feed_logs` (`feed_date`, `vendor`, `feed_type`/`feed_category`, `bags`/`bales`, `weight_lbs`/`feed_lbs`, `cost_total`, `receipt_url`, `receipt_group_id`)
- **Batch-Tagged Expenses & Processing:** `farm_expenses` (`batch_id`, `category`, `vendor`, `amount`, `expense_date`, `receipt_url`), `batch_harvests`, `harvest_shares`, `batch_sales`, `batch_losses`
- **Laying Flock & Eggs:** `egg_logs` (`species`, `log_date`, `total_collected`, `cracked_unusable`, `home_used`), `egg_pack_logs`, and species `ledger_sku_id` links
- **Perennial Herds & Fiber:** `herds`, `herd_animals`, `fiber_shearings` (`prime_lbs`, `seconds_lbs`, `fiber_grade`, `shearing_fee`), `fiber_mill_lots` (`shipping_out_cost`, `milling_fee`, `shipping_back_cost`, `other_handling_fee`), `fiber_lot_outputs` (`ledger_sku_id`)
- **Demand & POS Sales:** `batch_preorders`, `customers`, Iron Ledger `sales` + `sale_items` via `shared/bridge.py`

---

## Phase 1 — Core Price Intelligence, Enterprise P&L, Egg Cost/Dozen & Batch Actuals *(Shipped in `0.58.0`)*

Lives in **Financials & Taxes → Insights** (`/financials?tab=insights`), plus fixes to the **Batch Detail** screen (`/batches/{id}`):

1. **Feed & Hay Unit Cost Trends (`$/lb`, `$/bag`, `$/bale`):**
   - Unified feed ledger combining Meat Batch feed, Laying Flock feed, and Perennial Herd grain/hay/supplements (excluding negative refund lines from unit-price averages).
   - Monthly weighted `$/lb`, `$/50lb bag` equivalent, and `$/bale` (for hay), visualized with a responsive pure-SVG trend chart and a breakdown by feed type (`Starter`, `Grower`, `Layer`, `Scratch`, `Herd Grain`, `Hay`, `Supplement`).
2. **Vendor Price Comparison & Outlier Detection:**
   - Side-by-side spend, total pounds, weighted `$/lb`, bag equivalent, and average `$/bale` by vendor — automatically flagging the lowest-cost grain/feed supplier when multiple named vendors exist.
   - Automatic outlier scan flagging any receipt entry >40% above or below the category median `$/lb` so fat-finger entries (e.g., `5 lb` instead of `50 lb` or `$220` instead of `$22`) surface with a direct link back to the source.
3. **1-Click Preset Calibration Nudges:**
   - Compares trailing 90-day (fallback: latest 6 entries) actual weighted `$/bag` against `Starter`, `Grower`, `Layer`, and `Scratch` preset bag prices.
   - When actual prices drift ≥5% from presets, surfaces a 1-click **Update preset** button (`POST /financials/insights/sync-preset-price`) so forward batch budgets stay accurate.
4. **Enterprise Spend & P&L Breakdown:**
   - Side-by-side cash scorecard across **Meat Batches**, **Laying Flocks & Eggs**, **Breeding Herds**, and **Alpaca & Fiber Herds**, pairing direct feed/processing/mill spend with actual Homestead & year-filtered Iron Ledger POS revenue.
5. **Egg Feed Cost per Dozen by Month:**
   - Combines monthly usable eggs (`egg_logs`) with `layer_feed_logs` to track seasonal **actual feed cost per dozen** (highlighting lowest-cost spring flush vs. winter molt spikes).
6. **Batch-to-Batch Actuals & FCR Table + Batch Detail Screen Fix:**
   - Surfaces `farm_expenses` linked to a batch (`batch_id`) directly on `/batches/{id}` (both `Feed` expenses inside the Feed Log and non-feed batch expenses under *Linked Receipts from Financials*), and folds them into `reconciled_total_cost`.
   - Adds Actual vs. Planned comparisons across all 4 Batch Detail KPI cards (survival %, harvested lbs vs. plan, actual sales vs. estimated revenue, actual cost & `$/lb` vs. budget & plan) and an **Actual FCR** (`logged_feed_lbs ÷ actual_dressed_lbs`) benchmark table inside Insights.

---

## Phase 2 — Perennial Carrying Cost, Alpaca Cohort Curves & Mortality Cost Impact *(Planned)*

1. **Perennial Herd True Break-Even Cost:**
   - **Breeding Pigs:** Trailing 12-month sow/boar grain + hay + herd expenses divided by weaned piglets transferred to grow-out batches (home-farrowed piglet cost vs. buying feeder pigs).
   - **Alpaca Fiber:** Full annual carrying cost (grain + hay + shearing fees + milling + inbound/outbound shipping) divided by total finished skeins/roving produced (**all-in cost per skein**, not just direct mill COGS).
2. **Per-Alpaca Fleece Yield & Grade Aging Curve:**
   - Track each named alpaca's `prime_lbs`, `seconds_lbs`, and `fiber_grade` across annual shearings so you can watch micron grade shifts as animals age and compare fleece yield vs. feed share per head.
   - **Mill Yield % Benchmark:** Compare finished oz returned ÷ raw lbs sent across mills, colors (`White`, `Fawn`, `Grey`, etc.), and Huacaya vs. Suri lots.
3. **Mortality Timing & Wasted-Feed Impact:**
   - Attribute sunk feed consumed up to `loss_date` on `batch_losses` so late-stage losses (e.g., week-16 turkey) are separated from early brooder losses (`$/loss` financial impact).
4. **Home Freezer Value Tracker:**
   - Annual summary of total home-freezer lbs kept (meat + home-used eggs), actual out-of-pocket effective `$/lb`, and equivalent farm-gate retail value.

---

## Phase 3 — Margin-Target Pricing, Demand Sizing & Seasonal Cash-Flow Forecast *(Planned)*

1. **Dynamic Target-Margin Price Calculator:**
   - Feed trailing 90-day `$/lb` feed prices, actual FCR, survival %, and recent butcher processing fees into a pre-season pricing recommender (*"Price needed per lb to clear 35% margin on Cornfed Turkeys"*).
2. **Customer & Pre-Order Demand Sizing:**
   - Combine `batch_preorders` waitlists, customer repeat-buyer rates, and share size mix (`1/2` vs. `whole` pigs) to recommend next season's batch headcount.
3. **Monthly Cash-Flow Seasonality Overlay:**
   - Compare historical monthly actual cash outlay (feed + hay + processing) vs. upcoming planned purchasing schedule (`compute_purchasing_schedule`) to spot seasonal working-capital crunches 60 days ahead.

# Homestead Planner & Iron Ledger — Next-Level Roadmap Ideas

Captured: 2026-11-23

Because **Homestead Planner** and **Iron Ledger** share the same SQLite database (`homestead.db`) and merchant/family workspaces, the biggest advantage is connecting **production (barn/butcher)** directly to **retail (market/porch)** and **taxes**.

---

## 1. Harvest-to-Freezer Bridge & True Net Profit per Market Day

* **The gap today:** Eggs already flow from Homestead into Iron Ledger (`Pack Cartons → Stock`), and whole birds sell via `/pick/<slug>`. Leftover whole birds—or batches processed into retail cuts (breasts, wings, sausage, bacon, chops)—have to be entered into Iron Ledger manually, and Iron Ledger only knows **gross revenue**, not what that meat or dozen eggs cost to raise.
* **What it does:**
  * **1-Click "Send to Iron Ledger Stock"** on `3. Harvest & Pick` (for unsold whole birds or a cut-sheet breakdown) that restocks Iron Ledger SKUs stamped with that batch's exact `reconciled_cost_per_lb_meat` (and `Cost/Dozen` from `/eggs`).
  * **True Margin Analytics in Iron Ledger:** Market Days and Insights show **Net Profit**, not just gross sales: `Gross Sales − Production COGS − Market Booth Fee − Zettle Card Fees`.
* **Why it matters:** Shows to the penny what a market day actually put in your pocket after feed, chicks, butcher fees, booth rent, and card swipe fees.

---

## 2. Live "Porch Cooler & Market Hold" Storefront (`/shop/<farm-slug>`)

* **The gap today:** `/reserve/<slug>` and `/pick/<slug>` are built for a single seasonal batch (like Thanksgiving turkeys). For everyday inventory in Iron Ledger (eggs, frozen cuts, honey, extra birds), customers still have to text *"What do you have available right now?"*
* **What it does:**
  * Toggle `Listed on Farm Stand` on any Iron Ledger SKU.
  * Generates one permanent public link (`/shop/<farm-slug>`) showing live in-stock counts.
  * Customers build a cart, pick **Porch Cooler Pickup** or **Hold for Saturday Market**, and scan/tap existing Venmo/PayPal links.
  * Iron Ledger **immediately holds/deducts the stock** (preventing double-sales at market), pings the team via `ntfy`, and drops the order onto a 1-tap **Pack & Pickup** checklist.
* **Why it matters:** Zero-commission online farm stand powered by the exact same inventory ledger already used at market.

---

## 3. Unified Farm P&L & 1-Click IRS Schedule F Tax Packet

* **The gap today:** Homestead `/financials` only sees direct batch sales and Homestead expenses, while Iron Ledger holds all Zettle POS sales, Quick Sales, card fees, and market booth fees—even though both sit in the same database.
* **What it does:**
  * Merges **Homestead direct sales + Iron Ledger retail sales** alongside **chicks/poults, batch & layer feed, processing receipts, Zettle card fees, market booth fees, and general expenses** into one unified Farm P&L by tax year.
  * Maps every category directly to **IRS Schedule F line items** (Line 2 Livestock/Produce Sales, Line 16 Feed, Line 32 Processing/Booth/Merchant Fees) and exports a single accountant-ready **Schedule F Summary + Receipt Index ZIP/CSV** (complete with Google Drive receipt links).
* **Why it matters:** Turns April tax prep from reconciling three apps and a shoebox into a single click.

---

## 4. Active "Market Day Mode" (Cooler Loadout → Live Break-Even → Closeout) ✅ *Spec Finalized*

> **Priority note:** High immediate value for trial vendor—helps them track exactly what inventory was brought to the market vs. what's left in the booth/cooler during the day.

### Finalized Design Spec (from `/grill-me`)

1. **Virtual Booth Allocation (Zero-Risk Stock Model)**
   * Total `on_hand` in `stock_ledger` is **not** deducted when packing the truck.
   * Each Market Day stores `brought_qty` per SKU (`market_day_items`).
   * **Left at Booth** is computed live: `brought_qty − sold_in_window − write_off_qty`.
   * Healthy unsold items require zero check-in step at the end of the day—they never left `stock_ledger`.

2. **Forgiving Sales Time Window + Pack-Ahead**
   * **One Market Day per date** (`UNIQUE(merchant_id, market_date)`), matching Zettle's single-account sync.
   * **Pack Ahead:** Vendors can build and save Saturday's pack list on Friday night; it goes live automatically on `market_date`.
   * **Sales Window:** Counts all completed Zettle + Quick Sales from **12:00 AM local on `market_date` through `closed_at`** (or end of day while open). Early-bird sales before opening the app are always caught, while evening post-market sales after tapping **Close Out** stay separate. Supports **Reopen Market Day** if closed early by mistake.

3. **Navigation & Role Access**
   * Dedicated **`Market Day` (`/market-day`)** tab in Iron Ledger's main nav + mobile bottom bar.
   * Accessible to **`owner`, `manager`, and `staff`** so booth helpers can pack coolers, trigger a live Zettle sync, check booth counts, and count the cash drawer at wrap-up without needing access to historical `Insights` or `Settings`.

4. **Morning / Night-Before Pack-Out UX**
   * Select `Market Date` + `Marketplace` (auto-fills Booth Fee & Travel Cost, with inline **+ Quick-create marketplace** if none exist yet) + optional **`Starting Cash Float ($)`** (1-tap pre-fill from Recommended Cash Drawer).
   * SKU pack list starts at `0` alongside current `On hand`, with search/category filters and two 1-tap helpers:
     * **Fill Suggested Pack List** (historical demand for that marketplace, capped at `on_hand` when positive)
     * **Bring All On-Hand** (for smaller vendors bringing their full stash)
   * Supports mid-market **`+ Restock booth`** edits if someone brings extra inventory later.

5. **Live Booth Dashboard & Quick Sale Overlay**
   * **On `/market-day`:** Live **Booth Break-Even Meter** (`Gross` vs `Booth + Travel`), **Sell-Through %** (`Sold / Brought`), **1-Tap Sync Zettle Now** button, and live SKU table (`Brought · Sold · Left at Booth`, flagging booth sellouts).
   * **On `/quick-sale` (when today has an open Market Day with packed items):**
     * Compact Market Day status bar at the top.
     * Defaults to **`At Booth`** filter chip with a 1-tap **`All catalog`** toggle—keeps the screen focused on items actually in the tent while never blocking an unpacked item.
     * Product tiles display live **`X at booth`** counts (`left_at_booth − cart_qty`).

6. **1-Tap Closeout Reconciliation & Insights Upgrade**
   * Automatically triggers a fresh Zettle sync (when connected) before finalizing closeout.
   * **Cash Drawer Check:** `Starting Float + Physical Cash Sales = Expected Cash` vs optional `Counted Cash ($)` → `Over / Short`.
   * **Booth Inventory Check:** Shows `Brought · Sold · Expected Left` per packed SKU (zero typing needed when everything matches), with optional **`Write-off / Sample`** or **`Actual Counted Left`** override that records an idempotent `stock_ledger` `adjustment` for comps, breakage, or cooler discrepancies.
   * **Market Scorecard:** Displays `Gross · Net after Booth/Travel · Sell-Through % · Booth Sellouts · Drawer Over/Short` with a 1-tap **Share / Copy Summary** button, and upgrades `Insights` Suggested Pack List to recognize **true booth sellouts** (`sold >= brought` even when extra stock stayed home).

---

## 5. Unified VIP Customer Book, Egg/Farm Pass Credits & Past-Buyer Drops

* **The gap today:** Homestead tracks rich customer history (lifetime spend, tips, weight preferences), while Iron Ledger Quick Sale is anonymous—and launching next year's batch still means messaging repeat buyers manually.
* **What it does:**
  * Shares Homestead's `customers` directory with Iron Ledger Quick Sale so regulars can optionally be tagged on market sales—or buy a **Prepaid Farm / Egg Pass** (e.g., *"Pay $50, get $55 farm credit"* or *"10-Dozen Egg Club"*) that team members can deduct with 1 tap at market or the porch.
  * **Smart Seasonal Re-Order Alerts:** On a new batch's `2. Pre-Orders` page, filter past buyers (e.g., *"Everyone who bought a Thanksgiving Turkey last year"*) and launch a 1-tap personalized SMS/email queue with their `/reserve/<slug>` link.
* **Why it matters:** Locks in upfront spring cash flow and fills next season's pre-order sheet in minutes from repeat buyers.

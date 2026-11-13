# 🚜 Homestead Planner: Poultry & Pigs (`farm-app`)

A Streamlit application for planning, comparing, and persisting multiple livestock batches (chickens, turkeys, Kunekune pigs) on a shared lifecycle timeline, phased feed & barrel storage schedule, purchasing/cash-flow plan, and $/lb meat ROI calculator.

## Features

- **Top-Level Workspace Navigation & Master-Detail UX**:
  - **🗓️ Dashboard & Timeline**: 4 bordered KPI cards (Active Herd, Feed & Barrel Storage, Reconciled Cost vs. Est. Revenue, Net Profit & Home Freezer Meat), an **🔔 Upcoming Milestones & Action Items** strip with relative day badges and one-click Google Calendar links, and an interactive **Shared Herd Lifecycle Timeline** with quick action buttons (`🐾 Open in Batch Manager`, `✏️ Edit Batch`).
  - **🐾 Manage Batches & Logs (Master-Detail Workspace)**: Select any batch from the top selector bar (with `🟢 Active` / `🥩 Harvested` / `⚪ Excluded` status badges and action toolbar) to view 4 focused batch detail cards and 3 spacious tabs (`📉 Batch Losses`, `🔪 Harvest Log & Actual Feed Override`, `💵 Sales & Home Freezer Log`).
  - **🌾 Feed & Storage Schedule**: Side-by-side Weekly Feed Burn (Starter vs. Grower) and Concurrent Barrel Storage Demand charts with formatted Monthly & Weekly tables.
  - **💰 Financials & Reconciliation**: Dedicated sub-tabs for `🥩 Harvest, Freezer & Sales Reconciliation`, `💰 Estimated Cost, Yield & $/lb ROI`, and `🛒 Purchasing & Cash-Flow Schedule` with formatted currency and weight columns.
  - **⚙️ Settings & Backup**: Dedicated workspace for Barrel Size (lbs), Feed Bag Weight (lbs), and JSON Herd Backup & Restore.
- **Minimal Sidebar + Spacious `@st.dialog` Add/Edit Batch Modal**:
  - Minimal sidebar with a prominent `➕ Add Livestock Batch` button, Species & Status filters, and one-click `📅 Export Active Plan (.ics)`.
  - Wide popup `@st.dialog` modal organized into `1. Basics & Dates`, `2. Phased Feed Rations`, and `3. Pricing & Meat Yield` tabs with a **Live Batch Preview** banner before saving.
- **Persistent Multi-Batch Herd Planning**: Stores batches, dated loss records, actual harvest records, sales/home freezer allocations, and storage/bag settings in a local SQLite database (`FARM_DB_PATH`, defaulting to `data/homestead.db`) with full JSON backup & restore.
- **2-Phase Ration, Cost/Yield & Target Sale Price Modeling**:
  - Splits growing rations into **Starter** and **Grower/Finisher** phases (weeks, lbs/head, and bag prices pre-filled from species defaults and editable per batch).
  - Tracks acquisition cost per unit, whole-bag feed cost (with configurable bag weight, default 50 lbs, plus exact consumed cost comparison), processing cost per head, expected dressed meat yield (lbs/head), **Target Sale Price (\$/lb)**, **Feed Cost/lb of Meat**, **Total Cost/lb of Meat**, **Estimated Revenue**, **Estimated Net Profit**, and **ROI (%)**.
- **Dated Batch Loss Tracking**:
  - Record losses as they occur (`loss_date`, `count`, `reason`) inside each Saved Batch card.
  - Dynamically adjusts surviving headcount from `loss_date` onward, automatically recalculating Starter/Grower feed consumption, bags needed, barrel storage demand, harvested headcount, processing cost, expected meat yield, and \$/lb ROI.
  - Displays `⚠️ -N Lost` markers on the batch's swimlane in the shared timeline.
- **Actual Harvest, Home Freezer & Sales Reconciliation**:
  - **Harvest & Butcher Log**: Log whole-batch or split/multi-date harvests (`harvest_date`, `head_count`, `dressed_lbs`, `processing_cost_total`, `notes`). Early or split harvests automatically reduce remaining feed demand after `harvest_date` and place `🥩 Harvested` markers on the shared timeline.
  - **Actual Feed Override**: Optionally override total feed lbs and total feed cost used at the end of a batch to reconcile leftover or extra bags.
  - **Customer Sales & Home Freezer Log**: Allocate harvested meat between **Customer Sale** (tracking lbs sold and actual cash revenue) and **Home Freezer** (tracking lbs stored for personal consumption), computing **Actual Net Cash Position (\$)** and **Effective Cost of Home Freezer Meat (\$/lb)** after offsetting batch costs with customer sales revenue (highlighting `"🎉 $0.00/lb (Free Meat!)"` when customer sales cover 100% of batch costs).
- **Shared Grouped Swimlane Timeline**: Visualizes all active batches side-by-side using `streamlit-vis-timeline` with color-coded phases (`Breeding`/`Arrival`, `Gestation`/`Incubation`, `Starter`, `Grower/Finisher`, `Processing`, and loss markers).
- **Purchasing & Cash-Flow Schedule**: Chronological and monthly summary of when to purchase animals, Starter feed bags, Grower/Finisher feed bags, and pay butcher/processing fees.
- **Calendar Exports**: Fixed Google Calendar all-day event links per batch (including Ration Transition reminders) plus a one-click `.ics` calendar export for all active batches.

## Running Locally

```bash
pip install streamlit streamlit-vis-timeline pandas
streamlit run app.py
```

## Running with Docker

```bash
docker build -t farm-app .
docker run -p 8501:8501 -v $(pwd)/data:/app/data farm-app
```

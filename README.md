# 🚜 Homestead Planner: Poultry & Pigs (`farm-app`)

A Streamlit application for planning, comparing, and persisting multiple livestock batches (chickens, turkeys, Kunekune pigs) on a shared lifecycle timeline, phased feed & barrel storage schedule, purchasing/cash-flow plan, and $/lb meat ROI calculator.

## Features

- **Multi-Tenant User Accounts & Persistent Sessions**:
  - **First-Run Admin Onboarding**: Creating the first account promotes it to Administrator, seeds the 4 built-in species presets, and automatically claims any existing unowned batches, logs, and settings in the database.
  - **Per-User Data Isolation**: Each user has their own isolated livestock batches, loss/harvest/sales logs, storage barrel/bag settings, custom species presets, and JSON backups.
  - **Admin User Management & Persistent Login**: Administrators can create/delete user accounts, reset passwords, or toggle public self-registration on/off. Sessions persist across browser reloads via expiring session tokens.
- **Custom Livestock Species Presets & Lifecycle Types**:
  - Each user starts with 4 built-in presets (`Chicken Eggs`, `Chicken Chicks`, `Turkey Chicks`, `Kunekune Pigs`) and can add new species (e.g., `Meat Rabbits`, `Ducklings`, `Quail Eggs`, `Feeder Pigs`), edit/delete presets, or restore defaults via the `🐾 Manage Species Presets` modal or inline inside the `➕ Add Livestock Batch` dialog.
  - Supports 3 configurable **Lifecycle Types** per species: `🐣 Poultry / Eggs (Arrival → Incubation → Hatch)`, `🐖 Mammal / Breeding (Breeding → Gestation → Birth)`, and `📥 Direct Arrival (0 days to birth/hatch)`.
- **Streamlined 4-Tab Workflow Navigation**:
  - **🗓️ Radar & Timeline (Operational Radar)**: 3 clean single-number vital cards (`🐾 Animals on Ground`, `🌾 Feed & Barrel Storage`, `⏳ Next Farm Milestone`), plus a side-by-side main view with the interactive **Shared Herd Lifecycle Timeline** (2/3 width, with quick action buttons to `🐾 Open Batch Logs` or `✏️ Edit Batch`) and a compact **🔔 Next Up Action Agenda** checklist (1/3 width, with relative day badges and Google Calendar links).
  - **🐾 Batch Logs (Master-Detail Logging)**: Compact batch selector bar with primary `✏️ Edit Batch` button and a tucked-away `⋯ More & Calendar` popover menu (`📋 Duplicate Batch`, `⏸️ Exclude from Plan` / `▶️ Include in Plan`, `🗑️ Delete Batch`, and Google Calendar links), a single-line 4-metric vitals strip, and 3 action-first logging tabs (`📉 Batch Losses`, `🔪 Harvest & Butcher Log` with tucked-away Actual Feed Override, and `💵 Freezer & Sales Log`).
  - **🌾 Feed & Shopping**: Side-by-side Weekly Feed Burn (Starter vs. Grower) and Barrel Storage Demand charts, a chronological **🛒 Feed & Livestock Shopping Schedule**, and raw weekly/monthly feed tables tucked inside an optional expander.
  - **💰 Financials & Freezer (Unified Scorecard)**: 3 headline scorecard metrics (`💰 Total Outlay (Reconciled)`, `💵 Cash Sales & Net Cash`, `❄️ Home Freezer Meat`), a unified 8-column **🥩 Batch Economics & Freezer Summary** table, and granular 19-column cost/ration/ROI math tucked inside a `🔍 View Detailed Cost, Ration & Planned ROI Breakdown` expander.
- **Minimal Sidebar + Quick-Add `@st.dialog` Batch Modal**:
  - Minimal sidebar with a prominent `➕ Add Livestock Batch` button, Species & Status filters, one-click `📅 Export Active Plan (.ics)`, and a tucked-away `⚙️ Settings & Backup` expander (Barrel Size, Bag Weight, and JSON Herd Backup & Restore).
  - Streamlined Quick-Add popup `@st.dialog` modal showing only the 4 essentials (`Select Livestock Type`, `Initial Quantity`, `Arrival/Breeding Date`, `Batch Name`) up front, an optional `⚙️ Customize Feed Rations, Pricing & Notes` expander, and a 1-line **Live Preview** summary before saving.
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

## Project Structure

```text
├── app.py                        # Streamlit entry point & backward-compatible re-exports
├── homestead/                    # Core domain & data package
│   ├── config.py                 # Species presets, defaults, workspace & status constants
│   ├── database.py               # SQLite persistence, schema migrations, CRUD & JSON backup/restore
│   ├── scheduling.py             # Batch lifecycle engine, phased feed demand & upcoming milestones
│   ├── financials.py             # Shopping schedule, ROI summary & harvest/freezer reconciliation tables
│   ├── calendar_export.py        # Google Calendar link builder & RFC 5545 .ics export
│   └── ui/                       # Streamlit UI views & components
│       ├── timeline_shim.py      # streamlit-vis-timeline loader & headless AppTest fallback
│       ├── auth_view.py          # First-run admin setup, login/registration gate & user management modal
│       ├── species_modal.py      # @st.dialog modal for adding, editing & deleting species presets
│       ├── sidebar.py            # Minimal sidebar: user badge, Quick-Add, species manager, filters & settings
│       ├── batch_modal.py        # @st.dialog Quick-Add / Edit batch modal & inline custom species creation
│       ├── radar_tab.py          # Workspace 1: 🗓️ Radar & Timeline
│       ├── batch_logs_tab.py     # Workspace 2: 🐾 Batch Logs (losses, harvests & freezer/sales)
│       ├── feed_tab.py           # Workspace 3: 🌾 Feed & Shopping
│       └── financials_tab.py     # Workspace 4: 💰 Financials & Freezer
```

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

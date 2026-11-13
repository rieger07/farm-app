# 🚜 Homestead Planner: Poultry & Pigs (`farm-app`)

A Streamlit application for planning, comparing, and persisting multiple livestock batches (chickens, turkeys, Kunekune pigs) on a shared lifecycle timeline, phased feed & barrel storage schedule, purchasing/cash-flow plan, and $/lb meat ROI calculator.

## Features

- **Persistent Multi-Batch Herd Planning**: Stores batches, dated loss records, and storage/bag settings in a local SQLite database (`FARM_DB_PATH`, defaulting to `data/homestead.db`) with full JSON backup & restore in the sidebar.
- **2-Phase Ration & Cost/Yield Modeling**:
  - Splits growing rations into **Starter** and **Grower/Finisher** phases (weeks, lbs/head, and bag prices pre-filled from species defaults and editable per batch).
  - Tracks acquisition cost per unit, whole-bag feed cost (with configurable bag weight, default 50 lbs, plus exact consumed cost comparison), processing cost per head, expected dressed meat yield (lbs/head), **Feed Cost/lb of Meat**, and **Total Cost/lb of Meat**.
- **Dated Batch Loss Tracking**:
  - Record losses as they occur (`loss_date`, `count`, `reason`) inside each Saved Batch card.
  - Dynamically adjusts surviving headcount from `loss_date` onward, automatically recalculating Starter/Grower feed consumption, bags needed, barrel storage demand, harvested headcount, processing cost, expected meat yield, and $/lb ROI.
  - Displays `⚠️ -N Lost` markers on the batch's swimlane in the shared timeline.
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

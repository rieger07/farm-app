# 🚜 Homestead Planner: Poultry & Pigs (`farm-app`)

A Streamlit application for planning, comparing, and persisting multiple livestock batches (chickens, turkeys, Kunekune pigs) on a shared lifecycle timeline and combined feed/barrel storage schedule.

## Features

- **Persistent Multi-Batch Herd Planning**: Stores batches and storage settings in a local SQLite database (`FARM_DB_PATH`, defaulting to `data/homestead.db`) with full JSON backup & restore in the sidebar.
- **Customizable Batch Overrides**: Pre-fills gestation/incubation days, growing weeks to processing, and lifetime feed per animal from species presets while allowing per-batch overrides, custom batch names, notes, and past or future single dates or date ranges.
- **Shared Grouped Swimlane Timeline**: Visualizes all active batches side-by-side using `streamlit-vis-timeline` with color-coded phases (`Breeding`/`Arrival`, `Gestation`/`Incubation`, `Growing`, `Processing`).
- **Combined Feed & Storage Schedule**: Computes total lifetime feed & barrels, uniform weekly feed burn by batch, monthly consumption summaries, and peak concurrent barrel storage needed across overlapping batches.
- **Calendar Exports**: Fixed Google Calendar all-day event links per batch plus a one-click `.ics` calendar export for all active batches.

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

"""Bulk receipt CSV/duplicate helpers and historical batch creation/CSV import."""

from __future__ import annotations

import csv
from datetime import date, datetime
import io

from homestead.config import DEFAULT_BAG_WEIGHT, infer_lifecycle_type
from homestead.db.batches import (
    add_batch_feed_log,
    add_batch_harvest,
    add_batch_loss,
    add_batch_sale,
    list_batches,
    save_batch,
)
from homestead.db.core import get_bag_weight, get_db_connection, get_species_catalog
from homestead.db.customers import update_harvest_assignment
from homestead.db.expenses import EXPENSE_CATEGORIES
from homestead.db.herds import normalize_herd_feed_category, normalize_herd_feed_unit
from homestead.db.shares import normalize_sale_unit
from homestead.scheduling import compute_batch_schedule
from shared.bridge import normalize_homestead_payment_method

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 500

RECEIPT_CSV_HEADERS = [
    "date",
    "vendor",
    "kind",
    "category",
    "batch_or_herd",
    "feed_type",
    "quantity",
    "unit",
    "weight_lbs",
    "amount",
    "notes",
    "receipt_url",
]

BATCH_CSV_HEADERS = [
    "name",
    "species",
    "start_date",
    "initial_quantity",
    "unit_cost",
    "sale_unit",
    "harvest_date",
    "harvested_head",
    "dressed_lbs",
    "processing_cost",
    "feed_lbs",
    "feed_cost",
    "losses",
    "live_sold_head",
    "live_sale_date",
    "live_sale_revenue",
    "meat_sale_date",
    "meat_sale_revenue",
    "freezer_lbs",
    "payment_method",
    "notes",
]

_CAT_SYNONYMS = {
    "feed": "Feed",
    "processing": "Processing",
    "butcher": "Processing",
    "custom hire": "Processing",
    "livestock": "Livestock / Chicks",
    "chicks": "Livestock / Chicks",
    "poults": "Livestock / Chicks",
    "livestock / chicks": "Livestock / Chicks",
    "supplies": "Supplies & Equipment",
    "supplies & equipment": "Supplies & Equipment",
    "bedding": "Supplies & Equipment",
    "capital": "Capital Equipment / Depreciation",
    "capital equipment": "Capital Equipment / Depreciation",
    "capital equipment / depreciation": "Capital Equipment / Depreciation",
    "equipment": "Capital Equipment / Depreciation",
    "market": "Market / Selling Fees",
    "market / selling fees": "Market / Selling Fees",
    "selling": "Market / Selling Fees",
    "repairs": "Repairs & Maintenance",
    "repairs & maintenance": "Repairs & Maintenance",
    "maintenance": "Repairs & Maintenance",
    "utilities": "Utilities / Fuel",
    "fuel": "Utilities / Fuel",
    "utilities / fuel": "Utilities / Fuel",
    "veterinary": "Veterinary / Medicine",
    "vet": "Veterinary / Medicine",
    "veterinary / medicine": "Veterinary / Medicine",
    "other": "Other",
    "general": "Other",
}


def _parse_flexible_date(raw: str | None) -> str | None:
    """Parse YYYY-MM-DD, MM/DD/YYYY, or M/D/YY into strict YYYY-MM-DD."""
    s = str(raw or "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_float(raw: object, default: float = 0.0) -> float:
    s = str(raw or "").strip().replace("$", "").replace(",", "")
    if not s:
        return default
    return float(s)


def _parse_int(raw: object, default: int = 0) -> int:
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return default
    return int(round(float(s)))


def _csv_escape(val: object) -> str:
    s = str(val if val is not None else "")
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def find_duplicate_receipt_indices(
    user_id: int, candidates: list[dict]
) -> set[int]:
    """Return 0-based candidate indices matching existing entries by (date, vendor, rounded amount)."""
    uid = int(user_id)
    if not candidates:
        return set()

    existing_keys: set[tuple[str, str, float]] = set()
    group_totals: dict[tuple[str, str, str], float] = {}

    with get_db_connection() as conn:
        queries = [
            (
                "SELECT expense_date AS dt, vendor, amount AS amt, COALESCE(receipt_group_id, '') AS gid "
                "FROM farm_expenses WHERE user_id = ?",
                (uid,),
            ),
            (
                "SELECT fl.feed_date AS dt, COALESCE(fl.vendor, '') AS vendor, fl.cost_total AS amt, "
                "COALESCE(fl.receipt_group_id, '') AS gid "
                "FROM batch_feed_logs fl JOIN batches b ON b.id = fl.batch_id WHERE b.user_id = ?",
                (uid,),
            ),
            (
                "SELECT feed_date AS dt, COALESCE(vendor, '') AS vendor, cost_total AS amt, "
                "COALESCE(receipt_group_id, '') AS gid "
                "FROM layer_feed_logs WHERE user_id = ?",
                (uid,),
            ),
            (
                "SELECT feed_date AS dt, COALESCE(vendor, '') AS vendor, cost_total AS amt, "
                "COALESCE(receipt_group_id, '') AS gid "
                "FROM herd_feed_logs WHERE user_id = ?",
                (uid,),
            ),
        ]
        for sql, params in queries:
            for r in conn.execute(sql, params).fetchall():
                d_str = str(r["dt"] or "").strip()[:10]
                v_str = str(r["vendor"] or "").strip().lower()
                amt = round(float(r["amt"] or 0.0), 2)
                if d_str and amt != 0.0:
                    existing_keys.add((d_str, v_str, amt))
                    gid = str(r["gid"] or "").strip()
                    if gid:
                        gk = (d_str, v_str, gid)
                        group_totals[gk] = round(group_totals.get(gk, 0.0) + amt, 2)

    for (d_str, v_str, _), tot in group_totals.items():
        if tot != 0.0:
            existing_keys.add((d_str, v_str, tot))

    dups: set[int] = set()
    for idx, c in enumerate(candidates):
        d_str = _parse_flexible_date(c.get("date")) or str(c.get("date") or "").strip()[:10]
        v_str = str(c.get("vendor") or "").strip().lower()
        try:
            amt = round(float(c.get("total", c.get("amount", 0.0)) or 0.0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        if d_str and amt != 0.0 and (d_str, v_str, amt) in existing_keys:
            dups.add(idx)
    return dups


def generate_receipt_csv_template() -> str:
    """Return sample CSV spreadsheet for bulk receipt imports."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(RECEIPT_CSV_HEADERS)
    sample_rows = [
        ["2025-09-14", "Tractor Supply", "general", "Supplies & Equipment", "", "", "", "", "", "64.95", "Heat lamp bulbs & shavings", ""],
        ["2025-10-03", "Local Feed Mill", "batch_feed", "", "Spring Broilers", "grower", "4", "bags", "200", "92.00", "Bulk grower pellets", ""],
        ["2025-11-10", "Country Co-op", "layer_feed", "", "all", "layer", "2", "bags", "100", "46.00", "Layer mash", ""],
        ["2026-01-12", "Valley Hay Farm", "herd_feed", "", "Alpaca Herd", "hay", "10", "bales", "500", "85.00", "First cutting square bales", ""],
    ]
    for row in sample_rows:
        w.writerow([_csv_escape(x) for x in row])
    return buf.getvalue()


def generate_batch_csv_template() -> str:
    """Return sample CSV spreadsheet for historical batch imports."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(BATCH_CSV_HEADERS)
    sample_rows = [
        ["Fall Broilers 2025", "cornish cross", "2025-08-10", "30", "3.50", "whole", "2025-10-08", "28", "126.0", "140.00", "450", "198.00", "2", "0", "", "0", "2025-10-12", "550.00", "26.0", "cash", "Historical broiler cohort"],
        ["Kune Winter Litter 2025", "kunekune pigs", "2025-07-01", "7", "50.00", "shares", "2026-01-10", "4", "420.0", "0.00", "2100", "940.00", "1", "2", "2025-09-15", "450.00", "2026-01-22", "1890.00", "80.0", "venmo", "4 butchered + 2 sold as pets"],
    ]
    for row in sample_rows:
        w.writerow([_csv_escape(x) for x in row])
    return buf.getvalue()


def _normalize_receipt_kind(raw_kind: str) -> str:
    k = str(raw_kind or "general").strip().lower().replace("-", "_").replace(" ", "_")
    if k in ("batch_feed", "batch", "meat_batch_feed"):
        return "batch_feed"
    if k in ("layer_feed", "layer", "layers", "laying_flock_feed"):
        return "layer_feed"
    if k in ("herd_feed", "herd", "hay"):
        return "herd_feed"
    if k in ("general", "farm_expense", "general_farm_expense", "expense", ""):
        return "general"
    return k


def _normalize_expense_category(raw_cat: str) -> str:
    clean = str(raw_cat or "").strip()
    if clean in EXPENSE_CATEGORIES:
        return clean
    return _CAT_SYNONYMS.get(clean.lower(), "Other")


def _match_named_record(query: str, rows: list[dict]) -> dict | None:
    q = str(query or "").strip()
    if not q:
        return None
    if q.isdigit():
        qid = int(q)
        for r in rows:
            if int(r["id"]) == qid:
                return r
    ql = q.lower()
    exact = [r for r in rows if str(r.get("name") or "").strip().lower() == ql]
    if len(exact) == 1:
        return exact[0]
    partial = [r for r in rows if ql in str(r.get("name") or "").strip().lower()]
    if len(partial) == 1:
        return partial[0]
    return None


def parse_receipt_csv(
    raw_bytes: bytes,
    *,
    batches: list[dict],
    herds: list[dict],
    bag_weight: int,
    existing_user_id: int,
) -> dict:
    """Parse and validate a receipt CSV upload into preview rows."""
    if len(raw_bytes) > MAX_CSV_BYTES:
        raise ValueError("CSV file exceeds the 2 MB limit.")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded text.") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty or missing a header row.")

    safe_bw = max(1, int(bag_weight or DEFAULT_BAG_WEIGHT))
    parsed_rows: list[dict] = []
    valid_candidates: list[dict] = []
    candidate_map: list[int] = []

    for row_num, raw_row in enumerate(reader, start=2):
        if len(parsed_rows) >= MAX_CSV_ROWS:
            raise ValueError(f"CSV exceeds maximum of {MAX_CSV_ROWS} rows per import.")
        row = {str(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        if not any(row.values()):
            continue

        errs: list[str] = []
        rdate = _parse_flexible_date(row.get("date"))
        if not rdate:
            errs.append("Invalid or missing date")
        vendor = row.get("vendor", "")
        if not vendor:
            errs.append("Missing vendor")

        try:
            amt = round(_parse_float(row.get("amount"), 0.0), 2)
        except ValueError:
            amt = 0.0
            errs.append("Invalid amount")
        if amt == 0.0 and "Invalid amount" not in errs:
            errs.append("Amount must be non-zero")

        kind = _normalize_receipt_kind(row.get("kind", "general"))
        if kind not in ("general", "batch_feed", "layer_feed", "herd_feed"):
            errs.append(f"Unsupported kind '{row.get('kind', '')}'")
            kind = "general"

        target_raw = row.get("batch_or_herd", "")
        cat = _normalize_expense_category(row.get("category", "Other"))
        feed_type = row.get("feed_type", "").strip().lower()
        unit = row.get("unit", "").strip().lower()
        try:
            qty = _parse_float(row.get("quantity"), 0.0)
            lbs = _parse_float(row.get("weight_lbs"), 0.0)
        except ValueError:
            qty, lbs = 0.0, 0.0
            errs.append("Invalid quantity/weight")

        r_url = row.get("receipt_url", "").strip()
        if r_url and not r_url.lower().startswith(("http://", "https://")):
            errs.append("Receipt URL must start with http:// or https://")
            r_url = ""

        line_payload: dict = {"kind": kind, "amount": amt, "notes": row.get("notes", "")}
        alloc_label = cat

        if kind == "batch_feed":
            matched_b = _match_named_record(target_raw, batches)
            if not matched_b:
                errs.append(f"Batch '{target_raw}' not found")
            else:
                line_payload["batch_id"] = str(matched_b["id"])
                alloc_label = f"Batch Feed · {matched_b['name']}"
            phase = feed_type if feed_type in ("starter", "grower") else "grower"
            bags_val = qty
            lbs_val = lbs if lbs > 0 else (round(bags_val * safe_bw, 1) if bags_val > 0 else 0.0)
            line_payload.update({"feed_type": phase, "bags": bags_val, "feed_lbs": lbs_val})
        elif kind == "layer_feed":
            lt = feed_type if feed_type in ("layer", "scratch", "supplement") else "layer"
            tags = [t.strip().lower() for t in target_raw.split(",") if t.strip()] if target_raw else ["all"]
            bags_val = qty
            lbs_val = lbs if lbs > 0 else (round(bags_val * safe_bw, 1) if bags_val > 0 else 0.0)
            line_payload.update({"feed_type": lt, "species_tags": tags, "bags": bags_val, "feed_lbs": lbs_val})
            alloc_label = f"Layer Feed ({lt})"
        elif kind == "herd_feed":
            matched_h = _match_named_record(target_raw, herds)
            if not matched_h:
                errs.append(f"Herd '{target_raw}' not found")
            else:
                line_payload["herd_id"] = str(matched_h["id"])
                alloc_label = f"Herd Feed · {matched_h['name']}"
            fcat = normalize_herd_feed_category(feed_type or "grain")
            funit = normalize_herd_feed_unit(unit or ("bales" if fcat == "hay" else "bags"), fcat)
            lbs_val = lbs
            if lbs_val <= 0 and qty > 0:
                if funit == "bags":
                    lbs_val = round(qty * safe_bw, 1)
                elif funit == "lbs":
                    lbs_val = round(qty, 1)
            line_payload.update({"feed_category": fcat, "unit": funit, "quantity": qty, "weight_lbs": lbs_val})
        else:
            line_payload["category"] = cat
            if target_raw:
                matched_b = _match_named_record(target_raw, batches)
                if matched_b:
                    line_payload["batch_id"] = str(matched_b["id"])
                    alloc_label = f"{cat} · {matched_b['name']}"
                else:
                    errs.append(f"Batch '{target_raw}' not found for expense")

        item = {
            "row_num": row_num,
            "date": rdate or row.get("date", ""),
            "vendor": vendor,
            "kind": kind,
            "category": cat,
            "alloc_label": alloc_label,
            "amount": amt,
            "notes": row.get("notes", ""),
            "receipt_url": r_url,
            "line": line_payload,
            "errors": errs,
            "is_valid": len(errs) == 0,
            "is_duplicate": False,
        }
        idx = len(parsed_rows)
        parsed_rows.append(item)
        if item["is_valid"]:
            valid_candidates.append({"date": item["date"], "vendor": vendor, "total": amt})
            candidate_map.append(idx)

    dup_sub_indices = find_duplicate_receipt_indices(existing_user_id, valid_candidates)
    for sub_idx in dup_sub_indices:
        parsed_rows[candidate_map[sub_idx]]["is_duplicate"] = True

    return {
        "rows": parsed_rows,
        "valid_count": sum(1 for r in parsed_rows if r["is_valid"]),
        "error_count": sum(1 for r in parsed_rows if not r["is_valid"]),
        "duplicate_count": sum(1 for r in parsed_rows if r["is_duplicate"]),
    }


def _resolve_species(raw_species: str, species_catalog: dict) -> str:
    sp = str(raw_species or "").strip().lower()
    if not sp:
        return next(iter(species_catalog), "chicken chicks")
    if sp in species_catalog:
        return sp
    for key in species_catalog:
        if sp in key.lower() or key.lower() in sp:
            return key
    return sp


def parse_batch_csv(raw_bytes: bytes, *, species_catalog: dict) -> dict:
    """Parse and validate a historical batch CSV upload into preview rows."""
    if len(raw_bytes) > MAX_CSV_BYTES:
        raise ValueError("CSV file exceeds the 2 MB limit.")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded text.") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty or missing a header row.")

    parsed_rows: list[dict] = []
    for row_num, raw_row in enumerate(reader, start=2):
        if len(parsed_rows) >= MAX_CSV_ROWS:
            raise ValueError(f"CSV exceeds maximum of {MAX_CSV_ROWS} rows per import.")
        row = {str(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        if not any(row.values()):
            continue

        errs: list[str] = []
        name = row.get("name", "")
        if not name:
            errs.append("Missing batch name")

        start_d = _parse_flexible_date(row.get("start_date"))
        if not start_d:
            errs.append("Invalid or missing start_date")

        try:
            init_qty = _parse_int(row.get("initial_quantity"), 0)
        except ValueError:
            init_qty = 0
            errs.append("Invalid initial_quantity")
        if init_qty < 1 and "Invalid initial_quantity" not in errs:
            errs.append("initial_quantity must be >= 1")

        harvest_d = _parse_flexible_date(row.get("harvest_date")) if row.get("harvest_date") else None
        if row.get("harvest_date") and not harvest_d:
            errs.append("Invalid harvest_date")

        live_d = _parse_flexible_date(row.get("live_sale_date")) if row.get("live_sale_date") else None
        if row.get("live_sale_date") and not live_d:
            errs.append("Invalid live_sale_date")

        meat_d = _parse_flexible_date(row.get("meat_sale_date")) if row.get("meat_sale_date") else None
        if row.get("meat_sale_date") and not meat_d:
            errs.append("Invalid meat_sale_date")

        try:
            unit_cost = max(0.0, _parse_float(row.get("unit_cost"), 0.0))
            harvested_head = max(0, _parse_int(row.get("harvested_head"), 0))
            dressed_lbs = max(0.0, _parse_float(row.get("dressed_lbs"), 0.0))
            proc_cost = max(0.0, _parse_float(row.get("processing_cost"), 0.0))
            feed_lbs = max(0.0, _parse_float(row.get("feed_lbs"), 0.0))
            feed_cost = max(0.0, _parse_float(row.get("feed_cost"), 0.0))
            losses = max(0, _parse_int(row.get("losses"), 0))
            live_sold_head = max(0, _parse_int(row.get("live_sold_head"), 0))
            live_sale_rev = max(0.0, _parse_float(row.get("live_sale_revenue"), 0.0))
            meat_sale_rev = max(0.0, _parse_float(row.get("meat_sale_revenue"), 0.0))
            freezer_lbs = max(0.0, _parse_float(row.get("freezer_lbs"), 0.0))
        except ValueError:
            errs.append("Invalid numeric field")
            unit_cost = proc_cost = dressed_lbs = feed_lbs = feed_cost = live_sale_rev = meat_sale_rev = freezer_lbs = 0.0
            harvested_head = losses = live_sold_head = 0

        sp = _resolve_species(row.get("species", ""), species_catalog)
        su = normalize_sale_unit(row.get("sale_unit") or ("shares" if any(k in sp for k in ("pig", "hog", "cattle", "beef", "sheep", "lamb")) else "whole"))
        pm = normalize_homestead_payment_method(row.get("payment_method", "cash")) or "cash"

        parsed_rows.append(
            {
                "row_num": row_num,
                "name": name,
                "species": sp,
                "start_date": start_d or row.get("start_date", ""),
                "initial_quantity": init_qty,
                "unit_cost": round(unit_cost, 2),
                "sale_unit": su,
                "harvest_date": harvest_d or "",
                "harvested_head": harvested_head,
                "dressed_lbs": round(dressed_lbs, 2),
                "processing_cost": round(proc_cost, 2),
                "feed_lbs": round(feed_lbs, 1),
                "feed_cost": round(feed_cost, 2),
                "losses": losses,
                "live_sold_head": live_sold_head,
                "live_sale_date": live_d or "",
                "live_sale_revenue": round(live_sale_rev, 2),
                "meat_sale_date": meat_d or "",
                "meat_sale_revenue": round(meat_sale_rev, 2),
                "freezer_lbs": round(freezer_lbs, 2),
                "payment_method": pm,
                "notes": row.get("notes", ""),
                "errors": errs,
                "is_valid": len(errs) == 0,
            }
        )

    return {
        "rows": parsed_rows,
        "valid_count": sum(1 for r in parsed_rows if r["is_valid"]),
        "error_count": sum(1 for r in parsed_rows if not r["is_valid"]),
    }


def create_historical_batch_record(user_id: int, row: dict) -> int:
    """Create an archived historical batch and its summary losses, feed, harvest, and sales rows."""
    uid = int(user_id)
    catalog = get_species_catalog(user_id=uid)
    sp = _resolve_species(str(row.get("species") or ""), catalog)
    start_d = _parse_flexible_date(row.get("start_date"))
    if not start_d:
        raise ValueError("Valid start_date (YYYY-MM-DD) is required.")

    init_qty = max(1, _parse_int(row.get("initial_quantity"), 1))
    unit_cost = max(0.0, _parse_float(row.get("unit_cost"), 0.0))
    sale_unit = normalize_sale_unit(row.get("sale_unit") or "whole")
    harvested_head = max(0, _parse_int(row.get("harvested_head"), 0))
    dressed_lbs = round(max(0.0, _parse_float(row.get("dressed_lbs"), 0.0)), 2)
    proc_cost = round(max(0.0, _parse_float(row.get("processing_cost"), 0.0)), 2)
    feed_lbs = round(max(0.0, _parse_float(row.get("feed_lbs"), 0.0)), 1)
    feed_cost = round(max(0.0, _parse_float(row.get("feed_cost"), 0.0)), 2)
    losses = max(0, _parse_int(row.get("losses"), 0))
    live_sold_head = max(0, _parse_int(row.get("live_sold_head"), 0))
    live_sale_rev = round(max(0.0, _parse_float(row.get("live_sale_revenue"), 0.0)), 2)
    meat_sale_rev = round(max(0.0, _parse_float(row.get("meat_sale_revenue"), 0.0)), 2)
    freezer_lbs = round(max(0.0, _parse_float(row.get("freezer_lbs"), 0.0)), 2)
    pm = normalize_homestead_payment_method(row.get("payment_method")) or "cash"

    defaults = catalog.get(sp, {})
    avg_yield = (
        round(dressed_lbs / harvested_head, 2)
        if harvested_head > 0 and dressed_lbs > 0
        else float(defaults.get("expected_yield_lbs") or 0.0)
    )
    avg_proc = (
        round(proc_cost / harvested_head, 2)
        if harvested_head > 0 and proc_cost > 0
        else float(defaults.get("processing_cost") or 0.0)
    )
    lifecycle = str(row.get("lifecycle_type") or defaults.get("lifecycle_type") or infer_lifecycle_type(sp))

    bid = save_batch(
        {
            "name": str(row.get("name") or "Historical Batch").strip(),
            "species": sp,
            "start_date": start_d,
            "quantity": init_qty,
            "unit_cost": unit_cost,
            "expected_yield_lbs": avg_yield,
            "processing_cost": avg_proc,
            "lifecycle_type": lifecycle,
            "sale_unit": sale_unit,
            "active": False,
            "notes": str(row.get("notes") or "").strip(),
        },
        user_id=uid,
    )

    bag_weight = max(1, int(get_bag_weight(user_id=uid) or DEFAULT_BAG_WEIGHT))
    saved_batch = next(b for b in list_batches(user_id=uid) if int(b["id"]) == bid)
    sched = compute_batch_schedule(
        saved_batch,
        losses=[],
        harvests=[],
        sales=[],
        bag_weight=bag_weight,
    )
    default_proc_date = sched["proc_date"].isoformat()
    hdate = _parse_flexible_date(row.get("harvest_date")) or default_proc_date
    live_date = _parse_flexible_date(row.get("live_sale_date")) or hdate
    meat_date = _parse_flexible_date(row.get("meat_sale_date")) or hdate

    if losses > 0:
        add_batch_loss(bid, start_d, losses, "Historical summary losses")

    if feed_lbs > 0 or feed_cost > 0:
        bags_est = round(feed_lbs / bag_weight, 2) if feed_lbs > 0 else 0.0
        add_batch_feed_log(
            bid,
            hdate,
            "grower",
            bags=bags_est,
            feed_lbs=feed_lbs,
            cost_total=feed_cost,
            notes="Historical batch feed total",
        )

    if live_sold_head > 0 or live_sale_rev > 0:
        add_batch_sale(
            bid,
            live_date,
            "Live Sale",
            weight_lbs=0.0,
            revenue=live_sale_rev,
            notes="Historical live / pet sale",
            payment_method=pm,
            head_count=max(1, live_sold_head or 1),
        )

    has_harvest_data = harvested_head > 0 or dressed_lbs > 0 or proc_cost > 0
    if has_harvest_data:
        hc = max(1, harvested_head or 1)
        if meat_sale_rev > 0 and freezer_lbs <= 0:
            hid = add_batch_harvest(
                bid,
                hdate,
                hc,
                dressed_lbs,
                proc_cost,
                notes="Historical harvest & sale",
                for_sale=True,
            )
            update_harvest_assignment(
                bid,
                hid,
                customer_id=None,
                for_sale=True,
                actual_sale_price=meat_sale_rev,
                payment_status="paid",
                pickup_status="picked_up",
                payment_method=pm,
                paid_date=meat_date,
            )
        elif meat_sale_rev > 0 and freezer_lbs > 0:
            add_batch_harvest(
                bid,
                hdate,
                hc,
                dressed_lbs,
                proc_cost,
                notes="Historical harvest (sold + freezer)",
                for_sale=True,
            )
            sold_wt = max(0.01, round(dressed_lbs - freezer_lbs, 2))
            add_batch_sale(
                bid,
                meat_date,
                "Customer Sale",
                weight_lbs=sold_wt,
                revenue=meat_sale_rev,
                notes="Historical meat sales",
                payment_method=pm,
            )
            add_batch_sale(
                bid,
                hdate,
                "Home Freezer",
                weight_lbs=freezer_lbs,
                revenue=0.0,
                notes="Historical home freezer share",
            )
        elif meat_sale_rev <= 0 and freezer_lbs > 0:
            add_batch_harvest(
                bid,
                hdate,
                hc,
                max(dressed_lbs, freezer_lbs),
                proc_cost,
                notes="Historical home freezer harvest",
                for_sale=False,
            )
        else:
            add_batch_harvest(
                bid,
                hdate,
                hc,
                dressed_lbs,
                proc_cost,
                notes="Historical harvest",
                for_sale=True,
            )
    else:
        if meat_sale_rev > 0:
            add_batch_sale(
                bid,
                meat_date,
                "Customer Sale",
                weight_lbs=max(0.01, round(dressed_lbs, 2)),
                revenue=meat_sale_rev,
                notes="Historical meat sales",
                payment_method=pm,
            )
        if freezer_lbs > 0:
            add_batch_sale(
                bid,
                hdate,
                "Home Freezer",
                weight_lbs=freezer_lbs,
                revenue=0.0,
                notes="Historical home freezer share",
            )

    return bid

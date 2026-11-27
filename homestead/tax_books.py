"""Unified Schedule F Tax Books aggregator combining Homestead and Iron Ledger cash data."""

from __future__ import annotations

from datetime import date

from homestead.db.batches import (
    list_batch_feed_logs,
    list_batch_harvests,
    list_batch_sales,
    list_batches,
)
from homestead.db.eggs import list_layer_feed_logs
from homestead.db.expenses import list_farm_expenses, schedule_f_line_for
from homestead.db.fiber import list_fiber_mill_lots, list_fiber_shearings
from homestead.db.herds import list_herd_feed_logs
from homestead.db.shares import effective_share_weight, list_batch_harvest_shares
from shared.bridge import (
    get_ledger_sales_summary_by_year,
    list_ledger_market_day_costs,
)


def _year_of(date_str: str | None) -> int | None:
    s = str(date_str or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    return None


def _make_entry(
    *,
    entry_id: int,
    date_str: str,
    source_key: str,
    source_label: str,
    category: str,
    vendor: str,
    allocation_label: str,
    amount: float,
    notes: str = "",
    receipt_url: str = "",
    receipt_group_id: str = "",
    source_url: str = "",
    can_delete: bool = True,
    can_attach_receipt: bool = True,
) -> dict:
    sf_code, sf_label, sf_sort = schedule_f_line_for(category)
    return {
        "entry_id": int(entry_id),
        "date": str(date_str or "")[:10],
        "source_key": source_key,
        "source_label": source_label,
        "category": category,
        "schedule_f_code": sf_code,
        "schedule_f_label": sf_label,
        "schedule_f_sort": sf_sort,
        "vendor": str(vendor or "").strip(),
        "allocation_label": str(allocation_label or "").strip(),
        "amount": round(float(amount or 0.0), 2),
        "notes": str(notes or "").strip(),
        "receipt_url": str(receipt_url or "").strip(),
        "receipt_group_id": str(receipt_group_id or "").strip(),
        "source_url": source_url,
        "can_delete": can_delete,
        "can_attach_receipt": can_attach_receipt,
        "is_split": False,
        "split_count": 1,
    }


def build_unified_tax_books(
    user_id: int,
    merchant_id: int | None = None,
    tax_year: int | None = None,
) -> dict:
    """Aggregate all actual farm expenses, cash-basis income, and Schedule F totals."""
    uid = int(user_id)
    batches = list_batches(user_id=uid)
    batches_by_id = {int(b["id"]): b for b in batches}
    all_entries: list[dict] = []

    # 1. General farm expenses
    for exp in list_farm_expenses(uid):
        bid = exp.get("batch_id")
        all_entries.append(
            _make_entry(
                entry_id=int(exp["id"]),
                date_str=exp["expense_date"],
                source_key="general",
                source_label="General",
                category=exp.get("category") or "Other",
                vendor=exp.get("vendor") or "",
                allocation_label=exp.get("batch_name") or "General farm",
                amount=float(exp.get("amount") or 0.0),
                notes=exp.get("notes") or "",
                receipt_url=exp.get("receipt_url") or "",
                receipt_group_id=exp.get("receipt_group_id") or "",
                source_url=f"/batches/{bid}" if bid else "",
                can_delete=True,
                can_attach_receipt=True,
            )
        )

    # 2. Batch feed purchases
    for b in batches:
        bid = int(b["id"])
        for fl in list_batch_feed_logs(bid):
            all_entries.append(
                _make_entry(
                    entry_id=int(fl["id"]),
                    date_str=fl["feed_date"],
                    source_key="batch_feed",
                    source_label="Batch Feed",
                    category="Feed",
                    vendor=fl.get("vendor") or "",
                    allocation_label=f"{b['name']} ({fl['feed_type']})",
                    amount=float(fl.get("cost_total") or 0.0),
                    notes=fl.get("notes") or "",
                    receipt_url=fl.get("receipt_url") or "",
                    receipt_group_id=fl.get("receipt_group_id") or "",
                    source_url=f"/batches/{bid}",
                    can_delete=True,
                    can_attach_receipt=True,
                )
            )

    # 3. Laying flock feed purchases
    for fl in list_layer_feed_logs(uid, limit=5000):
        all_entries.append(
            _make_entry(
                entry_id=int(fl["id"]),
                date_str=fl["feed_date"],
                source_key="layer_feed",
                source_label="Layer Feed",
                category="Feed",
                vendor=fl.get("vendor") or "",
                allocation_label=f"Layers · {fl['feed_type']} ({fl['species_tags_label']})",
                amount=float(fl.get("cost_total") or 0.0),
                notes=fl.get("notes") or "",
                receipt_url=fl.get("receipt_url") or "",
                receipt_group_id=fl.get("receipt_group_id") or "",
                source_url="/eggs?tab=flock",
                can_delete=True,
                can_attach_receipt=True,
            )
        )

    # 3b. Perennial herd feed & hay purchases
    for fl in list_herd_feed_logs(uid, limit=5000):
        qty_val = float(fl.get("quantity") or 0.0)
        unit_lbl = str(fl.get("unit") or "bags")
        cat_lbl = str(fl.get("feed_category") or "grain").title()
        qty_str = f" ({qty_val:g} {unit_lbl})" if qty_val else ""
        all_entries.append(
            _make_entry(
                entry_id=int(fl["id"]),
                date_str=fl["feed_date"],
                source_key="herd_feed",
                source_label="Herd Feed",
                category="Feed",
                vendor=fl.get("vendor") or "",
                allocation_label=f"{fl.get('herd_name') or 'Herd'} · {cat_lbl}{qty_str}",
                amount=float(fl.get("cost_total") or 0.0),
                notes=fl.get("notes") or "",
                receipt_url=fl.get("receipt_url") or "",
                receipt_group_id=fl.get("receipt_group_id") or "",
                source_url=f"/herds?herd_id={int(fl['herd_id'])}",
                can_delete=True,
                can_attach_receipt=True,
            )
        )

    # 4. Harvest processing fees
    all_harvests = list_batch_harvests(user_id=uid)
    all_shares = list_batch_harvest_shares(user_id=uid)
    shares_by_harvest: dict[int, list[dict]] = {}
    for sh in all_shares:
        shares_by_harvest.setdefault(int(sh["harvest_id"]), []).append(sh)
    for hv in all_harvests:
        proc = float(hv.get("processing_cost_total") or 0.0)
        if proc <= 0:
            continue
        bid = int(hv["batch_id"])
        b_name = batches_by_id.get(bid, {}).get("name") or f"Batch #{bid}"
        all_entries.append(
            _make_entry(
                entry_id=int(hv["id"]),
                date_str=hv["harvest_date"],
                source_key="processing",
                source_label="Processing",
                category="Processing",
                vendor="",
                allocation_label=b_name,
                amount=proc,
                notes=hv.get("notes") or "",
                receipt_url=hv.get("receipt_url") or "",
                receipt_group_id="",
                source_url=f"/batches/{bid}",
                can_delete=False,
                can_attach_receipt=True,
            )
        )

    # 4b. Fiber shearing & mill processing fees
    for sh in list_fiber_shearings(uid):
        fee = float(sh.get("shearing_fee") or 0.0)
        if fee <= 0:
            continue
        hid = int(sh["herd_id"])
        who_or_clr = sh.get("animal_name") or sh.get("fleece_color") or "Shearing"
        all_entries.append(
            _make_entry(
                entry_id=int(sh["id"]),
                date_str=sh["shear_date"],
                source_key="fiber_shearing",
                source_label="Fiber Shearing",
                category="Processing",
                vendor="",
                allocation_label=f"{sh.get('herd_name') or 'Herd'} · {who_or_clr}",
                amount=fee,
                notes=sh.get("notes") or "",
                receipt_url="",
                receipt_group_id="",
                source_url=f"/herds?herd_id={hid}&tab=shearing",
                can_delete=False,
                can_attach_receipt=False,
            )
        )

    for lot in list_fiber_mill_lots(uid):
        if not lot.get("include_in_tax_books"):
            continue
        cost = float(lot.get("total_mill_cost") or 0.0)
        if cost <= 0:
            continue
        hid = int(lot["herd_id"])
        dt_str = (lot.get("received_date") or lot.get("sent_date") or str(lot.get("created_at") or "")[:10])
        all_entries.append(
            _make_entry(
                entry_id=int(lot["id"]),
                date_str=dt_str,
                source_key="fiber_mill",
                source_label="Fiber Mill",
                category="Processing",
                vendor=lot.get("mill_name") or "",
                allocation_label=f"{lot.get('herd_name') or 'Herd'} · {lot['lot_name']}",
                amount=cost,
                notes=lot.get("notes") or "",
                receipt_url=lot.get("receipt_url") or "",
                receipt_group_id="",
                source_url=f"/herds?herd_id={hid}&tab=mill",
                can_delete=False,
                can_attach_receipt=True,
            )
        )

    # 5. Iron Ledger market booth & travel fees
    for md in list_ledger_market_day_costs(merchant_id, tax_year=None):
        all_entries.append(
            _make_entry(
                entry_id=int(md["id"]),
                date_str=md["market_date"],
                source_key="market_day",
                source_label="Market Day",
                category="Market / Selling Fees",
                vendor=md["marketplace_name"],
                allocation_label=(
                    f"Booth ${md['booth_fee_dollars']:.2f}"
                    f" + Travel ${md['travel_cost_dollars']:.2f}"
                ),
                amount=md["total_dollars"],
                notes=md["notes"],
                receipt_url="",
                receipt_group_id="",
                source_url="",
                can_delete=False,
                can_attach_receipt=False,
            )
        )

    # Tag multi-line split receipts
    group_counts: dict[str, int] = {}
    for entry in all_entries:
        gid = entry["receipt_group_id"]
        if gid:
            group_counts[gid] = group_counts.get(gid, 0) + 1
    for entry in all_entries:
        gid = entry["receipt_group_id"]
        if gid and group_counts.get(gid, 0) > 1:
            entry["is_split"] = True
            entry["split_count"] = group_counts[gid]

    all_sales = list_batch_sales(user_id=uid)
    ledger_all = get_ledger_sales_summary_by_year(merchant_id, tax_year=None)

    years: set[int] = {date.today().year}
    for entry in all_entries:
        yr = _year_of(entry["date"])
        if yr is not None:
            years.add(yr)
    for sa in all_sales:
        yr = _year_of(sa.get("entry_date"))
        if yr is not None:
            years.add(yr)
    for hv in all_harvests:
        yr = _year_of(hv.get("harvest_date"))
        if yr is not None:
            years.add(yr)
    for sh in all_shares:
        yr = _year_of(sh.get("paid_at"))
        if yr is not None:
            years.add(yr)
    years.update(ledger_all["years"])
    available_years = sorted(years, reverse=True)

    # Filter expense entries by selected tax year
    if tax_year is not None:
        entries = [e for e in all_entries if _year_of(e["date"]) == tax_year]
    else:
        entries = list(all_entries)
    entries.sort(key=lambda e: (e["date"], int(e["entry_id"])), reverse=True)

    # Schedule F category summary
    cat_buckets: dict[tuple[str, str, int], dict] = {}
    for entry in entries:
        key = (
            entry["schedule_f_code"],
            entry["schedule_f_label"],
            entry["schedule_f_sort"],
        )
        bucket = cat_buckets.setdefault(
            key,
            {
                "schedule_f_code": entry["schedule_f_code"],
                "schedule_f_label": entry["schedule_f_label"],
                "sort_order": entry["schedule_f_sort"],
                "count": 0,
                "missing_receipt_count": 0,
                "total": 0.0,
            },
        )
        bucket["count"] += 1
        bucket["total"] = round(bucket["total"] + entry["amount"], 2)
        if entry["can_attach_receipt"] and not entry["receipt_url"]:
            bucket["missing_receipt_count"] += 1
    category_summary = sorted(
        cat_buckets.values(), key=lambda c: (c["sort_order"], c["schedule_f_label"])
    )

    # Cash-basis gross farm income (Homestead + Iron Ledger)
    homestead_income = 0.0
    for sa in all_sales:
        if sa.get("allocation_type") != "Customer Sale":
            continue
        if tax_year is not None and _year_of(sa.get("entry_date")) != tax_year:
            continue
        homestead_income += float(sa.get("revenue") or 0.0)

    for hv in all_harvests:
        child_shares = shares_by_harvest.get(int(hv["id"]))
        if child_shares:
            carcass_lbs = float(hv.get("dressed_lbs") or 0.0)
            b = batches_by_id.get(int(hv["batch_id"]), {})
            sp = float(b.get("sale_price_per_lb") or 0.0)
            for sh in child_shares:
                if not bool(sh.get("for_sale", 1)):
                    continue
                pay_stat = str(sh.get("payment_status") or "").lower()
                pick_stat = str(sh.get("pickup_status") or "").lower()
                if not (pay_stat == "paid" or pick_stat == "picked_up"):
                    continue
                cash_yr = _year_of(sh.get("paid_at")) or _year_of(hv.get("harvest_date"))
                if tax_year is not None and cash_yr != tax_year:
                    continue
                actual_p = sh.get("actual_sale_price")
                if actual_p is not None:
                    homestead_income += float(actual_p)
                else:
                    homestead_income += round(effective_share_weight(sh, carcass_lbs) * sp, 2)
            continue
        if tax_year is not None and _year_of(hv.get("harvest_date")) != tax_year:
            continue
        if not bool(hv.get("for_sale", 1)):
            continue
        pay_stat = str(hv.get("payment_status") or "").lower()
        pick_stat = str(hv.get("pickup_status") or "").lower()
        actual_p = hv.get("actual_sale_price")
        if not (pay_stat == "paid" or pick_stat == "picked_up" or actual_p is not None):
            continue
        if actual_p is not None:
            homestead_income += float(actual_p)
        else:
            b = batches_by_id.get(int(hv["batch_id"]), {})
            sp = float(b.get("sale_price_per_lb") or 0.0)
            homestead_income += round(float(hv.get("dressed_lbs") or 0.0) * sp, 2)

    homestead_income = round(homestead_income, 2)
    ledger_filtered = (
        ledger_all
        if tax_year is None
        else get_ledger_sales_summary_by_year(merchant_id, tax_year=tax_year)
    )
    ledger_income = round(float(ledger_filtered["total_dollars"] or 0.0), 2)
    gross_income = round(homestead_income + ledger_income, 2)
    total_expenses = round(sum(e["amount"] for e in entries), 2)
    estimated_net = round(gross_income - total_expenses, 2)

    # Home freezer personal-use callouts
    freezer_notes: list[dict] = []
    harvests_by_batch: dict[int, list[dict]] = {}
    sales_by_batch: dict[int, list[dict]] = {}
    for hv in all_harvests:
        if tax_year is not None and _year_of(hv.get("harvest_date")) != tax_year:
            continue
        harvests_by_batch.setdefault(int(hv["batch_id"]), []).append(hv)
    for sa in all_sales:
        if tax_year is not None and _year_of(sa.get("entry_date")) != tax_year:
            continue
        sales_by_batch.setdefault(int(sa["batch_id"]), []).append(sa)

    for b in batches:
        bid = int(b["id"])
        bh = harvests_by_batch.get(bid, [])
        bs = sales_by_batch.get(bid, [])
        if tax_year is not None and not bh and not bs:
            continue
        hv_freezer_lbs = 0.0
        hv_total_lbs = 0.0
        for h in bh:
            carcass_lbs = float(h.get("dressed_lbs") or 0.0)
            child_shares = shares_by_harvest.get(int(h["id"]))
            if child_shares:
                for sh in child_shares:
                    sw = effective_share_weight(sh, carcass_lbs)
                    hv_total_lbs += sw
                    if not bool(sh.get("for_sale", 1)):
                        hv_freezer_lbs += sw
            else:
                hv_total_lbs += carcass_lbs
                if not bool(h.get("for_sale", 1)):
                    hv_freezer_lbs += carcass_lbs
        freezer_lbs = round(
            hv_freezer_lbs
            + sum(float(s.get("weight_lbs") or 0.0) for s in bs if s.get("allocation_type") == "Home Freezer"),
            2,
        )
        total_lbs = round(
            hv_total_lbs
            + sum(float(s.get("weight_lbs") or 0.0) for s in bs),
            2,
        )
        if freezer_lbs > 0 and total_lbs > 0:
            freezer_notes.append(
                {
                    "batch_name": b["name"],
                    "freezer_lbs": freezer_lbs,
                    "total_lbs": total_lbs,
                    "freezer_pct": round((freezer_lbs / total_lbs) * 100.0, 1),
                }
            )

    receipt_eligible = sum(1 for e in entries if e["can_attach_receipt"])
    with_receipt = sum(
        1 for e in entries if e["can_attach_receipt"] and e["receipt_url"]
    )
    return {
        "entries": entries,
        "available_years": available_years,
        "selected_year": tax_year,
        "category_summary": category_summary,
        "totals": {
            "homestead_income": homestead_income,
            "ledger_income": ledger_income,
            "ledger_sale_count": int(ledger_filtered["sale_count"]),
            "gross_income": gross_income,
            "total_expenses": total_expenses,
            "estimated_net": estimated_net,
            "receipt_eligible_count": receipt_eligible,
            "with_receipt_count": with_receipt,
            "missing_receipt_count": receipt_eligible - with_receipt,
        },
        "freezer_notes": freezer_notes,
    }

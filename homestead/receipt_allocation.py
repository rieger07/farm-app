"""Multi-batch receipt allocation math, timeframe batch options, and split expansion."""

from __future__ import annotations

import math
from typing import Any

from homestead.db.herds import (
    normalize_herd_feed_category,
    normalize_herd_feed_unit,
)


class BatchOwnershipError(LookupError):
    """Raised when a receipt line references a batch not owned by the active user."""


def allocate_signed_total(
    total: float, weights: list[float], decimals: int = 2
) -> list[float]:
    """Distribute ``total`` across ``weights`` using largest-remainder rounding.

    Guarantees exact reconciliation: ``round(sum(shares), decimals) == round(total, decimals)``
    for both positive charges and negative refunds.
    """
    if not weights:
        raise ValueError("At least one allocation weight is required.")
    clean_weights = [float(w) for w in weights]
    if any((not math.isfinite(w)) or w < 0 for w in clean_weights):
        raise ValueError("Allocation weights must be non-negative finite numbers.")
    total_weight = sum(clean_weights)
    if total_weight <= 0:
        raise ValueError("Total allocation weight must be greater than 0.")

    scale = 10**decimals
    total_units = int(round(float(total) * scale))
    if total_units == 0:
        return [0.0] * len(clean_weights)

    sign = -1 if total_units < 0 else 1
    abs_units = abs(total_units)

    raw_units = [(abs_units * w) / total_weight for w in clean_weights]
    floors = [int(math.floor(val)) for val in raw_units]
    remainder = abs_units - sum(floors)
    if remainder > 0:
        order = sorted(
            range(len(clean_weights)),
            key=lambda idx: (raw_units[idx] - floors[idx], clean_weights[idx], -idx),
            reverse=True,
        )
        for idx in order[:remainder]:
            floors[idx] += 1

    return [round((sign * cnt) / scale, decimals) for cnt in floors]


def build_receipt_batch_options(schedules: list[dict]) -> list[dict[str, Any]]:
    """Convert computed batch schedules into JSON-safe receipt modal options."""
    out: list[dict[str, Any]] = []
    for sched in schedules:
        batch = sched["batch"]
        harvest_dates = [
            hv["harvest_date"]
            for hv in (sched.get("harvests") or [])
            if hv.get("harvest_date")
        ]
        effective_end = max([sched["proc_date"], *harvest_dates])
        out.append(
            {
                "id": int(batch["id"]),
                "name": str(batch["name"]),
                "species": str(batch["species"]),
                "active": bool(batch.get("active", 1)),
                "start_date": sched["start_date"].isoformat(),
                "feed_start_date": sched["proc_base"].isoformat(),
                "transition_date": sched["transition_date"].isoformat(),
                "end_date": effective_end.isoformat(),
                "surviving_quantity": int(sched["surviving_quantity"]),
                "starter_weekly_feed": round(float(sched["starter_weekly_feed"]), 2),
                "grower_weekly_feed": round(float(sched["grower_weekly_feed"]), 2),
                "weekly_feed": round(float(sched["weekly_feed"]), 2),
                "starter_bag_price": float(sched["starter_bag_price"]),
                "grower_bag_price": float(sched["grower_bag_price"]),
            }
        )
    return out


def _align_signed_qty(value: float, amount: float, decimals: int) -> float:
    mag = abs(float(value or 0.0))
    if mag == 0:
        return 0.0
    return round(-mag if amount < 0 else mag, decimals)


def _parse_owned_batch_id(raw_bid: Any, owned_batch_ids: set[int]) -> int:
    s = str(raw_bid or "").strip()
    if not s.isdigit():
        raise BatchOwnershipError("Batch not found.")
    bid = int(s)
    if bid not in owned_batch_ids:
        raise BatchOwnershipError("Batch not found.")
    return bid


def _parse_owned_herd_id(raw_hid: Any, owned_herd_ids: set[int]) -> int:
    s = str(raw_hid or "").strip()
    if not s.isdigit():
        raise BatchOwnershipError("Herd not found.")
    hid = int(s)
    if hid not in owned_herd_ids:
        raise BatchOwnershipError("Herd not found.")
    return hid


def expand_receipt_lines(
    raw_lines: list[dict],
    *,
    owned_batch_ids: set[int],
    owned_herd_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Validate and expand raw modal lines (including multi-batch & multi-herd allocations) into DB rows."""
    if not isinstance(raw_lines, list):
        raise ValueError("Invalid receipt line data.")

    herd_ids_set = owned_herd_ids if owned_herd_ids is not None else set()
    expanded: list[dict[str, Any]] = []
    for item in raw_lines:
        if not isinstance(item, dict):
            continue
        try:
            amt = round(float(item.get("amount") or 0.0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        if amt == 0.0:
            continue

        kind = str(item.get("kind") or "general").strip()
        if kind not in ("batch_feed", "layer_feed", "herd_feed", "general"):
            kind = "general"
        notes = str(item.get("notes") or "").strip()

        if kind == "layer_feed":
            try:
                raw_bags = float(item.get("bags") or 0.0)
                raw_lbs = float(item.get("feed_lbs") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid quantity on Laying Flock Feed line.") from exc
            expanded.append(
                {
                    "kind": "layer_feed",
                    "amount": amt,
                    "feed_type": str(item.get("feed_type") or "layer").strip() or "layer",
                    "species_tags": item.get("species_tags") or ["all"],
                    "bags": _align_signed_qty(raw_bags, amt, 2),
                    "feed_lbs": _align_signed_qty(raw_lbs, amt, 1),
                    "notes": notes,
                }
            )
            continue

        if kind == "herd_feed":
            fcat = normalize_herd_feed_category(item.get("feed_category"))
            funit = normalize_herd_feed_unit(item.get("unit"), fcat)
            try:
                raw_qty = float(item.get("quantity") or 0.0)
                raw_lbs_val = item.get("weight_lbs") if item.get("weight_lbs") is not None else item.get("feed_lbs")
                raw_lbs = float(raw_lbs_val or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid quantity on Herd Feed / Hay line.") from exc

            raw_h_allocs = item.get("allocations")
            if isinstance(raw_h_allocs, list) and len(raw_h_allocs) > 0:
                merged_h: dict[int, float] = {}
                for alloc in raw_h_allocs:
                    if not isinstance(alloc, dict):
                        continue
                    hid = _parse_owned_herd_id(alloc.get("herd_id"), herd_ids_set)
                    try:
                        wt = float(alloc.get("weight") or 0.0)
                    except (TypeError, ValueError):
                        wt = 0.0
                    if wt > 0 and math.isfinite(wt):
                        merged_h[hid] = merged_h.get(hid, 0.0) + wt
                if not merged_h:
                    raise ValueError(
                        "Enter a positive split weight for at least one selected herd."
                    )
                h_ids = list(merged_h.keys())
                h_weights = [merged_h[hid] for hid in h_ids]
                amt_shares = allocate_signed_total(amt, h_weights, decimals=2)
                qty_shares = allocate_signed_total(
                    _align_signed_qty(raw_qty, amt, 2), h_weights, decimals=2
                )
                lb_shares = allocate_signed_total(
                    _align_signed_qty(raw_lbs, amt, 1), h_weights, decimals=1
                )
                for hid, a_sh, q_sh, l_sh in zip(
                    h_ids, amt_shares, qty_shares, lb_shares
                ):
                    if a_sh == 0.0:
                        continue
                    expanded.append(
                        {
                            "kind": "herd_feed",
                            "herd_id": hid,
                            "feed_category": fcat,
                            "unit": funit,
                            "quantity": q_sh,
                            "weight_lbs": l_sh,
                            "amount": a_sh,
                            "notes": notes,
                        }
                    )
            else:
                raw_hid = str(item.get("herd_id") or "").strip()
                if not raw_hid.isdigit():
                    raise ValueError("Choose a herd for each Herd Feed / Hay line.")
                hid = _parse_owned_herd_id(raw_hid, herd_ids_set)
                expanded.append(
                    {
                        "kind": "herd_feed",
                        "herd_id": hid,
                        "feed_category": fcat,
                        "unit": funit,
                        "quantity": _align_signed_qty(raw_qty, amt, 2),
                        "weight_lbs": _align_signed_qty(raw_lbs, amt, 1),
                        "amount": amt,
                        "notes": notes,
                    }
                )
            continue

        raw_allocs = item.get("allocations")
        if isinstance(raw_allocs, list) and len(raw_allocs) > 0:
            merged: dict[int, float] = {}
            for alloc in raw_allocs:
                if not isinstance(alloc, dict):
                    continue
                bid = _parse_owned_batch_id(alloc.get("batch_id"), owned_batch_ids)
                try:
                    wt = float(alloc.get("weight") or 0.0)
                except (TypeError, ValueError):
                    wt = 0.0
                if wt > 0 and math.isfinite(wt):
                    merged[bid] = merged.get(bid, 0.0) + wt

            if not merged:
                raise ValueError(
                    "Enter a positive split weight for at least one selected batch."
                )

            batch_ids = list(merged.keys())
            weights = [merged[bid] for bid in batch_ids]
            amt_shares = allocate_signed_total(amt, weights, decimals=2)

            if kind == "batch_feed":
                try:
                    raw_bags = float(item.get("bags") or 0.0)
                    raw_lbs = float(item.get("feed_lbs") or 0.0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid feed weight on Meat Batch Feed line.") from exc
                total_bags = _align_signed_qty(raw_bags, amt, 2)
                total_lbs = _align_signed_qty(raw_lbs, amt, 1)
                bag_shares = allocate_signed_total(total_bags, weights, decimals=2)
                lb_shares = allocate_signed_total(total_lbs, weights, decimals=1)
                feed_type = str(item.get("feed_type") or "grower").strip() or "grower"
                for bid, a_sh, b_sh, l_sh in zip(
                    batch_ids, amt_shares, bag_shares, lb_shares
                ):
                    if a_sh == 0.0:
                        continue
                    expanded.append(
                        {
                            "kind": "batch_feed",
                            "batch_id": bid,
                            "feed_type": feed_type,
                            "bags": b_sh,
                            "feed_lbs": l_sh,
                            "amount": a_sh,
                            "notes": notes,
                        }
                    )
            else:
                category = str(item.get("category") or "Other").strip() or "Other"
                for bid, a_sh in zip(batch_ids, amt_shares):
                    if a_sh == 0.0:
                        continue
                    expanded.append(
                        {
                            "kind": "general",
                            "category": category,
                            "batch_id": bid,
                            "amount": a_sh,
                            "notes": notes,
                        }
                    )
            continue

        # Legacy single-target line shape
        if kind == "batch_feed":
            raw_bid = str(item.get("batch_id") or "").strip()
            if not raw_bid.isdigit():
                raise ValueError("Choose a batch for each Meat Batch Feed line.")
            bid = _parse_owned_batch_id(raw_bid, owned_batch_ids)
            try:
                raw_bags = float(item.get("bags") or 0.0)
                raw_lbs = float(item.get("feed_lbs") or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid feed weight on Meat Batch Feed line.") from exc
            expanded.append(
                {
                    "kind": "batch_feed",
                    "batch_id": bid,
                    "feed_type": str(item.get("feed_type") or "grower").strip() or "grower",
                    "bags": _align_signed_qty(raw_bags, amt, 2),
                    "feed_lbs": _align_signed_qty(raw_lbs, amt, 1),
                    "amount": amt,
                    "notes": notes,
                }
            )
        else:
            raw_bid = str(item.get("batch_id") or "").strip()
            bid_opt = _parse_owned_batch_id(raw_bid, owned_batch_ids) if raw_bid else None
            expanded.append(
                {
                    "kind": "general",
                    "category": str(item.get("category") or "Other").strip() or "Other",
                    "batch_id": bid_opt,
                    "amount": amt,
                    "notes": notes,
                }
            )

    if not expanded:
        raise ValueError("Add at least one line item with a non-zero amount.")
    return expanded

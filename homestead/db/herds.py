"""Perennial herds (breeding pigs, fiber alpacas), named animals, and grain/hay feed logs."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homestead.db.core import get_db_connection
from homestead.db.fiber import (
    fiber_grade_label,
    fleece_factor_labels,
    get_alpaca_color_code,
    get_alpaca_color_hex,
    normalize_fiber_grade,
    normalize_fleece_factors,
    parse_fleece_factors,
)
from shared.bridge import delete_ledger_entry

HERD_TYPES = ("breeding", "fiber", "other")
HERD_SPECIES = ("pig", "alpaca", "goat", "sheep", "cattle", "other")
HERD_FEED_CATEGORIES = ("grain", "hay", "supplement")
HERD_FEED_UNITS = ("bags", "bales", "lbs")


def normalize_herd_species(raw: str | None) -> str:
    clean = str(raw or "").strip().lower()
    if clean in HERD_SPECIES:
        return clean
    if any(k in clean for k in ("pig", "swine", "hog", "kunekune", "boar", "sow")):
        return "pig"
    if "alpaca" in clean or "llama" in clean:
        return "alpaca"
    if "goat" in clean:
        return "goat"
    if "sheep" in clean or "lamb" in clean:
        return "sheep"
    if any(k in clean for k in ("cattle", "cow", "steer", "calf")):
        return "cattle"
    return "other"


def normalize_herd_type(raw: str | None, species: str = "pig") -> str:
    clean = str(raw or "").strip().lower()
    if clean in HERD_TYPES:
        return clean
    return "fiber" if normalize_herd_species(species) == "alpaca" else "breeding"


def normalize_herd_feed_category(raw: str | None) -> str:
    clean = str(raw or "").strip().lower()
    if clean in ("hay", "forage", "bale", "bales"):
        return "hay"
    if clean in ("supplement", "minerals", "mineral", "grit", "salt"):
        return "supplement"
    return "grain"


def normalize_herd_feed_unit(raw: str | None, category: str = "grain") -> str:
    clean = str(raw or "").strip().lower()
    if clean in ("bale", "bales"):
        return "bales"
    if clean in ("lb", "lbs", "pound", "pounds"):
        return "lbs"
    if clean in ("bag", "bags"):
        return "bags"
    return "bales" if normalize_herd_feed_category(category) == "hay" else "bags"


def herd_species_label(species: str) -> str:
    return {
        "pig": "Pigs",
        "alpaca": "Alpacas",
        "goat": "Goats",
        "sheep": "Sheep",
        "cattle": "Cattle",
    }.get(species, "Livestock")


def list_herds(user_id: int, include_inactive: bool = False) -> list[dict]:
    """List perennial herds enriched with active roster counts and YTD / 12-mo feed KPIs."""
    uid = int(user_id)
    today = date.today()
    ytd_prefix = f"{today.year}-"
    cutoff_12m = (today - timedelta(days=365)).isoformat()

    with get_db_connection() as conn:
        active_clause = "" if include_inactive else " AND active = 1"
        herd_rows = conn.execute(
            f"""
            SELECT * FROM herds
            WHERE user_id = ?{active_clause}
            ORDER BY active DESC, id ASC
            """,
            (uid,),
        ).fetchall()
        if not herd_rows:
            return []

        animal_rows = conn.execute(
            "SELECT herd_id, active FROM herd_animals WHERE user_id = ?",
            (uid,),
        ).fetchall()
        feed_rows = conn.execute(
            """
            SELECT herd_id, feed_date, feed_category, quantity, unit, weight_lbs, cost_total
            FROM herd_feed_logs
            WHERE user_id = ?
            """,
            (uid,),
        ).fetchall()

    counts_by_herd: dict[int, dict[str, int]] = {}
    for a in animal_rows:
        hid = int(a["herd_id"])
        acc = counts_by_herd.setdefault(hid, {"active": 0, "total": 0})
        acc["total"] += 1
        if int(a["active"] or 0):
            acc["active"] += 1

    feed_stats: dict[int, dict[str, float]] = {}
    for f in feed_rows:
        hid = int(f["herd_id"])
        st = feed_stats.setdefault(
            hid,
            {
                "ytd_feed_cost": 0.0,
                "trailing_12m_cost": 0.0,
                "ytd_grain_cost": 0.0,
                "ytd_hay_cost": 0.0,
                "ytd_supp_cost": 0.0,
                "ytd_grain_bags": 0.0,
                "ytd_hay_bales": 0.0,
                "ytd_weight_lbs": 0.0,
            },
        )
        fdate = str(f["feed_date"] or "")[:10]
        cost = float(f["cost_total"] or 0.0)
        qty = float(f["quantity"] or 0.0)
        lbs = float(f["weight_lbs"] or 0.0)
        cat = normalize_herd_feed_category(f["feed_category"])
        unit = normalize_herd_feed_unit(f["unit"], cat)
        if fdate >= cutoff_12m:
            st["trailing_12m_cost"] += cost
        if fdate.startswith(ytd_prefix):
            st["ytd_feed_cost"] += cost
            st["ytd_weight_lbs"] += lbs
            if cat == "hay":
                st["ytd_hay_cost"] += cost
                if unit == "bales":
                    st["ytd_hay_bales"] += qty
            elif cat == "supplement":
                st["ytd_supp_cost"] += cost
            else:
                st["ytd_grain_cost"] += cost
                if unit == "bags":
                    st["ytd_grain_bags"] += qty

    out: list[dict] = []
    for r in herd_rows:
        d = dict(r)
        hid = int(d["id"])
        cnt = counts_by_herd.get(hid, {"active": 0, "total": 0})
        st = feed_stats.get(
            hid,
            {
                "ytd_feed_cost": 0.0,
                "trailing_12m_cost": 0.0,
                "ytd_grain_cost": 0.0,
                "ytd_hay_cost": 0.0,
                "ytd_supp_cost": 0.0,
                "ytd_grain_bags": 0.0,
                "ytd_hay_bales": 0.0,
                "ytd_weight_lbs": 0.0,
            },
        )
        eff_head = cnt["active"] if cnt["total"] > 0 else max(0, int(d.get("head_count") or 0))
        ytd_cost = round(st["ytd_feed_cost"], 2)
        t12_cost = round(st["trailing_12m_cost"], 2)
        d.update(
            {
                "id": hid,
                "active": bool(d.get("active", 1)),
                "species": normalize_herd_species(d.get("species")),
                "species_label": herd_species_label(normalize_herd_species(d.get("species"))),
                "herd_type": normalize_herd_type(d.get("herd_type"), str(d.get("species") or "")),
                "active_animal_count": cnt["active"],
                "total_animal_count": cnt["total"],
                "effective_head_count": eff_head,
                "ytd_feed_cost": ytd_cost,
                "trailing_12m_cost": t12_cost,
                "ytd_grain_cost": round(st["ytd_grain_cost"], 2),
                "ytd_hay_cost": round(st["ytd_hay_cost"], 2),
                "ytd_supp_cost": round(st["ytd_supp_cost"], 2),
                "ytd_grain_bags": round(st["ytd_grain_bags"], 2),
                "ytd_hay_bales": round(st["ytd_hay_bales"], 2),
                "ytd_weight_lbs": round(st["ytd_weight_lbs"], 1),
                "cost_per_head_ytd": round(ytd_cost / eff_head, 2) if eff_head > 0 else 0.0,
                "trailing_12m_per_head": round(t12_cost / eff_head, 2) if eff_head > 0 else 0.0,
            }
        )
        out.append(d)
    return out


def get_herd(user_id: int, herd_id: int) -> dict | None:
    for h in list_herds(user_id, include_inactive=True):
        if int(h["id"]) == int(herd_id):
            return h
    return None


def save_herd(
    user_id: int,
    name: str,
    species: str = "pig",
    herd_type: str = "breeding",
    head_count: int = 0,
    notes: str = "",
    herd_id: int | None = None,
    active: bool = True,
) -> int:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Herd name is required.")
    sp = normalize_herd_species(species)
    htype = normalize_herd_type(herd_type, sp)
    hcount = max(0, int(head_count or 0))
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        if herd_id:
            cur = conn.execute(
                """
                UPDATE herds
                SET name = ?, species = ?, herd_type = ?, head_count = ?, active = ?, notes = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    clean_name[:120],
                    sp,
                    htype,
                    hcount,
                    1 if active else 0,
                    str(notes or "").strip(),
                    int(herd_id),
                    int(user_id),
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ValueError("Herd not found.")
            return int(herd_id)
        cur = conn.execute(
            """
            INSERT INTO herds (user_id, name, species, herd_type, head_count, active, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_name[:120],
                sp,
                htype,
                hcount,
                1 if active else 0,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_herd(user_id: int, herd_id: int) -> bool:
    uid = int(user_id)
    hid = int(herd_id)
    with get_db_connection() as conn:
        outs = conn.execute(
            """
            SELECT fo.ledger_entry_id
            FROM fiber_lot_outputs fo
            JOIN fiber_mill_lots ml ON ml.id = fo.lot_id
            WHERE ml.herd_id = ? AND ml.user_id = ? AND fo.ledger_entry_id IS NOT NULL
            """,
            (hid, uid),
        ).fetchall()
        for o in outs:
            delete_ledger_entry(int(o["ledger_entry_id"]), conn=conn)
        cur = conn.execute(
            "DELETE FROM herds WHERE id = ? AND user_id = ?",
            (hid, uid),
        )
        conn.commit()
        return cur.rowcount > 0


def list_herd_animals(
    user_id: int, herd_id: int | None = None, include_inactive: bool = False
) -> list[dict]:
    params: list = [int(user_id)]
    clauses = ["user_id = ?"]
    if herd_id is not None:
        clauses.append("herd_id = ?")
        params.append(int(herd_id))
    if not include_inactive:
        clauses.append("active = 1")
    where_sql = " AND ".join(clauses)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM herd_animals
            WHERE {where_sql}
            ORDER BY active DESC, LOWER(name) ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            clr = str(d.get("color") or "").strip()
            fgrade = normalize_fiber_grade(d.get("fiber_grade"))
            ffactors = normalize_fleece_factors(d.get("fleece_factors"))
            d["color"] = clr
            d["color_code"] = get_alpaca_color_code(clr)
            d["color_hex"] = get_alpaca_color_hex(clr)
            d["fiber_grade"] = fgrade
            d["fiber_grade_label"] = fiber_grade_label(fgrade)
            d["fleece_factors"] = ffactors
            d["fleece_factor_list"] = parse_fleece_factors(ffactors)
            d["fleece_factor_labels"] = fleece_factor_labels(ffactors)
            out.append(d)
        return out


def save_herd_animal(
    user_id: int,
    herd_id: int,
    name: str,
    sex_role: str = "",
    color: str = "",
    fiber_grade: str = "",
    fleece_factors: str | list[str] | tuple[str, ...] = "",
    birth_year: int | None = None,
    notes: str = "",
    active: bool = True,
    animal_id: int | None = None,
) -> int:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Animal name is required.")
    yr_val = int(birth_year) if birth_year and 1900 <= int(birth_year) <= 2200 else None
    clean_grade = normalize_fiber_grade(fiber_grade)
    clean_factors = normalize_fleece_factors(fleece_factors)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        owned = conn.execute(
            "SELECT id FROM herds WHERE id = ? AND user_id = ?",
            (int(herd_id), int(user_id)),
        ).fetchone()
        if not owned:
            raise ValueError("Herd not found.")
        if animal_id:
            cur = conn.execute(
                """
                UPDATE herd_animals
                SET name = ?, sex_role = ?, color = ?, fiber_grade = ?,
                    fleece_factors = ?, birth_year = ?, active = ?, notes = ?
                WHERE id = ? AND herd_id = ? AND user_id = ?
                """,
                (
                    clean_name[:80],
                    str(sex_role or "").strip()[:40],
                    str(color or "").strip()[:60],
                    clean_grade,
                    clean_factors,
                    yr_val,
                    1 if active else 0,
                    str(notes or "").strip(),
                    int(animal_id),
                    int(herd_id),
                    int(user_id),
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise ValueError("Animal not found.")
            return int(animal_id)
        cur = conn.execute(
            """
            INSERT INTO herd_animals (
                herd_id, user_id, name, sex_role, color, fiber_grade,
                fleece_factors, birth_year, active, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(herd_id),
                int(user_id),
                clean_name[:80],
                str(sex_role or "").strip()[:40],
                str(color or "").strip()[:60],
                clean_grade,
                clean_factors,
                yr_val,
                1 if active else 0,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def toggle_herd_animal_active(user_id: int, animal_id: int, active: bool) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE herd_animals SET active = ? WHERE id = ? AND user_id = ?",
            (1 if active else 0, int(animal_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_herd_animal(user_id: int, animal_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM herd_animals WHERE id = ? AND user_id = ?",
            (int(animal_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def add_herd_feed_log(
    user_id: int,
    herd_id: int,
    feed_date: str,
    feed_category: str = "grain",
    quantity: float = 0.0,
    unit: str = "bags",
    weight_lbs: float = 0.0,
    cost_total: float = 0.0,
    vendor: str = "",
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
    receipt_group_id: str = "",
) -> int:
    clean_date = (feed_date or date.today().isoformat()).strip()[:10]
    fcat = normalize_herd_feed_category(feed_category)
    funit = normalize_herd_feed_unit(unit, fcat)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        owned = conn.execute(
            "SELECT id FROM herds WHERE id = ? AND user_id = ?",
            (int(herd_id), int(user_id)),
        ).fetchone()
        if not owned:
            raise ValueError("Herd not found.")
        cur = conn.execute(
            """
            INSERT INTO herd_feed_logs (
                user_id, herd_id, feed_date, feed_category, quantity, unit,
                weight_lbs, cost_total, vendor, notes, receipt_url,
                drive_file_id, receipt_group_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                int(herd_id),
                clean_date,
                fcat,
                round(float(quantity or 0.0), 2),
                funit,
                round(float(weight_lbs or 0.0), 1),
                round(float(cost_total or 0.0), 2),
                str(vendor or "").strip(),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                str(receipt_group_id or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_herd_feed_logs(
    user_id: int, herd_id: int | None = None, limit: int = 500
) -> list[dict]:
    params: list = [int(user_id)]
    herd_clause = ""
    if herd_id is not None:
        herd_clause = " AND fl.herd_id = ?"
        params.append(int(herd_id))
    params.append(int(limit))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT fl.*, h.name AS herd_name, h.species AS herd_species, h.herd_type
            FROM herd_feed_logs fl
            JOIN herds h ON h.id = fl.herd_id
            WHERE fl.user_id = ?{herd_clause}
            ORDER BY fl.feed_date DESC, fl.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def attach_herd_feed_receipt(
    log_id: int,
    user_id: int,
    receipt_url: str,
    drive_file_id: str = "",
) -> bool:
    clean_url = str(receipt_url or "").strip()
    if not clean_url:
        return False
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            UPDATE herd_feed_logs
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ? AND user_id = ?
            """,
            (clean_url, str(drive_file_id or "").strip(), int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_herd_feed_log(user_id: int, log_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM herd_feed_logs WHERE id = ? AND user_id = ?",
            (int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0

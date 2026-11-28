"""Alpaca/fiber annual shearings, color-separated fleece lots, mill fees, yarn conversion, and Iron Ledger SKU sync."""

from __future__ import annotations

from datetime import date, datetime

from homestead.db.core import get_db_connection
from shared.bridge import (
    delete_ledger_entry,
    get_sku_completed_sales_summary,
    list_active_ledger_skus,
    restock_ledger_sku,
)

MILL_LOT_STATUSES = ("draft", "sent_to_mill", "received")

FIBER_PRODUCT_TYPES = (
    ("yarn_skein", "Yarn Skeins"),
    ("roving_bump", "Roving / Batts"),
    ("rug_yarn", "Rug Yarn"),
    ("felt_goods", "Felted Goods / Dryer Balls"),
    ("raw_fleece", "Raw Fleece Sale"),
    ("other", "Other Fiber Product"),
)
_PRODUCT_TYPE_MAP = dict(FIBER_PRODUCT_TYPES)

FIBER_OUTPUT_UNITS = ("skeins", "bumps", "pairs", "items", "oz", "lbs")
FIBER_SKU_KEYWORDS = ("yarn", "alpaca", "fiber", "roving", "skein", "wool", "fleece", "felt")

# Official 16 AOA alpaca fleece colors (4x4 palette order)
ALPACA_FLEECE_COLORS: tuple[dict[str, object], ...] = (
    {"name": "White", "short": "White", "code": "WH", "hex": "#F7F5EE", "is_light": True},
    {"name": "Beige", "short": "Beige", "code": "BG", "hex": "#E6D5B8", "is_light": True},
    {"name": "Light Fawn", "short": "Lt Fawn", "code": "LF", "hex": "#D7B996", "is_light": True},
    {"name": "Medium Fawn", "short": "Med Fawn", "code": "MF", "hex": "#BC9368", "is_light": False},
    {"name": "Dark Fawn", "short": "Dk Fawn", "code": "DF", "hex": "#9A6E43", "is_light": False},
    {"name": "Light Brown", "short": "Lt Brown", "code": "LB", "hex": "#84573A", "is_light": False},
    {"name": "Medium Brown", "short": "Med Brown", "code": "MB", "hex": "#633D28", "is_light": False},
    {"name": "Dark Brown", "short": "Dk Brown", "code": "DB", "hex": "#3F2519", "is_light": False},
    {"name": "Bay Black", "short": "Bay Black", "code": "BB", "hex": "#2B211E", "is_light": False},
    {"name": "True Black", "short": "True Black", "code": "TB", "hex": "#161616", "is_light": False},
    {"name": "Light Silver Grey", "short": "Lt Silv Grey", "code": "LSG", "hex": "#C2C4C4", "is_light": True},
    {"name": "Medium Silver Grey", "short": "Med Silv Grey", "code": "MSG", "hex": "#8F9396", "is_light": False},
    {"name": "Dark Silver Grey", "short": "Dk Silv Grey", "code": "DSG", "hex": "#575B60", "is_light": False},
    {"name": "Light Rose Grey", "short": "Lt Rose Grey", "code": "LRG", "hex": "#B5A39B", "is_light": True},
    {"name": "Medium Rose Grey", "short": "Med Rose Grey", "code": "MRG", "hex": "#8A736A", "is_light": False},
    {"name": "Dark Rose Grey", "short": "Dk Rose Grey", "code": "DRG", "hex": "#5E4B44", "is_light": False},
)
_COLOR_BY_NAME = {str(c["name"]).lower(): c for c in ALPACA_FLEECE_COLORS}
_COLOR_BY_CODE = {str(c["code"]).lower(): c for c in ALPACA_FLEECE_COLORS}

# US / AOA numbered fiber grades based on average fiber diameter (microns)
ALPACA_FIBER_GRADES: tuple[tuple[str, str, str], ...] = (
    ("grade_0", "Grade 0 · Ultra Royal", "Grade 0 · Ultra Royal (<17 µm)"),
    ("grade_1", "Grade 1 · Royal", "Grade 1 · Royal (17–19.9 µm)"),
    ("grade_2", "Grade 2 · Baby", "Grade 2 · Baby (20–22.9 µm)"),
    ("grade_3", "Grade 3 · Superfine", "Grade 3 · Superfine (23–25.9 µm)"),
    ("grade_4", "Grade 4 · Fine", "Grade 4 · Fine / Adult (26–28.9 µm)"),
    ("grade_5", "Grade 5 · Medium", "Grade 5 · Medium (29–31.9 µm)"),
    ("grade_6", "Grade 6 · Coarse", "Grade 6 · Coarse / Rug (32+ µm)"),
)
_GRADE_SHORT_MAP = {k: short for k, short, _ in ALPACA_FIBER_GRADES}
_GRADE_FULL_MAP = {k: full for k, _, full in ALPACA_FIBER_GRADES}

ALPACA_ANIMAL_FACTORS: tuple[tuple[str, str, str], ...] = (
    ("tuxedoed", "Tuxedoed", "Contrasting white chest/belly markings"),
    ("pinto", "Pinto / Multi", "Distinct multi-color patches"),
    ("appaloosa", "Appaloosa", "Spotted or speckled coat pattern"),
    ("roan", "Roan", "White fibers evenly mixed through colored coat"),
    ("suri", "Suri", "Silky lock fleece — mill separately from Huacaya"),
    ("high_guard_hair", "High Guard Hair", "Coarse guard hairs (dehairing / rug-grade)"),
)
ALPACA_CLIP_EXTRA_FACTORS: tuple[tuple[str, str, str], ...] = (
    ("heavy_vm", "Heavy VM", "Noticeable hay or vegetable matter in clip"),
    ("tender_break", "Tender / Break", "Weak staple spot (best for felting / roving)"),
)
ALPACA_SHEARING_FACTORS: tuple[tuple[str, str, str], ...] = (
    ALPACA_ANIMAL_FACTORS + ALPACA_CLIP_EXTRA_FACTORS
)
_FACTOR_LABEL_MAP = {k: lbl for k, lbl, _ in ALPACA_SHEARING_FACTORS}
_FACTOR_ORDER = [k for k, _, _ in ALPACA_SHEARING_FACTORS]


def get_alpaca_color_info(color_name: str | None) -> dict[str, object] | None:
    """Return official AOA color metadata by name or ARI code, case-insensitively."""
    clean = str(color_name or "").strip().lower()
    if not clean:
        return None
    return _COLOR_BY_NAME.get(clean) or _COLOR_BY_CODE.get(clean)


def get_alpaca_color_code(color_name: str | None) -> str:
    """Return AOA short code (e.g. 'MF') for an official fleece color, or '' if custom/empty."""
    info = get_alpaca_color_info(color_name)
    return str(info["code"]) if info else ""


def get_alpaca_color_hex(color_name: str | None) -> str:
    """Return display hex swatch for an official fleece color, or '' if custom/empty."""
    info = get_alpaca_color_info(color_name)
    return str(info["hex"]) if info else ""


def normalize_fiber_grade(raw: str | None) -> str:
    """Normalize fiber grade keys/labels while allowing legacy/custom strings."""
    clean = str(raw or "").strip()
    if not clean:
        return ""
    low = clean.lower()
    if low in _GRADE_SHORT_MAP:
        return low
    for key, short, full in ALPACA_FIBER_GRADES:
        if low in (short.lower(), full.lower()):
            return key
    return clean[:40]


def fiber_grade_label(raw: str | None, *, short: bool = True) -> str:
    """Format a normalized or custom fiber grade for display."""
    norm = normalize_fiber_grade(raw)
    if not norm:
        return ""
    mapping = _GRADE_SHORT_MAP if short else _GRADE_FULL_MAP
    return mapping.get(norm, norm)


def parse_fleece_factors(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Parse canonical fleece factor keys in stable display order."""
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    else:
        tokens = [str(t or "").strip().lower() for t in raw if str(t or "").strip()]
    present = set(tokens)
    return [k for k in _FACTOR_ORDER if k in present]


def normalize_fleece_factors(raw: str | list[str] | tuple[str, ...] | None) -> str:
    """Serialize deduplicated canonical fleece factor keys as comma-separated text."""
    return ",".join(parse_fleece_factors(raw))


def fleece_factor_labels(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Return human-readable labels for parsed fleece factor keys."""
    return [_FACTOR_LABEL_MAP[k] for k in parse_fleece_factors(raw) if k in _FACTOR_LABEL_MAP]


def normalize_mill_status(val: str | None) -> str:
    clean = str(val or "").strip().lower()
    return clean if clean in MILL_LOT_STATUSES else "sent_to_mill"


def normalize_product_type(val: str | None) -> str:
    clean = str(val or "").strip().lower()
    return clean if clean in _PRODUCT_TYPE_MAP else "yarn_skein"


def product_type_label(val: str | None) -> str:
    return _PRODUCT_TYPE_MAP.get(normalize_product_type(val), "Yarn Skeins")


def normalize_output_unit(val: str | None) -> str:
    clean = str(val or "").strip().lower()
    return clean if clean in FIBER_OUTPUT_UNITS else "skeins"


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def add_fiber_shearing(
    user_id: int,
    herd_id: int,
    *,
    animal_id: int | None = None,
    shear_date: str = "",
    fleece_color: str = "",
    fiber_grade: str = "",
    fleece_factors: str | list[str] | tuple[str, ...] | None = None,
    prime_lbs: float = 0.0,
    seconds_lbs: float = 0.0,
    total_lbs: float | None = None,
    shearing_fee: float = 0.0,
    notes: str = "",
) -> int:
    uid = int(user_id)
    hid = int(herd_id)
    clean_date = str(shear_date or "").strip()[:10] or date.today().isoformat()
    prime = round(max(0.0, float(prime_lbs or 0.0)), 2)
    seconds = round(max(0.0, float(seconds_lbs or 0.0)), 2)
    if prime > 0 or seconds > 0:
        total = round(prime + seconds, 2)
    else:
        total = round(max(0.0, float(total_lbs or 0.0)), 2)
    if total <= 0:
        raise ValueError("Enter raw fiber weight greater than 0 lb.")
    fee = round(max(0.0, float(shearing_fee or 0.0)), 2)
    clean_color = str(fleece_color or "").strip()[:60]
    clean_grade = normalize_fiber_grade(fiber_grade)
    resolved_factors: str | list[str] | tuple[str, ...] | None = fleece_factors
    aid = int(animal_id) if animal_id else None
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        herd_row = conn.execute(
            "SELECT id FROM herds WHERE id = ? AND user_id = ?",
            (hid, uid),
        ).fetchone()
        if not herd_row:
            raise ValueError("Herd not found.")
        if aid is not None:
            anm_row = conn.execute(
                "SELECT id, color, fiber_grade, fleece_factors FROM herd_animals"
                " WHERE id = ? AND herd_id = ? AND user_id = ?",
                (aid, hid, uid),
            ).fetchone()
            if not anm_row:
                raise ValueError("Animal not found.")
            if not clean_color:
                clean_color = str(anm_row["color"] or "").strip()[:60]
            if not clean_grade:
                clean_grade = normalize_fiber_grade(anm_row["fiber_grade"])
            if resolved_factors is None:
                resolved_factors = str(anm_row["fleece_factors"] or "")

        clean_factors = normalize_fleece_factors(resolved_factors)
        cur = conn.execute(
            """
            INSERT INTO fiber_shearings (
                user_id, herd_id, animal_id, shear_date, fleece_color,
                fiber_grade, fleece_factors,
                prime_lbs, seconds_lbs, total_lbs, shearing_fee, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                hid,
                aid,
                clean_date,
                clean_color,
                clean_grade,
                clean_factors,
                prime,
                seconds,
                total,
                fee,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_fiber_shearings(
    user_id: int,
    herd_id: int | None = None,
    year: int | None = None,
) -> list[dict]:
    clauses = ["fs.user_id = ?"]
    params: list[object] = [int(user_id)]
    if herd_id is not None:
        clauses.append("fs.herd_id = ?")
        params.append(int(herd_id))
    if year is not None:
        clauses.append("substr(fs.shear_date, 1, 4) = ?")
        params.append(str(int(year)))
    where_sql = " AND ".join(clauses)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT fs.*,
                   COALESCE(ha.name, '') AS animal_name,
                   COALESCE(ha.color, '') AS animal_color,
                   COALESCE(h.name, '') AS herd_name,
                   COALESCE((
                       SELECT SUM(li.raw_lbs_used)
                       FROM fiber_mill_lot_inputs li
                       WHERE li.shearing_id = fs.id
                   ), 0) AS allocated_lbs
            FROM fiber_shearings fs
            JOIN herds h ON h.id = fs.herd_id
            LEFT JOIN herd_animals ha ON ha.id = fs.animal_id
            WHERE {where_sql}
            ORDER BY fs.shear_date DESC, fs.id DESC
            """,
            tuple(params),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        tot = round(float(d.get("total_lbs") or 0.0), 2)
        alloc = round(float(d.get("allocated_lbs") or 0.0), 2)
        fcolor = str(d.get("fleece_color") or d.get("animal_color") or "").strip()
        fgrade = normalize_fiber_grade(d.get("fiber_grade"))
        ffactors = normalize_fleece_factors(d.get("fleece_factors"))
        factor_list = parse_fleece_factors(ffactors)
        d["total_lbs"] = tot
        d["allocated_lbs"] = alloc
        d["remaining_lbs"] = round(max(0.0, tot - alloc), 2)
        d["fleece_color"] = fcolor
        d["color_code"] = get_alpaca_color_code(fcolor)
        d["color_hex"] = get_alpaca_color_hex(fcolor)
        d["fiber_grade"] = fgrade
        d["fiber_grade_label"] = fiber_grade_label(fgrade)
        d["fleece_factors"] = ffactors
        d["fleece_factor_list"] = factor_list
        d["fleece_factor_labels"] = fleece_factor_labels(ffactors)
        d["is_suri"] = "suri" in factor_list
        out.append(d)
    return out


def delete_fiber_shearing(user_id: int, shearing_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM fiber_shearings WHERE id = ? AND user_id = ?",
            (int(shearing_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def save_fiber_mill_lot(
    user_id: int,
    herd_id: int,
    *,
    lot_name: str,
    mill_name: str = "",
    color_group: str = "",
    status: str = "sent_to_mill",
    sent_date: str = "",
    received_date: str = "",
    shipping_out_cost: float = 0.0,
    milling_fee: float = 0.0,
    shipping_back_cost: float = 0.0,
    other_handling_fee: float = 0.0,
    include_in_tax_books: bool = True,
    receipt_url: str = "",
    drive_file_id: str = "",
    notes: str = "",
    lot_id: int | None = None,
    inputs: list[dict] | None = None,
) -> int:
    uid = int(user_id)
    hid = int(herd_id)
    clean_name = str(lot_name or "").strip()
    if not clean_name:
        raise ValueError("Lot name is required.")
    clean_status = normalize_mill_status(status)
    clean_mill = str(mill_name or "").strip()[:120]
    clean_color = str(color_group or "").strip()[:80]
    clean_sent = str(sent_date or "").strip()[:10]
    clean_recv = str(received_date or "").strip()[:10]
    ship_out = round(max(0.0, float(shipping_out_cost or 0.0)), 2)
    mill_fee = round(max(0.0, float(milling_fee or 0.0)), 2)
    ship_back = round(max(0.0, float(shipping_back_cost or 0.0)), 2)
    other_fee = round(max(0.0, float(other_handling_fee or 0.0)), 2)
    inc_tax = 1 if include_in_tax_books else 0
    clean_url = str(receipt_url or "").strip()
    clean_drive = str(drive_file_id or "").strip()
    clean_notes = str(notes or "").strip()
    lid = int(lot_id) if lot_id else None
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        herd_row = conn.execute(
            "SELECT id FROM herds WHERE id = ? AND user_id = ?",
            (hid, uid),
        ).fetchone()
        if not herd_row:
            raise ValueError("Herd not found.")

        if lid is not None:
            existing_lot = conn.execute(
                "SELECT id, receipt_url, drive_file_id FROM fiber_mill_lots"
                " WHERE id = ? AND herd_id = ? AND user_id = ?",
                (lid, hid, uid),
            ).fetchone()
            if not existing_lot:
                raise ValueError("Mill lot not found.")
            if not clean_url and existing_lot["receipt_url"]:
                clean_url = str(existing_lot["receipt_url"] or "")
                clean_drive = str(existing_lot["drive_file_id"] or "")

        validated_inputs: list[tuple[int, float]] = []
        input_colors: list[str] = []
        seen_color_keys: set[str] = set()
        if inputs is not None:
            merged_by_shearing: dict[int, float] = {}
            for item in inputs:
                sid = int(item.get("shearing_id") or 0)
                raw_lbs = round(float(item.get("raw_lbs_used") or 0.0), 2)
                if sid <= 0 or raw_lbs <= 0:
                    raise ValueError("Allocated raw fleece weight must be greater than 0 lb.")
                merged_by_shearing[sid] = round(merged_by_shearing.get(sid, 0.0) + raw_lbs, 2)

            for sid, raw_lbs in merged_by_shearing.items():
                sh_row = conn.execute(
                    """
                    SELECT fs.id, fs.total_lbs, fs.fleece_color, COALESCE(ha.color, '') AS animal_color
                    FROM fiber_shearings fs
                    LEFT JOIN herd_animals ha ON ha.id = fs.animal_id
                    WHERE fs.id = ? AND fs.herd_id = ? AND fs.user_id = ?
                    """,
                    (sid, hid, uid),
                ).fetchone()
                if not sh_row:
                    raise ValueError("Selected shearing does not belong to this herd.")
                total_lbs = float(sh_row["total_lbs"] or 0.0)
                if lid is not None:
                    used_row = conn.execute(
                        "SELECT COALESCE(SUM(raw_lbs_used), 0) FROM fiber_mill_lot_inputs"
                        " WHERE shearing_id = ? AND lot_id != ?",
                        (sid, lid),
                    ).fetchone()
                else:
                    used_row = conn.execute(
                        "SELECT COALESCE(SUM(raw_lbs_used), 0) FROM fiber_mill_lot_inputs"
                        " WHERE shearing_id = ?",
                        (sid,),
                    ).fetchone()
                used_other = float(used_row[0] or 0.0)
                avail = round(max(0.0, total_lbs - used_other), 2)
                if raw_lbs > (avail + 1e-6):
                    raise ValueError(
                        f"Cannot allocate {raw_lbs:.2f} lb — only {avail:.2f} lb remaining on that fleece."
                    )
                validated_inputs.append((sid, raw_lbs))
                clr = str(sh_row["fleece_color"] or sh_row["animal_color"] or "").strip()
                if clr and clr.lower() not in seen_color_keys:
                    seen_color_keys.add(clr.lower())
                    input_colors.append(clr)

            if not clean_color and input_colors:
                clean_color = ", ".join(input_colors)[:80]

        if lid is not None:
            conn.execute(
                """
                UPDATE fiber_mill_lots
                SET lot_name = ?, mill_name = ?, color_group = ?, status = ?,
                    sent_date = ?, received_date = ?,
                    shipping_out_cost = ?, milling_fee = ?, shipping_back_cost = ?,
                    other_handling_fee = ?, include_in_tax_books = ?,
                    receipt_url = ?, drive_file_id = ?, notes = ?
                WHERE id = ? AND herd_id = ? AND user_id = ?
                """,
                (
                    clean_name[:120],
                    clean_mill,
                    clean_color,
                    clean_status,
                    clean_sent,
                    clean_recv,
                    ship_out,
                    mill_fee,
                    ship_back,
                    other_fee,
                    inc_tax,
                    clean_url,
                    clean_drive,
                    clean_notes,
                    lid,
                    hid,
                    uid,
                ),
            )
            saved_lot_id = lid
        else:
            cur = conn.execute(
                """
                INSERT INTO fiber_mill_lots (
                    user_id, herd_id, lot_name, mill_name, color_group, status,
                    sent_date, received_date,
                    shipping_out_cost, milling_fee, shipping_back_cost, other_handling_fee,
                    include_in_tax_books, receipt_url, drive_file_id, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    hid,
                    clean_name[:120],
                    clean_mill,
                    clean_color,
                    clean_status,
                    clean_sent,
                    clean_recv,
                    ship_out,
                    mill_fee,
                    ship_back,
                    other_fee,
                    inc_tax,
                    clean_url,
                    clean_drive,
                    clean_notes,
                    now_iso,
                ),
            )
            saved_lot_id = int(cur.lastrowid)

        if inputs is not None:
            conn.execute(
                "DELETE FROM fiber_mill_lot_inputs WHERE lot_id = ?",
                (saved_lot_id,),
            )
            for sid, raw_lbs in validated_inputs:
                conn.execute(
                    """
                    INSERT INTO fiber_mill_lot_inputs (lot_id, shearing_id, raw_lbs_used)
                    VALUES (?, ?, ?)
                    """,
                    (saved_lot_id, sid, raw_lbs),
                )
        conn.commit()
        return saved_lot_id


def list_fiber_mill_lots(
    user_id: int,
    herd_id: int | None = None,
    sku_map: dict[int, dict] | None = None,
) -> list[dict]:
    clauses = ["ml.user_id = ?"]
    params: list[object] = [int(user_id)]
    if herd_id is not None:
        clauses.append("ml.herd_id = ?")
        params.append(int(herd_id))
    where_sql = " AND ".join(clauses)

    with get_db_connection() as conn:
        lot_rows = conn.execute(
            f"""
            SELECT ml.*, COALESCE(h.name, '') AS herd_name
            FROM fiber_mill_lots ml
            JOIN herds h ON h.id = ml.herd_id
            WHERE {where_sql}
            ORDER BY COALESCE(NULLIF(ml.received_date, ''), NULLIF(ml.sent_date, ''), substr(ml.created_at, 1, 10)) DESC,
                     ml.id DESC
            """,
            tuple(params),
        ).fetchall()
        if not lot_rows:
            return []

        lot_ids = [int(r["id"]) for r in lot_rows]
        marks = ",".join("?" * len(lot_ids))
        in_rows = conn.execute(
            f"""
            SELECT li.*, fs.animal_id, fs.shear_date, fs.fleece_color,
                   fs.fiber_grade, fs.fleece_factors,
                   fs.total_lbs AS shearing_total_lbs, fs.shearing_fee,
                   COALESCE(ha.name, '') AS animal_name,
                   COALESCE(ha.color, '') AS animal_color
            FROM fiber_mill_lot_inputs li
            JOIN fiber_shearings fs ON fs.id = li.shearing_id
            LEFT JOIN herd_animals ha ON ha.id = fs.animal_id
            WHERE li.lot_id IN ({marks})
            ORDER BY fs.shear_date ASC, li.id ASC
            """,
            lot_ids,
        ).fetchall()
        out_rows = conn.execute(
            f"""
            SELECT *
            FROM fiber_lot_outputs
            WHERE lot_id IN ({marks})
            ORDER BY id ASC
            """,
            lot_ids,
        ).fetchall()

    inputs_by_lot: dict[int, list[dict]] = {}
    for ir in in_rows:
        d = dict(ir)
        sh_tot = float(d.get("shearing_total_lbs") or 0.0)
        used = round(float(d.get("raw_lbs_used") or 0.0), 2)
        fee_share = round(float(d.get("shearing_fee") or 0.0) * (used / sh_tot), 2) if sh_tot > 0 else 0.0
        fcolor = str(d.get("fleece_color") or d.get("animal_color") or "").strip()
        fgrade = normalize_fiber_grade(d.get("fiber_grade"))
        ffactors = normalize_fleece_factors(d.get("fleece_factors"))
        factor_list = parse_fleece_factors(ffactors)
        d["raw_lbs_used"] = used
        d["shearing_fee_share"] = fee_share
        d["fleece_color"] = fcolor
        d["color_code"] = get_alpaca_color_code(fcolor)
        d["fiber_grade"] = fgrade
        d["fiber_grade_label"] = fiber_grade_label(fgrade)
        d["fleece_factor_list"] = factor_list
        d["fleece_factor_labels"] = fleece_factor_labels(ffactors)
        d["is_suri"] = "suri" in factor_list
        inputs_by_lot.setdefault(int(d["lot_id"]), []).append(d)

    need_skus = sku_map is None and any(r["ledger_sku_id"] for r in out_rows)
    resolved_sku_map = sku_map if sku_map is not None else (
        {s["id"]: s for s in list_active_ledger_skus()} if need_skus else {}
    )

    outputs_by_lot: dict[int, list[dict]] = {}
    for orow in out_rows:
        outputs_by_lot.setdefault(int(orow["lot_id"]), []).append(dict(orow))

    result: list[dict] = []
    for lr in lot_rows:
        d = dict(lr)
        lid = int(d["id"])
        lot_inputs = inputs_by_lot.get(lid, [])
        lot_outputs = outputs_by_lot.get(lid, [])

        distinct_colors: list[str] = []
        seen_c: set[str] = set()
        for inp in lot_inputs:
            c = str(inp.get("fleece_color") or "").strip()
            if c and c.lower() not in seen_c:
                seen_c.add(c.lower())
                distinct_colors.append(c)

        total_raw_lbs = round(sum(inp["raw_lbs_used"] for inp in lot_inputs), 2)
        allocated_shearing_cost = round(sum(inp["shearing_fee_share"] for inp in lot_inputs), 2)
        total_mill_cost = round(
            float(d.get("shipping_out_cost") or 0.0)
            + float(d.get("milling_fee") or 0.0)
            + float(d.get("shipping_back_cost") or 0.0)
            + float(d.get("other_handling_fee") or 0.0),
            2,
        )
        total_direct_cogs = round(total_mill_cost + allocated_shearing_cost, 2)

        total_finished_oz = round(sum(float(o.get("finished_weight_oz") or 0.0) for o in lot_outputs), 2)
        total_finished_lbs = round(total_finished_oz / 16.0, 3)
        total_output_units = round(sum(float(o.get("quantity") or 0.0) for o in lot_outputs), 2)
        yield_pct = round((total_finished_lbs / total_raw_lbs) * 100.0, 1) if total_raw_lbs > 0 else 0.0

        use_oz_weights = total_finished_oz > 0 and all(float(o.get("finished_weight_oz") or 0.0) > 0 for o in lot_outputs)
        weight_denom = total_finished_oz if use_oz_weights else total_output_units

        enriched_outputs: list[dict] = []
        for o in lot_outputs:
            qty = round(float(o.get("quantity") or 0.0), 2)
            oz = round(float(o.get("finished_weight_oz") or 0.0), 2)
            o_wt = oz if use_oz_weights else qty
            share_ratio = (o_wt / weight_denom) if weight_denom > 0 else 0.0
            alloc_cogs = round(total_direct_cogs * share_ratio, 2)
            cost_per_unit = round(alloc_cogs / qty, 2) if qty > 0 else 0.0

            sku_id = int(o["ledger_sku_id"]) if o.get("ledger_sku_id") else None
            sku_info = resolved_sku_map.get(sku_id) if sku_id else None
            sku_price = round(float(sku_info["price_cents"]) / 100.0, 2) if sku_info else None
            margin_unit = round(sku_price - cost_per_unit, 2) if sku_price is not None else None
            units_sold, sales_rev = (
                get_sku_completed_sales_summary(sku_id) if sku_id else (0.0, 0.0)
            )
            enriched_outputs.append(
                {
                    **o,
                    "product_type_label": product_type_label(o.get("product_type")),
                    "quantity": qty,
                    "finished_weight_oz": oz,
                    "finished_lbs": round(oz / 16.0, 3),
                    "allocated_cogs": alloc_cogs,
                    "cost_per_unit": cost_per_unit,
                    "sku_label": sku_info["label"] if sku_info else (f"SKU #{sku_id}" if sku_id else ""),
                    "sku_price_dollars": sku_price,
                    "margin_per_unit": margin_unit,
                    "units_sold": units_sold,
                    "sales_revenue": sales_rev,
                    "synced": bool(o.get("ledger_entry_id")),
                }
            )

        has_suri = any(inp["is_suri"] for inp in lot_inputs)
        has_non_suri = any(not inp["is_suri"] for inp in lot_inputs)
        d.update(
            {
                "include_in_tax_books": bool(d.get("include_in_tax_books", 1)),
                "inputs": lot_inputs,
                "outputs": enriched_outputs,
                "distinct_colors": distinct_colors,
                "color_code": get_alpaca_color_code(d.get("color_group")),
                "mixed_color_warning": len(distinct_colors) > 1,
                "mixed_suri_warning": has_suri and has_non_suri,
                "total_raw_lbs": total_raw_lbs,
                "total_mill_cost": total_mill_cost,
                "allocated_shearing_cost": allocated_shearing_cost,
                "total_direct_cogs": total_direct_cogs,
                "total_finished_oz": total_finished_oz,
                "total_finished_lbs": total_finished_lbs,
                "total_output_units": total_output_units,
                "yield_pct": yield_pct,
            }
        )
        result.append(d)
    return result


def delete_fiber_mill_lot(user_id: int, lot_id: int) -> bool:
    uid = int(user_id)
    lid = int(lot_id)
    with get_db_connection() as conn:
        lot = conn.execute(
            "SELECT id FROM fiber_mill_lots WHERE id = ? AND user_id = ?",
            (lid, uid),
        ).fetchone()
        if not lot:
            return False
        outs = conn.execute(
            "SELECT ledger_entry_id FROM fiber_lot_outputs WHERE lot_id = ? AND user_id = ?",
            (lid, uid),
        ).fetchall()
        for o in outs:
            if o["ledger_entry_id"]:
                delete_ledger_entry(int(o["ledger_entry_id"]), conn=conn)
        conn.execute(
            "DELETE FROM fiber_mill_lots WHERE id = ? AND user_id = ?",
            (lid, uid),
        )
        conn.commit()
        return True


def attach_fiber_mill_lot_receipt(
    lot_id: int,
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
            UPDATE fiber_mill_lots
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ? AND user_id = ?
            """,
            (clean_url, str(drive_file_id or "").strip(), int(lot_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def add_fiber_lot_output(
    user_id: int,
    lot_id: int,
    *,
    product_type: str = "yarn_skein",
    product_name: str = "",
    unit: str = "skeins",
    quantity: float = 1.0,
    finished_weight_oz: float = 0.0,
    ledger_sku_id: int | None = None,
    sync_to_ledger: bool = True,
    notes: str = "",
    created_by: int | None = None,
) -> dict:
    uid = int(user_id)
    lid = int(lot_id)
    clean_name = str(product_name or "").strip()
    if not clean_name:
        raise ValueError("Finished product name is required.")
    qty = round(float(quantity or 0.0), 2)
    if qty <= 0:
        raise ValueError("Output quantity must be greater than 0.")
    oz = round(max(0.0, float(finished_weight_oz or 0.0)), 2)
    ptype = normalize_product_type(product_type)
    u = normalize_output_unit(unit)
    actor = int(created_by if created_by is not None else uid)
    now_iso = _now_utc_iso()
    today_iso = date.today().isoformat()

    with get_db_connection() as conn:
        lot = conn.execute(
            "SELECT * FROM fiber_mill_lots WHERE id = ? AND user_id = ?",
            (lid, uid),
        ).fetchone()
        if not lot:
            raise ValueError("Mill lot not found.")

        sku_id = int(ledger_sku_id) if ledger_sku_id else None
        ledger_entry_id = None
        if sku_id and sync_to_ledger:
            sku_res, entry_res = restock_ledger_sku(
                sku_id,
                qty,
                note=f"Milled {qty:g} {u} {clean_name} ({lot['lot_name']})",
                created_by=actor,
                created_at=now_iso,
                conn=conn,
            )
            if sku_res is None:
                raise ValueError("Selected Iron Ledger product was not found.")
            sku_id, ledger_entry_id = sku_res, entry_res

        cur = conn.execute(
            """
            INSERT INTO fiber_lot_outputs (
                user_id, lot_id, product_type, product_name, unit, quantity,
                finished_weight_oz, ledger_sku_id, ledger_entry_id, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                lid,
                ptype,
                clean_name[:120],
                u,
                qty,
                oz,
                sku_id,
                ledger_entry_id,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        if str(lot["status"] or "") != "received":
            recv_dt = str(lot["received_date"] or "").strip() or today_iso
            conn.execute(
                "UPDATE fiber_mill_lots SET status = 'received', received_date = ? WHERE id = ?",
                (recv_dt, lid),
            )
        conn.commit()
        return {
            "output_id": int(cur.lastrowid),
            "lot_id": lid,
            "quantity": qty,
            "ledger_sku_id": sku_id,
            "ledger_entry_id": ledger_entry_id,
            "synced_to_ledger": bool(ledger_entry_id),
        }


def sync_fiber_output_to_ledger(
    user_id: int,
    output_id: int,
    ledger_sku_id: int,
    created_by: int | None = None,
) -> dict:
    uid = int(user_id)
    oid = int(output_id)
    sku_id = int(ledger_sku_id)
    if sku_id <= 0:
        raise ValueError("Choose an Iron Ledger product to restock.")
    actor = int(created_by if created_by is not None else uid)
    now_iso = _now_utc_iso()

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT fo.*, ml.lot_name
            FROM fiber_lot_outputs fo
            JOIN fiber_mill_lots ml ON ml.id = fo.lot_id
            WHERE fo.id = ? AND fo.user_id = ?
            """,
            (oid, uid),
        ).fetchone()
        if not row:
            raise ValueError("Finished output entry not found.")
        if row["ledger_entry_id"]:
            raise ValueError("Output is already synced to Iron Ledger.")
        qty = float(row["quantity"] or 0.0)
        sku_res, entry_res = restock_ledger_sku(
            sku_id,
            qty,
            note=f"Milled {qty:g} {row['unit']} {row['product_name']} ({row['lot_name']})",
            created_by=actor,
            created_at=now_iso,
            conn=conn,
        )
        if sku_res is None or entry_res is None:
            raise ValueError("Selected Iron Ledger product was not found.")
        conn.execute(
            "UPDATE fiber_lot_outputs SET ledger_sku_id = ?, ledger_entry_id = ? WHERE id = ? AND user_id = ?",
            (sku_res, entry_res, oid, uid),
        )
        conn.commit()
        return {
            "output_id": oid,
            "ledger_sku_id": sku_res,
            "ledger_entry_id": entry_res,
            "quantity": qty,
        }


def delete_fiber_lot_output(user_id: int, output_id: int) -> bool:
    uid = int(user_id)
    oid = int(output_id)
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM fiber_lot_outputs WHERE id = ? AND user_id = ?",
            (oid, uid),
        ).fetchone()
        if not row:
            return False
        if row["ledger_entry_id"]:
            delete_ledger_entry(int(row["ledger_entry_id"]), conn=conn)
        conn.execute(
            "DELETE FROM fiber_lot_outputs WHERE id = ? AND user_id = ?",
            (oid, uid),
        )
        conn.commit()
        return True


def compute_fiber_dashboard(
    user_id: int,
    herd_id: int,
    merchant_id: int | None = None,
) -> dict:
    uid = int(user_id)
    hid = int(herd_id)
    yr_prefix = f"{date.today().year}-"
    shearings = list_fiber_shearings(uid, herd_id=hid)
    available_shearings = [s for s in shearings if float(s["remaining_lbs"] or 0.0) > 0]

    color_map: dict[str, dict] = {}
    for s in shearings:
        c_raw = str(s.get("fleece_color") or "").strip() or "Unlabeled"
        ckey = c_raw.lower()
        bucket = color_map.setdefault(
            ckey,
            {
                "color": c_raw,
                "color_code": get_alpaca_color_code(c_raw),
                "color_hex": get_alpaca_color_hex(c_raw),
                "total_lbs": 0.0,
                "remaining_lbs": 0.0,
                "clip_count": 0,
            },
        )
        bucket["total_lbs"] = round(bucket["total_lbs"] + float(s["total_lbs"] or 0.0), 2)
        bucket["remaining_lbs"] = round(bucket["remaining_lbs"] + float(s["remaining_lbs"] or 0.0), 2)
        bucket["clip_count"] += 1
    by_color = sorted(color_map.values(), key=lambda x: (-x["remaining_lbs"], x["color"].lower()))

    ledger_skus = list_active_ledger_skus(
        merchant_id=merchant_id, prefer_keywords=FIBER_SKU_KEYWORDS
    )
    sku_map = {s["id"]: s for s in ledger_skus}
    mill_lots = list_fiber_mill_lots(uid, herd_id=hid, sku_map=sku_map)

    ytd_raw_lbs = round(
        sum(float(s["total_lbs"] or 0.0) for s in shearings if str(s.get("shear_date") or "").startswith(yr_prefix)),
        2,
    )
    total_raw_lbs = round(sum(float(s["total_lbs"] or 0.0) for s in shearings), 2)
    on_hand_raw_lbs = round(sum(float(s["remaining_lbs"] or 0.0) for s in shearings), 2)
    avg_lbs_per_shearing = round(total_raw_lbs / len(shearings), 2) if shearings else 0.0
    ytd_shearing_fees = round(
        sum(float(s["shearing_fee"] or 0.0) for s in shearings if str(s.get("shear_date") or "").startswith(yr_prefix)),
        2,
    )

    lots_at_mill = sum(1 for l in mill_lots if l["status"] in ("draft", "sent_to_mill"))
    total_finished_units = round(sum(float(l["total_output_units"] or 0.0) for l in mill_lots), 2)
    total_mill_fees = round(sum(float(l["total_mill_cost"] or 0.0) for l in mill_lots), 2)
    total_cogs = round(sum(float(l["total_direct_cogs"] or 0.0) for l in mill_lots if l["outputs"]), 2)
    avg_direct_cost_per_unit = (
        round(total_cogs / total_finished_units, 2) if total_finished_units > 0 else 0.0
    )

    return {
        "shearings": shearings,
        "available_shearings": available_shearings,
        "by_color": by_color,
        "mill_lots": mill_lots,
        "ledger_skus": ledger_skus,
        "ytd_raw_lbs": ytd_raw_lbs,
        "total_raw_lbs": total_raw_lbs,
        "on_hand_raw_lbs": on_hand_raw_lbs,
        "avg_lbs_per_shearing": avg_lbs_per_shearing,
        "ytd_shearing_fees": ytd_shearing_fees,
        "lots_at_mill": lots_at_mill,
        "total_finished_units": total_finished_units,
        "total_mill_fees": total_mill_fees,
        "avg_direct_cost_per_unit": avg_direct_cost_per_unit,
    }

"""Multi-species eggs, laying flock headcount, batch graduation, feed allocation & dashboard."""

from datetime import date, datetime, timedelta

from homestead.db.batches import set_batch_active
from homestead.db.core import (
    get_db_connection,
    get_text_setting,
    set_text_setting,
)
from shared.bridge import (
    delete_ledger_entry,
    get_sku_completed_sales_summary,
    list_active_ledger_skus,
    restock_ledger_sku,
)


EGG_SPECIES_ORDER = ("chicken", "duck", "goose")
EGG_SPECIES_DEFAULTS = {
    "chicken": {
        "key": "chicken",
        "label": "Chicken",
        "plural": "Chickens",
        "bird_label": "laying hens",
        "bird_title": "Laying Hens",
        "bird_singular": "hen",
        "pack_size": 12,
        "pack_unit": "dz",
        "pack_unit_full": "Dozen",
        "pack_unit_plural": "Dozens",
        "quick_add": [6, 12, 18, 24],
        "default_feed_per_bird_day": 0.25,
    },
    "duck": {
        "key": "duck",
        "label": "Duck",
        "plural": "Ducks",
        "bird_label": "laying ducks",
        "bird_title": "Laying Ducks",
        "bird_singular": "duck",
        "pack_size": 6,
        "pack_unit": "half-dz",
        "pack_unit_full": "Half-Dozen",
        "pack_unit_plural": "Half-Dozens (6-pk)",
        "quick_add": [1, 2, 4, 6],
        "default_feed_per_bird_day": 0.35,
    },
    "goose": {
        "key": "goose",
        "label": "Goose",
        "plural": "Geese",
        "bird_label": "laying geese",
        "bird_title": "Laying Geese",
        "bird_singular": "goose",
        "pack_size": 4,
        "pack_unit": "4-pk",
        "pack_unit_full": "4-Pack",
        "pack_unit_plural": "4-Packs",
        "quick_add": [1, 2, 4],
        "default_feed_per_bird_day": 0.50,
    },
}


def normalize_egg_species(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in EGG_SPECIES_ORDER:
        return s
    if "duck" in s:
        return "duck"
    if "goose" in s or "geese" in s:
        return "goose"
    return "chicken"


def normalize_feed_species_tags(raw_tags: str | list[str] | tuple[str, ...] | None) -> str:
    if isinstance(raw_tags, (list, tuple, set)):
        parts = [str(x).strip().lower() for x in raw_tags if str(x).strip()]
    else:
        parts = [x.strip().lower() for x in str(raw_tags or "").split(",") if x.strip()]
    if not parts or "all" in parts:
        return "all"
    valid = []
    for p in ("chicken", "waterfowl", "duck", "goose"):
        if p in parts and p not in valid:
            valid.append(p)
    return ",".join(valid) if valid else "all"


def feed_tags_target_species(tags_str: str | None) -> set[str]:
    norm = normalize_feed_species_tags(tags_str)
    if norm == "all":
        return set(EGG_SPECIES_ORDER)
    out: set[str] = set()
    for p in norm.split(","):
        if p == "waterfowl":
            out.update(("duck", "goose"))
        elif p in EGG_SPECIES_ORDER:
            out.add(p)
    return out or set(EGG_SPECIES_ORDER)


def feed_tags_label(tags_str: str | None) -> str:
    norm = normalize_feed_species_tags(tags_str)
    if norm == "all":
        return "All Flocks"
    labels = {
        "chicken": "Chickens",
        "waterfowl": "Waterfowl",
        "duck": "Ducks",
        "goose": "Geese",
    }
    return " + ".join(labels.get(p, p.title()) for p in norm.split(","))


def _parse_pos_float(raw: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(str(raw or "").strip()))
    except ValueError:
        return default


def _parse_sku_id(raw: str) -> int | None:
    s = str(raw or "").strip()
    return int(s) if s.isdigit() and int(s) > 0 else None


def get_layer_settings(user_id: int) -> dict:
    raw_enabled = get_text_setting("layer_enabled_species", "chicken", user_id=user_id).strip()
    enabled = [
        sp for sp in EGG_SPECIES_ORDER
        if sp in {p.strip().lower() for p in raw_enabled.split(",") if p.strip()}
    ]
    if not enabled:
        enabled = ["chicken"]

    raw_goose_pack = get_text_setting("layer_goose_pack_size", "4", user_id=user_id).strip()
    goose_pack_size = 1 if raw_goose_pack == "1" else 4

    rate_chicken = _parse_pos_float(
        get_text_setting("layer_feed_lbs_per_hen_day", "0.25", user_id=user_id), 0.25, minimum=0.01
    )
    rate_duck = _parse_pos_float(
        get_text_setting("layer_feed_lbs_per_duck_day", "0.35", user_id=user_id), 0.35, minimum=0.01
    )
    rate_goose = _parse_pos_float(
        get_text_setting("layer_feed_lbs_per_goose_day", "0.50", user_id=user_id), 0.50, minimum=0.01
    )
    scratch_rate = _parse_pos_float(
        get_text_setting("layer_scratch_lbs_per_day", "0.0", user_id=user_id), 0.0
    )
    price = _parse_pos_float(
        get_text_setting("layer_bag_price", "22.00", user_id=user_id), 22.00
    )
    scratch_price = _parse_pos_float(
        get_text_setting("layer_scratch_bag_price", "16.00", user_id=user_id), 16.00
    )

    sku_chicken = _parse_sku_id(get_text_setting("layer_ledger_sku_id", "", user_id=user_id))
    sku_duck = _parse_sku_id(get_text_setting("layer_ledger_sku_id_duck", "", user_id=user_id))
    sku_goose = _parse_sku_id(get_text_setting("layer_ledger_sku_id_goose", "", user_id=user_id))

    rates_map = {"chicken": round(rate_chicken, 3), "duck": round(rate_duck, 3), "goose": round(rate_goose, 3)}
    skus_map = {"chicken": sku_chicken, "duck": sku_duck, "goose": sku_goose}

    species_meta: dict[str, dict] = {}
    for sp in EGG_SPECIES_ORDER:
        meta = dict(EGG_SPECIES_DEFAULTS[sp])
        if sp == "goose" and goose_pack_size == 1:
            meta["pack_size"] = 1
            meta["pack_unit"] = "egg"
            meta["pack_unit_full"] = "Single Egg"
            meta["pack_unit_plural"] = "Single Eggs"
        meta["feed_lbs_per_bird_day"] = rates_map[sp]
        meta["ledger_sku_id"] = skus_map[sp]
        meta["enabled"] = sp in enabled
        species_meta[sp] = meta

    return {
        "enabled_species": enabled,
        "goose_pack_size": goose_pack_size,
        "species_meta": species_meta,
        "feed_lbs_per_hen_day": round(rate_chicken, 3),
        "feed_lbs_by_species": rates_map,
        "scratch_lbs_per_day": round(scratch_rate, 2),
        "bag_price": round(price, 2),
        "scratch_bag_price": round(scratch_price, 2),
        "ledger_sku_id": sku_chicken,
        "ledger_sku_by_species": skus_map,
    }


def save_egg_species_settings(
    user_id: int,
    enabled_species: list[str] | tuple[str, ...],
    goose_pack_size: int = 4,
) -> dict:
    chosen = [sp for sp in EGG_SPECIES_ORDER if sp in {str(x).strip().lower() for x in (enabled_species or [])}]
    if not chosen:
        chosen = ["chicken"]
    gp = 1 if int(goose_pack_size or 4) == 1 else 4
    set_text_setting("layer_enabled_species", ",".join(chosen), user_id=user_id)
    set_text_setting("layer_goose_pack_size", str(gp), user_id=user_id)
    return get_layer_settings(user_id)


def save_layer_settings(
    user_id: int,
    feed_lbs_per_hen_day: float = 0.25,
    bag_price: float = 22.00,
    ledger_sku_id: int | None = None,
    scratch_lbs_per_day: float = 0.0,
    scratch_bag_price: float = 16.00,
    species: str = "chicken",
    goose_pack_size: int | None = None,
) -> dict:
    sp = normalize_egg_species(species)
    rate = round(max(0.01, float(feed_lbs_per_hen_day or EGG_SPECIES_DEFAULTS[sp]["default_feed_per_bird_day"])), 3)
    s_rate = round(max(0.0, float(scratch_lbs_per_day or 0.0)), 2)
    price = round(max(0.0, float(bag_price or 0.0)), 2)
    s_price = round(max(0.0, float(scratch_bag_price or 0.0)), 2)
    sku_str = str(int(ledger_sku_id)) if ledger_sku_id and int(ledger_sku_id) > 0 else ""

    rate_key = "layer_feed_lbs_per_hen_day" if sp == "chicken" else f"layer_feed_lbs_per_{sp}_day"
    sku_key = "layer_ledger_sku_id" if sp == "chicken" else f"layer_ledger_sku_id_{sp}"

    set_text_setting(rate_key, str(rate), user_id=user_id)
    set_text_setting("layer_scratch_lbs_per_day", str(s_rate), user_id=user_id)
    set_text_setting("layer_bag_price", f"{price:.2f}", user_id=user_id)
    set_text_setting("layer_scratch_bag_price", f"{s_price:.2f}", user_id=user_id)
    set_text_setting(sku_key, sku_str, user_id=user_id)
    if sp == "goose" and goose_pack_size is not None:
        gp = 1 if int(goose_pack_size) == 1 else 4
        set_text_setting("layer_goose_pack_size", str(gp), user_id=user_id)
    return get_layer_settings(user_id)


def list_available_ledger_skus() -> list[dict]:
    """List active Iron Ledger SKUs from the shared SQLite DB."""
    return list_active_ledger_skus()


def add_layer_flock_event(
    user_id: int,
    event_date: str,
    delta: int,
    reason: str = "purchase",
    source_batch_id: int | None = None,
    notes: str = "",
    species: str = "chicken",
) -> int:
    clean_date = (event_date or date.today().isoformat()).strip()[:10]
    sp = normalize_egg_species(species)
    d_val = int(delta)
    if d_val == 0:
        raise ValueError("Headcount change cannot be zero.")
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO layer_flock_events (
                user_id, species, event_date, delta, reason, source_batch_id, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                sp,
                clean_date,
                d_val,
                str(reason or "purchase").strip(),
                int(source_batch_id) if source_batch_id else None,
                str(notes or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_layer_flock_event(user_id: int, event_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM layer_flock_events WHERE id = ? AND user_id = ?",
            (int(event_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_layer_flock_events(user_id: int, species: str | None = None) -> list[dict]:
    sp_clause = " AND COALESCE(e.species, 'chicken') = ?" if species else ""
    params: list = [int(user_id)]
    if species:
        params.append(normalize_egg_species(species))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT e.*, COALESCE(e.species, 'chicken') AS species, b.name AS batch_name
            FROM layer_flock_events e
            LEFT JOIN batches b ON b.id = e.source_batch_id
            WHERE e.user_id = ?{sp_clause}
            ORDER BY e.event_date DESC, e.id DESC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def is_batch_graduated_to_layers(user_id: int, batch_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM layer_flock_events WHERE user_id = ? AND source_batch_id = ? LIMIT 1",
            (int(user_id), int(batch_id)),
        ).fetchone()
        return bool(row)


def graduate_batch_to_layers(
    user_id: int,
    batch_id: int,
    head_count: int,
    event_date: str | None = None,
    archive_batch: bool = True,
) -> int:
    hc = max(1, int(head_count or 1))
    with get_db_connection() as conn:
        b = conn.execute(
            "SELECT id, name, species FROM batches WHERE id = ? AND user_id = ?",
            (int(batch_id), int(user_id)),
        ).fetchone()
        if not b:
            raise ValueError("Batch not found.")
        already = conn.execute(
            "SELECT id FROM layer_flock_events WHERE user_id = ? AND source_batch_id = ?",
            (int(user_id), int(batch_id)),
        ).fetchone()
        if already:
            raise ValueError("This batch has already been graduated to the laying flock.")
    sp = normalize_egg_species(b["species"])
    cfg = get_layer_settings(user_id)
    if sp not in cfg["enabled_species"]:
        save_egg_species_settings(user_id, [*cfg["enabled_species"], sp], cfg["goose_pack_size"])
    ev_id = add_layer_flock_event(
        user_id,
        event_date or date.today().isoformat(),
        delta=hc,
        reason="graduated_batch",
        source_batch_id=batch_id,
        notes=f"Graduated from {b['name']}",
        species=sp,
    )
    if archive_batch:
        set_batch_active(batch_id, False)
    return ev_id


def add_egg_log(
    user_id: int,
    log_date: str,
    total_collected: int,
    cracked_unusable: int = 0,
    home_used: int = 0,
    notes: str = "",
    created_by: int | None = None,
    species: str = "chicken",
) -> int:
    clean_date = (log_date or date.today().isoformat()).strip()[:10]
    sp = normalize_egg_species(species)
    tot = max(0, int(total_collected or 0))
    cracked = max(0, min(tot, int(cracked_unusable or 0)))
    home = max(0, min(tot - cracked, int(home_used or 0)))
    if tot == 0 and cracked == 0 and home == 0:
        raise ValueError("Total eggs collected must be greater than 0.")
    actor = int(created_by if created_by is not None else user_id)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO egg_logs (
                user_id, species, log_date, total_collected, cracked_unusable, home_used, notes, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), sp, clean_date, tot, cracked, home, str(notes or "").strip(), actor, now_iso),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_egg_log(user_id: int, log_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM egg_logs WHERE id = ? AND user_id = ?",
            (int(log_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_egg_logs(user_id: int, limit: int = 90, species: str | None = None) -> list[dict]:
    sp_clause = " AND COALESCE(el.species, 'chicken') = ?" if species else ""
    params: list = [int(user_id)]
    if species:
        params.append(normalize_egg_species(species))
    params.append(int(limit))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT el.*,
                   COALESCE(el.species, 'chicken') AS species,
                   COALESCE(NULLIF(u.display_name, ''), u.username, '') AS logged_by
            FROM egg_logs el
            LEFT JOIN users u ON u.id = COALESCE(el.created_by, el.user_id)
            WHERE el.user_id = ?{sp_clause}
            ORDER BY el.log_date DESC, el.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["good_packable"] = max(
                0,
                int(d["total_collected"] or 0)
                - int(d["cracked_unusable"] or 0)
                - int(d["home_used"] or 0),
            )
            out.append(d)
        return out


def pack_egg_cartons(
    user_id: int,
    pack_date: str,
    dozens_packed: int,
    notes: str = "",
    sync_to_ledger: bool = True,
    created_by: int | None = None,
    species: str = "chicken",
    pack_size_override: int | None = None,
) -> dict:
    sp = normalize_egg_species(species)
    cfg = get_layer_settings(user_id)
    meta = cfg["species_meta"][sp]
    pack_size = max(1, int(pack_size_override or meta["pack_size"]))
    packs_count = max(1, int(dozens_packed or 1))
    eggs_used = packs_count * pack_size
    clean_date = (pack_date or date.today().isoformat()).strip()[:10]
    actor = int(created_by if created_by is not None else user_id)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    sku_id = meta["ledger_sku_id"] if sync_to_ledger else None
    ledger_entry_id = None

    with get_db_connection() as conn:
        if sku_id:
            sku_id, ledger_entry_id = restock_ledger_sku(
                int(sku_id),
                packs_count,
                note=f"Packed {packs_count} {meta['pack_unit']} {meta['label']} eggs from Homestead Egg Log ({clean_date})",
                created_by=actor,
                created_at=now_iso,
                conn=conn,
            )

        p_cur = conn.execute(
            """
            INSERT INTO egg_pack_logs (
                user_id, species, pack_date, dozens_packed, pack_size, eggs_used,
                ledger_sku_id, ledger_entry_id, notes, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                sp,
                clean_date,
                packs_count,
                pack_size,
                eggs_used,
                sku_id,
                ledger_entry_id,
                str(notes or "").strip(),
                actor,
                now_iso,
            ),
        )
        conn.commit()
        return {
            "pack_id": int(p_cur.lastrowid),
            "species": sp,
            "pack_size": pack_size,
            "pack_unit": meta["pack_unit"],
            "dozens_packed": packs_count,
            "eggs_used": eggs_used,
            "synced_to_ledger": bool(ledger_entry_id),
            "ledger_entry_id": ledger_entry_id,
        }


def delete_egg_pack_log(user_id: int, pack_id: int) -> str | None:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM egg_pack_logs WHERE id = ? AND user_id = ?",
            (int(pack_id), int(user_id)),
        ).fetchone()
        if not row:
            return None
        sp = normalize_egg_species(row["species"] if "species" in row.keys() else "chicken")
        if row["ledger_entry_id"]:
            delete_ledger_entry(int(row["ledger_entry_id"]), conn=conn)
        conn.execute("DELETE FROM egg_pack_logs WHERE id = ? AND user_id = ?", (int(pack_id), int(user_id)))
        conn.commit()
        return sp


def list_egg_pack_logs(user_id: int, limit: int = 30, species: str | None = None) -> list[dict]:
    sp_clause = " AND COALESCE(ep.species, 'chicken') = ?" if species else ""
    params: list = [int(user_id)]
    if species:
        params.append(normalize_egg_species(species))
    params.append(int(limit))
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT ep.*,
                   COALESCE(ep.species, 'chicken') AS species,
                   COALESCE(ep.pack_size, 12) AS pack_size,
                   COALESCE(NULLIF(u.display_name, ''), u.username, '') AS logged_by
            FROM egg_pack_logs ep
            LEFT JOIN users u ON u.id = COALESCE(ep.created_by, ep.user_id)
            WHERE ep.user_id = ?{sp_clause}
            ORDER BY ep.pack_date DESC, ep.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            ps = int(d.get("pack_size") or 12)
            d["pack_unit"] = (
                "dz" if ps == 12 else ("half-dz" if ps == 6 else ("4-pk" if ps == 4 else ("egg" if ps == 1 else f"{ps}-pk")))
            )
            out.append(d)
        return out


def add_layer_feed_log(
    user_id: int,
    feed_date: str,
    bags: float,
    feed_lbs: float,
    cost_total: float,
    notes: str = "",
    receipt_url: str = "",
    drive_file_id: str = "",
    feed_type: str = "layer",
    species_tags: str | list[str] | None = "all",
    vendor: str = "",
    receipt_group_id: str = "",
) -> int:
    clean_date = (feed_date or date.today().isoformat()).strip()[:10]
    ftype = feed_type if feed_type in ("layer", "scratch", "supplement") else "layer"
    norm_tags = normalize_feed_species_tags(species_tags)
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO layer_feed_logs (
                user_id, feed_date, feed_type, species_tags, bags, feed_lbs, cost_total,
                notes, receipt_url, drive_file_id, vendor, receipt_group_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                clean_date,
                ftype,
                norm_tags,
                max(0.0, float(bags or 0.0)),
                max(0.0, float(feed_lbs or 0.0)),
                max(0.0, float(cost_total or 0.0)),
                str(notes or "").strip(),
                str(receipt_url or "").strip(),
                str(drive_file_id or "").strip(),
                str(vendor or "").strip(),
                str(receipt_group_id or "").strip(),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def attach_layer_feed_receipt(
    feed_id: int,
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
            UPDATE layer_feed_logs
            SET receipt_url = ?,
                drive_file_id = COALESCE(NULLIF(?, ''), drive_file_id)
            WHERE id = ? AND user_id = ?
            """,
            (clean_url, str(drive_file_id or "").strip(), int(feed_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_layer_feed_log(user_id: int, feed_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute(
            "DELETE FROM layer_feed_logs WHERE id = ? AND user_id = ?",
            (int(feed_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def list_layer_feed_logs(user_id: int, limit: int = 50) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT *, COALESCE(species_tags, 'all') AS species_tags
            FROM layer_feed_logs
            WHERE user_id = ?
            ORDER BY feed_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["species_tags"] = normalize_feed_species_tags(d.get("species_tags"))
            d["species_tags_label"] = feed_tags_label(d["species_tags"])
            out.append(d)
        return out


def compute_egg_dashboard(
    user_id: int,
    bag_weight: int = 50,
    species: str | None = None,
) -> dict:
    """Compute multi-species laying flock headcount, daily egg metrics, loose eggs ready to pack,
    shared feed allocation & economics, and live Iron Ledger carton stock & sales revenue.
    """
    cfg = get_layer_settings(user_id)
    enabled_species = cfg["enabled_species"]
    if species:
        active_species = normalize_egg_species(species)
    else:
        active_species = "chicken" if "chicken" in enabled_species else enabled_species[0]
    active_meta = cfg["species_meta"][active_species]
    pack_size = int(active_meta["pack_size"])

    all_flock_events = list_layer_flock_events(user_id)
    all_egg_logs_recent = list_egg_logs(user_id, limit=500)
    flock_events = [ev for ev in all_flock_events if ev.get("species", "chicken") == active_species]
    egg_logs = [lg for lg in all_egg_logs_recent if lg.get("species", "chicken") == active_species]
    pack_logs = list_egg_pack_logs(user_id, limit=100, species=active_species)
    feed_logs = list_layer_feed_logs(user_id, limit=100)
    ledger_skus = list_available_ledger_skus()

    def _birds_on_date(sp: str, d_iso: str) -> int:
        tot = sum(
            int(ev["delta"] or 0)
            for ev in all_flock_events
            if (ev.get("species") or "chicken") == sp and str(ev["event_date"]) <= d_iso
        )
        return max(0, tot)

    today = date.today()
    today_iso = today.isoformat()
    active_birds_by_species = {sp: _birds_on_date(sp, today_iso) for sp in EGG_SPECIES_ORDER}
    active_hens = active_birds_by_species[active_species]
    total_active_layers = sum(active_birds_by_species.values())

    # Cross-species summaries (for sticky species switcher + Radar Today's Eggs card)
    with get_db_connection() as conn:
        egg_sums_by_sp = {
            r["sp"]: dict(r)
            for r in conn.execute(
                """
                SELECT COALESCE(species, 'chicken') AS sp,
                       COALESCE(SUM(total_collected), 0) AS tot,
                       COALESCE(SUM(cracked_unusable), 0) AS cr,
                       COALESCE(SUM(home_used), 0) AS hm,
                       COALESCE(SUM(CASE WHEN substr(log_date, 1, 10) = ? THEN total_collected ELSE 0 END), 0) AS today_tot
                FROM egg_logs WHERE user_id = ?
                GROUP BY COALESCE(species, 'chicken')
                """,
                (today_iso, int(user_id)),
            ).fetchall()
        }
        pack_sums_by_sp = {
            r["sp"]: dict(r)
            for r in conn.execute(
                """
                SELECT COALESCE(species, 'chicken') AS sp,
                       COALESCE(SUM(dozens_packed), 0) AS packs,
                       COALESCE(SUM(eggs_used), 0) AS eggs
                FROM egg_pack_logs WHERE user_id = ?
                GROUP BY COALESCE(species, 'chicken')
                """,
                (int(user_id),),
            ).fetchall()
        }

    species_summaries: dict[str, dict] = {}
    today_species_parts: list[str] = []
    total_ready_packs = 0
    total_today_eggs_all = 0
    for sp in EGG_SPECIES_ORDER:
        s_meta = cfg["species_meta"][sp]
        es = egg_sums_by_sp.get(sp, {"tot": 0, "cr": 0, "hm": 0, "today_tot": 0})
        ps = pack_sums_by_sp.get(sp, {"packs": 0, "eggs": 0})
        sp_good = max(0, int(es["tot"] or 0) - int(es["cr"] or 0) - int(es["hm"] or 0))
        sp_loose = max(0, sp_good - int(ps["eggs"] or 0))
        sp_ready = sp_loose // int(s_meta["pack_size"])
        sp_today = int(es["today_tot"] or 0)
        total_today_eggs_all += sp_today
        if sp in enabled_species:
            total_ready_packs += sp_ready
        if sp_today > 0:
            today_species_parts.append(f"{sp_today} {s_meta['label'].lower()}")
        species_summaries[sp] = {
            **s_meta,
            "active_birds": active_birds_by_species[sp],
            "today_eggs": sp_today,
            "loose_eggs": sp_loose,
            "ready_packs": sp_ready,
            "packed_total": int(ps["packs"] or 0),
            "lifetime_collected": int(es["tot"] or 0),
        }

    today_multi_species_summary = (
        " · ".join(today_species_parts) if len(today_species_parts) > 1 else ""
    )

    by_day: dict[str, dict] = {}
    for lg in egg_logs:
        ds = str(lg["log_date"])[:10]
        c_val = int(lg["total_collected"] or 0)
        cr_val = int(lg["cracked_unusable"] or 0)
        hm_val = int(lg["home_used"] or 0)
        gd_val = int(lg["good_packable"] or 0)
        slot = by_day.setdefault(ds, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
        slot["collected"] += c_val
        slot["cracked"] += cr_val
        slot["home"] += hm_val
        slot["good"] += gd_val

    cur_es = egg_sums_by_sp.get(active_species, {"tot": 0, "cr": 0, "hm": 0, "today_tot": 0})
    cur_ps = pack_sums_by_sp.get(active_species, {"packs": 0, "eggs": 0})
    lifetime_collected = int(cur_es["tot"] or 0)
    lifetime_cracked = int(cur_es["cr"] or 0)
    lifetime_home = int(cur_es["hm"] or 0)
    lifetime_good = max(0, lifetime_collected - lifetime_cracked - lifetime_home)
    lifetime_packed_dz = int(cur_ps["packs"] or 0)
    lifetime_packed_eggs = int(cur_ps["eggs"] or 0)

    loose_eggs = max(0, lifetime_good - lifetime_packed_eggs)
    ready_dozens = loose_eggs // pack_size
    remainder_loose = loose_eggs % pack_size

    today_slot = by_day.get(today_iso, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
    today_eggs = today_slot["collected"]
    today_lay_rate = round((today_eggs / active_hens) * 100, 1) if active_hens > 0 else 0.0
    today_last_logged_by = next(
        (lg["logged_by"] for lg in all_egg_logs_recent if str(lg["log_date"])[:10] == today_iso and lg.get("logged_by")),
        "",
    )

    # Last 7 days and 30 days series (scoped to active_species)
    chart_days = []
    eggs_7d = 0
    eggs_30d = 0
    rate_sum_7d = 0.0
    rate_days_7d = 0
    for i in range(29, -1, -1):
        d_obj = today - timedelta(days=i)
        d_iso = d_obj.isoformat()
        slot = by_day.get(d_iso, {"collected": 0, "cracked": 0, "home": 0, "good": 0})
        hens_d = _birds_on_date(active_species, d_iso) or active_hens
        rate_d = round((slot["collected"] / hens_d) * 100, 1) if hens_d > 0 else 0.0
        eggs_30d += slot["collected"]
        if i < 7:
            eggs_7d += slot["collected"]
            if hens_d > 0:
                rate_sum_7d += rate_d
                rate_days_7d += 1
        chart_days.append({
            "date": d_iso,
            "label": d_obj.strftime("%b %-d"),
            "short_label": d_obj.strftime("%-d"),
            "collected": slot["collected"],
            "good": slot["good"],
            "hens": hens_d,
            "lay_rate_pct": rate_d,
        })
    chart_peak = max((c["collected"] for c in chart_days), default=0) or 1
    avg_7d_eggs = round(eggs_7d / 7.0, 1)
    avg_7d_lay_rate = round(rate_sum_7d / rate_days_7d, 1) if rate_days_7d > 0 else 0.0

    # Shared Barn Feed Stockpile (all flocks) + Species-Allocated Feed Economics
    rates_map = cfg["feed_lbs_by_species"]
    total_daily_layer_burn_lbs = round(
        sum(active_birds_by_species[sp] * rates_map[sp] for sp in EGG_SPECIES_ORDER), 2
    )
    total_daily_scratch_burn_lbs = round(cfg["scratch_lbs_per_day"], 2) if total_active_layers > 0 else 0.0
    total_daily_feed_burn_lbs = round(total_daily_layer_burn_lbs + total_daily_scratch_burn_lbs, 2)

    daily_layer_burn_lbs = round(active_hens * rates_map[active_species], 2)
    scratch_share_today = (
        (daily_layer_burn_lbs / total_daily_layer_burn_lbs)
        if total_daily_layer_burn_lbs > 0
        else (1.0 if active_hens > 0 else 0.0)
    )
    daily_scratch_burn_lbs = round(cfg["scratch_lbs_per_day"] * scratch_share_today, 2) if active_hens > 0 else 0.0
    daily_feed_burn_lbs = round(daily_layer_burn_lbs + daily_scratch_burn_lbs, 2)
    weekly_feed_lbs = round(total_daily_feed_burn_lbs * 7, 1)
    monthly_feed_lbs = round(total_daily_feed_burn_lbs * 30, 1)
    monthly_bags = round(monthly_feed_lbs / max(1, bag_weight), 1)

    layer_bought_lbs = round(
        sum(float(f["feed_lbs"] or 0.0) for f in feed_logs if (f.get("feed_type") or "layer") == "layer"), 1
    )
    layer_bought_cost = round(
        sum(float(f["cost_total"] or 0.0) for f in feed_logs if (f.get("feed_type") or "layer") == "layer"), 2
    )
    scratch_bought_lbs = round(
        sum(float(f["feed_lbs"] or 0.0) for f in feed_logs if f.get("feed_type") == "scratch"), 1
    )
    scratch_bought_cost = round(
        sum(float(f["cost_total"] or 0.0) for f in feed_logs if f.get("feed_type") == "scratch"), 2
    )

    logged_feed_lbs = round(sum(float(f["feed_lbs"] or 0.0) for f in feed_logs), 1)
    logged_feed_cost = round(sum(float(f["cost_total"] or 0.0) for f in feed_logs), 2)

    # Species-specific share of each feed log entry based on species_tags ("all", "chicken", "waterfowl", etc.)
    def _species_share_for_targets(targets: set[str], d_iso: str = today_iso) -> float:
        if active_species not in targets:
            return 0.0
        demands = {
            sp: (_birds_on_date(sp, d_iso) or active_birds_by_species[sp]) * rates_map[sp]
            for sp in targets
        }
        tot_dem = sum(demands.values())
        if tot_dem > 0:
            return demands[active_species] / tot_dem
        active_in_targets = [sp for sp in targets if sp in enabled_species]
        if active_species in active_in_targets and active_in_targets:
            return 1.0 / len(active_in_targets)
        return 1.0 if len(targets) == 1 else 0.0

    sp_layer_lbs = 0.0
    sp_layer_cost = 0.0
    sp_scratch_lbs = 0.0
    sp_scratch_cost = 0.0
    sp_supp_cost = 0.0
    sp_logged_feed_cost = 0.0
    has_applicable_layer = False
    has_applicable_scratch = False

    for f in feed_logs:
        targets = feed_tags_target_species(f.get("species_tags"))
        sh = _species_share_for_targets(targets, str(f.get("feed_date") or today_iso)[:10])
        if sh <= 0:
            continue
        ft = f.get("feed_type") or "layer"
        lbs_f = float(f["feed_lbs"] or 0.0) * sh
        cost_f = float(f["cost_total"] or 0.0) * sh
        sp_logged_feed_cost += cost_f
        if ft == "layer":
            has_applicable_layer = True
            sp_layer_lbs += lbs_f
            sp_layer_cost += cost_f
        elif ft == "scratch":
            has_applicable_scratch = True
            sp_scratch_lbs += lbs_f
            sp_scratch_cost += cost_f
        else:
            sp_supp_cost += cost_f

    sp_logged_feed_cost = round(sp_logged_feed_cost, 2)

    layer_price_per_lb = (
        (sp_layer_cost / sp_layer_lbs)
        if sp_layer_lbs > 0 and sp_layer_cost > 0
        else (
            (layer_bought_cost / layer_bought_lbs)
            if not feed_logs and layer_bought_lbs > 0 and layer_bought_cost > 0
            else (cfg["bag_price"] / max(1, bag_weight))
        )
    )
    scratch_price_per_lb = (
        (sp_scratch_cost / sp_scratch_lbs)
        if sp_scratch_lbs > 0 and sp_scratch_cost > 0
        else (cfg["scratch_bag_price"] / max(1, bag_weight))
    )

    # Barn stockpile remaining across all active laying species
    layer_dates = [str(f["feed_date"])[:10] for f in feed_logs if (f.get("feed_type") or "layer") == "layer"]
    layer_burned_since_buy = 0.0
    if layer_dates:
        try:
            d0 = date.fromisoformat(min(layer_dates))
        except ValueError:
            d0 = today
        span_days = max(1, min(365, (today - d0).days + 1))
        for idx in range(span_days):
            di = (d0 + timedelta(days=idx)).isoformat()
            day_burn = sum(
                (_birds_on_date(sp, di) or active_birds_by_species[sp]) * rates_map[sp]
                for sp in EGG_SPECIES_ORDER
            )
            layer_burned_since_buy += day_burn
    layer_remaining_lbs = round(max(0.0, layer_bought_lbs - layer_burned_since_buy), 1)
    layer_days_left = (
        round(layer_remaining_lbs / total_daily_layer_burn_lbs, 1)
        if total_daily_layer_burn_lbs > 0 and layer_bought_lbs > 0
        else None
    )

    scratch_dates = [str(f["feed_date"])[:10] for f in feed_logs if f.get("feed_type") == "scratch"]
    scratch_burned_since_buy = 0.0
    if scratch_dates:
        try:
            sd0 = date.fromisoformat(min(scratch_dates))
        except ValueError:
            sd0 = today
        s_span = max(1, min(365, (today - sd0).days + 1))
        scratch_burned_since_buy = s_span * cfg["scratch_lbs_per_day"]
    scratch_remaining_lbs = round(max(0.0, scratch_bought_lbs - scratch_burned_since_buy), 1)
    scratch_days_left = (
        round(scratch_remaining_lbs / total_daily_scratch_burn_lbs, 1)
        if total_daily_scratch_burn_lbs > 0 and scratch_bought_lbs > 0
        else None
    )

    # Amortized feed cost eaten by active_species on its active egg-collection days
    consumed_layer_lbs = 0.0
    consumed_scratch_lbs = 0.0
    has_other_tagged_layer_only = bool(
        [f for f in feed_logs if (f.get("feed_type") or "layer") == "layer"]
    ) and not has_applicable_layer
    has_other_tagged_scratch_only = bool(
        [f for f in feed_logs if f.get("feed_type") == "scratch"]
    ) and not has_applicable_scratch

    for d_iso in by_day:
        h_d = _birds_on_date(active_species, d_iso) or active_hens
        if not has_other_tagged_layer_only:
            consumed_layer_lbs += h_d * rates_map[active_species]
        if h_d > 0 and not has_other_tagged_scratch_only:
            sh_d = _species_share_for_targets(set(EGG_SPECIES_ORDER), d_iso)
            consumed_scratch_lbs += cfg["scratch_lbs_per_day"] * sh_d

    consumed_feed_cost = round(
        consumed_layer_lbs * layer_price_per_lb
        + consumed_scratch_lbs * scratch_price_per_lb
        + sp_supp_cost,
        2,
    )
    if consumed_feed_cost > 0:
        reconciled_feed_cost = consumed_feed_cost
        feed_cost_source = "amortized from logged bags" if sp_logged_feed_cost > 0 else "estimated daily burn"
    elif sp_logged_feed_cost > 0:
        reconciled_feed_cost = sp_logged_feed_cost
        feed_cost_source = "logged"
    else:
        reconciled_feed_cost = 0.0
        feed_cost_source = "estimated"

    lifetime_packs_collected = round(lifetime_collected / float(pack_size), 2)
    cost_per_dozen = (
        round(reconciled_feed_cost / lifetime_packs_collected, 2)
        if lifetime_packs_collected > 0
        else 0.0
    )

    # Linked Iron Ledger SKU stats & realized sales revenue for active_species
    linked_sku = next((s for s in ledger_skus if s["id"] == active_meta["ledger_sku_id"]), None)
    ledger_dozens_sold = 0.0
    ledger_revenue = 0.0
    if linked_sku:
        ledger_dozens_sold, ledger_revenue = get_sku_completed_sales_summary(int(linked_sku["id"]))

    net_egg_profit = round(ledger_revenue - reconciled_feed_cost, 2)

    return {
        "settings": cfg,
        "active_species": active_species,
        "active_meta": active_meta,
        "enabled_species": enabled_species,
        "species_summaries": species_summaries,
        "today_multi_species_summary": today_multi_species_summary,
        "total_today_eggs_all": total_today_eggs_all,
        "total_ready_packs": total_ready_packs,
        "total_active_layers": total_active_layers,
        "active_birds_by_species": active_birds_by_species,
        "active_hens": active_hens,
        "pack_size": pack_size,
        "pack_unit": active_meta["pack_unit"],
        "pack_unit_full": active_meta["pack_unit_full"],
        "pack_unit_plural": active_meta["pack_unit_plural"],
        "quick_add": active_meta["quick_add"],
        "today_iso": today_iso,
        "today_eggs": today_eggs,
        "today_lay_rate": today_lay_rate,
        "today_last_logged_by": today_last_logged_by,
        "eggs_7d": eggs_7d,
        "eggs_30d": eggs_30d,
        "avg_7d_eggs": avg_7d_eggs,
        "avg_7d_lay_rate": avg_7d_lay_rate,
        "lifetime_collected": lifetime_collected,
        "lifetime_dozens_collected": lifetime_packs_collected,
        "lifetime_cracked": lifetime_cracked,
        "lifetime_home": lifetime_home,
        "lifetime_packed_dz": lifetime_packed_dz,
        "loose_eggs": loose_eggs,
        "ready_dozens": ready_dozens,
        "remainder_loose": remainder_loose,
        "daily_layer_burn_lbs": daily_layer_burn_lbs,
        "daily_scratch_burn_lbs": daily_scratch_burn_lbs,
        "daily_feed_burn_lbs": daily_feed_burn_lbs,
        "total_daily_feed_burn_lbs": total_daily_feed_burn_lbs,
        "weekly_feed_lbs": weekly_feed_lbs,
        "monthly_feed_lbs": monthly_feed_lbs,
        "monthly_bags": monthly_bags,
        "layer_bought_lbs": layer_bought_lbs,
        "layer_bought_cost": layer_bought_cost,
        "layer_remaining_lbs": layer_remaining_lbs,
        "layer_days_left": layer_days_left,
        "scratch_bought_lbs": scratch_bought_lbs,
        "scratch_bought_cost": scratch_bought_cost,
        "scratch_remaining_lbs": scratch_remaining_lbs,
        "scratch_days_left": scratch_days_left,
        "logged_feed_lbs": logged_feed_lbs,
        "logged_feed_cost": logged_feed_cost,
        "species_logged_feed_cost": sp_logged_feed_cost,
        "reconciled_feed_cost": reconciled_feed_cost,
        "feed_cost_source": feed_cost_source,
        "cost_per_dozen": cost_per_dozen,
        "cost_per_pack": cost_per_dozen,
        "linked_sku": linked_sku,
        "ledger_skus": ledger_skus,
        "ledger_dozens_sold": ledger_dozens_sold,
        "ledger_revenue": ledger_revenue,
        "net_egg_profit": net_egg_profit,
        "chart_days": chart_days,
        "chart_peak": chart_peak,
        "flock_events": flock_events,
        "egg_logs": egg_logs[:60],
        "pack_logs": pack_logs,
        "feed_logs": feed_logs,
    }

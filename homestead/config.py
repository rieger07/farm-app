"""Species presets, constants, and workspace definitions for Homestead Planner."""

import re

LIFECYCLE_TYPES: dict[str, str] = {
    "poultry_incubation": "🐣 Poultry / Eggs (Arrival → Incubation → Hatch)",
    "mammal_breeding": "🐖 Mammal / Breeding (Breeding → Gestation → Birth)",
    "direct_arrival": "📥 Direct Arrival (0 days to birth/hatch)",
}

# Livestock Data Dictionary (Default Species Presets)
# lifecycle_type: 'poultry_incubation' | 'mammal_breeding' | 'direct_arrival'
# days_to_birth: Gestation or Incubation days
# starter_weeks / starter_feed_lbs: Starter phase duration (weeks) & feed per animal (lbs)
# grower_weeks / grower_feed_lbs: Grower/Finisher phase duration (weeks) & feed per animal (lbs)
# starter_bag_price / grower_bag_price: Default price ($) per feed bag
# unit_cost: Default acquisition cost ($) per egg/chick/poult/piglet
# processing_cost: Default butcher/processing cost ($) per harvested animal
# expected_yield_lbs: Default dressed/packaged meat yield (lbs) per harvested animal
# sale_price_per_lb: Default target sale price ($/lb) of dressed meat
LIVESTOCK_DATA: dict[str, dict] = {
    "chicken eggs": {
        "lifecycle_type": "poultry_incubation",
        "days_to_birth": 21,
        "starter_weeks": 3,
        "starter_feed_lbs": 3.0,
        "grower_weeks": 5,
        "grower_feed_lbs": 12.0,
        "starter_bag_price": 24.0,
        "grower_bag_price": 22.0,
        "unit_cost": 2.50,
        "processing_cost": 5.00,
        "expected_yield_lbs": 4.5,
        "sale_price_per_lb": 6.50,
        "weeks_to_proc": 8,
        "feed_total": 15.0,
    },
    "chicken chicks": {
        "lifecycle_type": "direct_arrival",
        "days_to_birth": 0,
        "starter_weeks": 3,
        "starter_feed_lbs": 3.0,
        "grower_weeks": 5,
        "grower_feed_lbs": 12.0,
        "starter_bag_price": 24.0,
        "grower_bag_price": 22.0,
        "unit_cost": 4.00,
        "processing_cost": 5.00,
        "expected_yield_lbs": 4.5,
        "sale_price_per_lb": 6.50,
        "weeks_to_proc": 8,
        "feed_total": 15.0,
    },
    "turkey chicks": {
        "lifecycle_type": "direct_arrival",
        "days_to_birth": 0,
        "starter_weeks": 6,
        "starter_feed_lbs": 12.0,
        "grower_weeks": 12,
        "grower_feed_lbs": 63.0,
        "starter_bag_price": 28.0,
        "grower_bag_price": 24.0,
        "unit_cost": 12.00,
        "processing_cost": 15.00,
        "expected_yield_lbs": 16.0,
        "sale_price_per_lb": 7.50,
        "weeks_to_proc": 18,
        "feed_total": 75.0,
    },
    "kunekune pigs": {
        "lifecycle_type": "mammal_breeding",
        "days_to_birth": 116,
        "starter_weeks": 8,
        "starter_feed_lbs": 50.0,
        "grower_weeks": 44,
        "grower_feed_lbs": 600.0,
        "starter_bag_price": 26.0,
        "grower_bag_price": 20.0,
        "unit_cost": 250.00,
        "processing_cost": 350.00,
        "expected_yield_lbs": 120.0,
        "sale_price_per_lb": 9.00,
        "weeks_to_proc": 52,
        "feed_total": 650.0,
    },
}

DEFAULT_BARREL_CAPACITY = 300  # lbs per barrel
DEFAULT_BAG_WEIGHT = 50  # lbs per feed bag

WORKSPACES = [
    "🗓️ Radar & Timeline",
    "🐾 Batch Logs",
    "🌾 Feed & Shopping",
    "💰 Financials & Freezer",
]

STATUS_OPTIONS = [
    "🟢 Active",
    "🥩 Harvested",
    "⚪ Excluded",
]


def infer_lifecycle_type(species: str, days_to_birth: int = 0) -> str:
    sp = str(species).strip().lower()
    if sp in LIVESTOCK_DATA:
        return str(LIVESTOCK_DATA[sp].get("lifecycle_type", "direct_arrival"))
    if any(
        kw in sp
        for kw in ("pig", "hog", "boar", "sow", "goat", "sheep", "lamb", "calf", "cattle", "cow", "rabbit", "doe")
    ):
        return "mammal_breeding" if int(days_to_birth) > 0 else "direct_arrival"
    if int(days_to_birth) > 0 or "egg" in sp:
        return "poultry_incubation"
    return "direct_arrival"


def get_lifecycle_labels(
    lifecycle_type: str | None = None,
    species: str = "",
    days_to_birth: int = 0,
) -> dict[str, str]:
    resolved_type = lifecycle_type or infer_lifecycle_type(
        species, days_to_birth
    )
    if resolved_type == "mammal_breeding":
        return {
            "lifecycle_type": "mammal_breeding",
            "initial_label": "Breeding",
            "gestation_label": "Gestation",
            "birth_label": "Birth",
            "birth_icon": "🐖",
            "date_input_label": "Expected Breeding Date (or Range)",
        }
    if resolved_type == "poultry_incubation":
        return {
            "lifecycle_type": "poultry_incubation",
            "initial_label": "Arrival",
            "gestation_label": "Incubation",
            "birth_label": "Hatch",
            "birth_icon": "🐣",
            "date_input_label": "Egg Set / Arrival Date (or Range)",
        }
    return {
        "lifecycle_type": "direct_arrival",
        "initial_label": "Arrival",
        "gestation_label": "Incubation",
        "birth_label": "Hatch",
        "birth_icon": "🐣",
        "date_input_label": "Arrival Date (or Range)",
    }


def get_species_defaults(species: str) -> dict:
    return dict(
        LIVESTOCK_DATA.get(
            str(species).strip().lower(), LIVESTOCK_DATA["chicken chicks"]
        )
    )


_EXACT_SPECIES_DISPLAY: dict[str, tuple[str, str]] = {
    "chicken eggs": ("Chicken", "Chickens"),
    "chicken chicks": ("Chicken", "Chickens"),
    "turkey chicks": ("Turkey", "Turkeys"),
    "turkey poults": ("Turkey", "Turkeys"),
    "turkey eggs": ("Turkey", "Turkeys"),
    "duck eggs": ("Duck", "Ducks"),
    "ducklings": ("Duck", "Ducks"),
    "duckling": ("Duck", "Ducks"),
    "goose eggs": ("Goose", "Geese"),
    "goslings": ("Goose", "Geese"),
    "gosling": ("Goose", "Geese"),
    "quail eggs": ("Quail", "Quail"),
    "quail chicks": ("Quail", "Quail"),
    "guinea eggs": ("Guinea", "Guineas"),
    "guinea keets": ("Guinea", "Guineas"),
    "kunekune pigs": ("Kunekune Pig", "Kunekune Pigs"),
    "chicks": ("Chicken", "Chickens"),
    "chick": ("Chicken", "Chickens"),
    "poults": ("Turkey", "Turkeys"),
    "poult": ("Turkey", "Turkeys"),
    "piglets": ("Pig", "Pigs"),
    "piglet": ("Pig", "Pigs"),
    "keets": ("Guinea", "Guineas"),
    "keet": ("Guinea", "Guineas"),
}

_STAGE_SUFFIXES = frozenset({
    "egg", "eggs", "chick", "chicks", "poult", "poults", "keet", "keets", "hatchling", "hatchlings",
})

_JUVENILE_TO_ADULT = {
    "duckling": "duck",
    "ducklings": "duck",
    "gosling": "goose",
    "goslings": "goose",
    "piglet": "pig",
    "piglets": "pig",
}

_INVARIANT_SPECIES = frozenset({
    "sheep", "quail", "beef", "cattle", "swine", "poultry", "livestock", "cross",
})

_IRREGULAR_PAIRS = {
    "goose": ("goose", "geese"),
    "geese": ("goose", "geese"),
    "calf": ("calf", "calves"),
    "calves": ("calf", "calves"),
    "turkey": ("turkey", "turkeys"),
    "turkeys": ("turkey", "turkeys"),
    "chicken": ("chicken", "chickens"),
    "chickens": ("chicken", "chickens"),
    "pig": ("pig", "pigs"),
    "pigs": ("pig", "pigs"),
    "hog": ("hog", "hogs"),
    "hogs": ("hog", "hogs"),
    "duck": ("duck", "ducks"),
    "ducks": ("duck", "ducks"),
    "lamb": ("lamb", "lambs"),
    "lambs": ("lamb", "lambs"),
    "goat": ("goat", "goats"),
    "goats": ("goat", "goats"),
    "rabbit": ("rabbit", "rabbits"),
    "rabbits": ("rabbit", "rabbits"),
    "broiler": ("broiler", "broilers"),
    "broilers": ("broiler", "broilers"),
    "roaster": ("roaster", "roasters"),
    "roasters": ("roaster", "roasters"),
    "ranger": ("ranger", "rangers"),
    "rangers": ("ranger", "rangers"),
    "alpaca": ("alpaca", "alpacas"),
    "alpacas": ("alpaca", "alpacas"),
    "steer": ("steer", "steers"),
    "steers": ("steer", "steers"),
    "cow": ("cow", "cows"),
    "cows": ("cow", "cows"),
    "guinea": ("guinea", "guineas"),
    "guineas": ("guinea", "guineas"),
}

_EXACT_PRESET_LABELS: dict[str, str] = {
    "chicken eggs": "Chicken (Hatching Eggs)",
    "chicken chicks": "Chicken (Day-Old Chicks)",
    "turkey chicks": "Turkey (Poults / Chicks)",
    "turkey eggs": "Turkey (Hatching Eggs)",
    "turkey poults": "Turkey (Poults)",
    "duck eggs": "Duck (Hatching Eggs)",
    "goose eggs": "Goose (Hatching Eggs)",
    "quail eggs": "Quail (Hatching Eggs)",
    "kunekune pigs": "Kunekune Pigs",
}

_SPECIES_TERMS_TABLE: tuple[tuple[str, dict[str, object]], ...] = (
    (
        r"pig|pigs|piglet|piglets|hog|hogs|boar|boars|sow|sows|swine|kunekune|berkshire|duroc|mangalitsa|tamworth|hampshire|yorkshire",
        {
            "singular": "pig",
            "plural": "pigs",
            "category": "pig",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Barrow, gilt, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised pork ~120 lb hanging wt. Deposit holds your spot.",
            "ntfy_tag": "pig2",
        },
    ),
    (
        r"turkey|turkeys|poult|poults",
        {
            "singular": "turkey",
            "plural": "turkeys",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Tom, hen, freezer...",
            "preorder_placeholder": "e.g. Fresh pasture-raised turkeys ~16–20 lb. $25/turkey deposit holds your spot.",
            "ntfy_tag": "turkey",
        },
    ),
    (
        r"duck|ducks|duckling|ducklings|pekin|muscovy",
        {
            "singular": "duck",
            "plural": "ducks",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Drake, hen, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised ducks ~5–6 lb dressed. Deposit holds your spot.",
            "ntfy_tag": "duck",
        },
    ),
    (
        r"goose|geese|gosling|goslings",
        {
            "singular": "goose",
            "plural": "geese",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Gander, goose, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised geese ~10–12 lb dressed. Deposit holds your spot.",
            "ntfy_tag": "bird",
        },
    ),
    (
        r"quail",
        {
            "singular": "quail",
            "plural": "quail",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Pack size, freezer...",
            "preorder_placeholder": "e.g. Fresh dressed quail. Deposit holds your spot.",
            "ntfy_tag": "bird",
        },
    ),
    (
        r"guinea|guineas|keet|keets|pheasant|pheasants|poultry|bird|birds",
        {
            "singular": "bird",
            "plural": "birds",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Weight note, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised poultry. Deposit holds your spot.",
            "ntfy_tag": "bird",
        },
    ),
    (
        r"chicken|chickens|cornish|broiler|broilers|roaster|roasters|ranger|rangers|pullet|pullets|cockerel|cockerels|chick|chicks",
        {
            "singular": "chicken",
            "plural": "chickens",
            "category": "poultry",
            "is_poultry": True,
            "is_mammal": False,
            "notes_placeholder": "Small, large, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised broilers ~4–5 lb dressed. Deposit holds your spot.",
            "ntfy_tag": "chicken",
        },
    ),
    (
        r"beef|cattle|cow|cows|steer|steers|heifer|heifers|calf|calves|angus|hereford|dexter|highland",
        {
            "singular": "animal",
            "plural": "animals",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Steer, heifer, freezer...",
            "preorder_placeholder": "e.g. Grass-fed beef priced by hanging weight. Deposit holds your share.",
            "ntfy_tag": "cow2",
        },
    ),
    (
        r"lamb|lambs",
        {
            "singular": "lamb",
            "plural": "lambs",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Wether, ewe, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised lamb. Deposit holds your spot.",
            "ntfy_tag": "sheep",
        },
    ),
    (
        r"sheep|ewe|ewes|ram|rams|wether|wethers|katahdin|dorper",
        {
            "singular": "sheep",
            "plural": "sheep",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Wether, ewe, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised sheep & lamb. Deposit holds your spot.",
            "ntfy_tag": "sheep",
        },
    ),
    (
        r"goat|goats|kid|kids|boer|kiko",
        {
            "singular": "goat",
            "plural": "goats",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Wether, doe, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised goat. Deposit holds your spot.",
            "ntfy_tag": "goat",
        },
    ),
    (
        r"rabbit|rabbits|bunny|fryer|fryers",
        {
            "singular": "rabbit",
            "plural": "rabbits",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Fryer, roaster, freezer...",
            "preorder_placeholder": "e.g. Pasture-raised meat rabbits ~3–4 lb dressed. Deposit holds your spot.",
            "ntfy_tag": "rabbit2",
        },
    ),
    (
        r"alpaca|alpacas|llama|llamas",
        {
            "singular": "alpaca",
            "plural": "alpacas",
            "category": "mammal",
            "is_poultry": False,
            "is_mammal": True,
            "notes_placeholder": "Animal note...",
            "preorder_placeholder": "e.g. Reservation details & deposit info.",
            "ntfy_tag": "llama",
        },
    ),
)


def format_quantity_fraction(qty: int | float) -> str:
    """Format head counts or share fractions cleanly (e.g., 4, ½, 1½, ¼, ¾)."""
    v = round(float(qty or 0.0), 2)
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    whole = int(v)
    frac = round(v - whole, 2)
    frac_map = {0.25: "¼", 0.5: "½", 0.75: "¾"}
    for target, glyph in frac_map.items():
        if abs(frac - target) < 1e-6:
            return f"{whole}{glyph}" if whole > 0 else glyph
    return f"{v:g}"


def _normalize_species_pair(species: str) -> tuple[str, str]:
    """Return clean (singular_title, plural_title) display strings for a species/preset."""
    raw = str(species or "").strip()
    if not raw:
        return ("Livestock", "Livestock")
    key = " ".join(raw.lower().split())
    if key in _EXACT_SPECIES_DISPLAY:
        return _EXACT_SPECIES_DISPLAY[key]

    words = key.split()
    if len(words) >= 2 and words[-1] in _STAGE_SUFFIXES:
        words = words[:-1]
    if words and words[-1] in _JUVENILE_TO_ADULT:
        words[-1] = _JUVENILE_TO_ADULT[words[-1]]

    last = words[-1]
    if last in _INVARIANT_SPECIES:
        sg_last, pl_last = last, last
    elif last in _IRREGULAR_PAIRS:
        sg_last, pl_last = _IRREGULAR_PAIRS[last]
    elif last.endswith("ies") and len(last) > 4:
        sg_last, pl_last = last[:-3] + "y", last
    elif last.endswith("s") and not last.endswith(("ss", "us", "is")) and len(last) > 3:
        sg_last, pl_last = last[:-1], last
    else:
        sg_last = last
        pl_last = last if last.endswith(("s", "x", "z", "ch", "sh")) else f"{last}s"

    prefix = " ".join(w.title() for w in words[:-1])
    sg = f"{prefix} {sg_last.title()}".strip() if prefix else sg_last.title()
    pl = f"{prefix} {pl_last.title()}".strip() if prefix else pl_last.title()
    return (sg, pl)


def format_species_display(species: str, *, plural: bool = False) -> str:
    """Format species for tables, headings, and customer breakdowns without life-stage confusion."""
    sg, pl = _normalize_species_pair(species)
    return pl if plural else sg


def format_species_preset_label(species: str) -> str:
    """Format species preset keys with clear stage qualifiers for batch creation & Settings."""
    raw = str(species or "").strip()
    if not raw:
        return "Livestock"
    key = " ".join(raw.lower().split())
    if key in _EXACT_PRESET_LABELS:
        return _EXACT_PRESET_LABELS[key]
    words = key.split()
    if len(words) >= 2:
        tail = words[-1]
        base_disp = format_species_display(raw)
        if tail in ("egg", "eggs"):
            return f"{base_disp} (Hatching Eggs)"
        if tail in ("chick", "chicks"):
            return f"{base_disp} (Chicks)"
        if tail in ("poult", "poults"):
            return f"{base_disp} (Poults)"
    return raw.title()


def get_species_terminology(
    species: str, lifecycle_type: str | None = None
) -> dict[str, object]:
    """Return species-aware singular/plural unit nouns and UI strings."""
    sp_low = str(species or "").strip().lower()
    matched: dict[str, object] | None = None
    for pat, info in _SPECIES_TERMS_TABLE:
        if re.search(rf"\b(?:{pat})\b", sp_low):
            matched = dict(info)
            break

    if matched is None:
        if lifecycle_type == "poultry_incubation" or re.search(r"\b(?:egg|eggs)\b", sp_low):
            matched = {
                "singular": "bird",
                "plural": "birds",
                "category": "poultry",
                "is_poultry": True,
                "is_mammal": False,
                "notes_placeholder": "Weight note, freezer...",
                "preorder_placeholder": "e.g. Pasture-raised poultry. Deposit holds your spot.",
                "ntfy_tag": "bird",
            }
        else:
            is_mam = lifecycle_type == "mammal_breeding"
            matched = {
                "singular": "animal",
                "plural": "animals",
                "category": "mammal" if is_mam else "other",
                "is_poultry": False,
                "is_mammal": is_mam,
                "notes_placeholder": "Tag, cut note, freezer...",
                "preorder_placeholder": "e.g. Reservation details & deposit info.",
                "ntfy_tag": "tada",
            }

    sg = str(matched["singular"])
    pl = str(matched["plural"])
    matched["singular_title"] = sg.title()
    matched["plural_title"] = pl.title()
    matched["display_singular"] = format_species_display(species, plural=False)
    matched["display_plural"] = format_species_display(species, plural=True)
    matched["preset_label"] = format_species_preset_label(species)
    return matched

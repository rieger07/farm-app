"""Species presets, constants, and workspace definitions for Homestead Planner."""

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

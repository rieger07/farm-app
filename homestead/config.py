"""Species presets, constants, and workspace definitions for Homestead Planner."""

# Livestock Data Dictionary (Default Species Presets)
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


def get_species_defaults(species: str) -> dict:
    return LIVESTOCK_DATA.get(
        str(species).lower(), LIVESTOCK_DATA["chicken chicks"]
    )

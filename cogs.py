"""
cogs.py — COGS Calculator Blueprint

Routes:
  GET  /cogs                          cogs_page
  GET  /api/cogs                      get_cogs
  PATCH /api/cogs                     update_cogs
  GET  /api/cogs/recipe/<name>        get_recipe_cogs
  POST /api/cogs/reset                reset_cogs
  POST /api/cogs/compute              compute_cogs_scenario
"""
import os
import json
import logging

from flask import Blueprint, request, jsonify, render_template

from helpers import (
    _load_json, _save_json,
    login_required, _render,
    load_recipes, _normalize_format,
    DATA_DIR,
)

logger = logging.getLogger(__name__)

COGS_PATH = os.path.join(DATA_DIR, "cogs.json")

cogs_bp = Blueprint("cogs", __name__)

# ══════════════════════════════════════════════════════════════════════════════
# COGS CALCULATOR ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# Maps app format codes to COGS matrix size keys
_FORMAT_TO_COGS_SIZE = {
    "SS-473ML":    "475ml",
    "SS-474ML":    "475ml",
    "SS-475ML":    "475ml",
    "SS-476ML":    "475ml",
    "SS-750ML":    "750ml_ss",
    "SS-735ML":    "750ml_ss",
    "SS-876ML":    "876ml",
    "FZ-750ML":    "750ml_fz",
    "FZ-780ML":    "750ml_fz",
    "BB-750ML":    "750ml_bb",
    "BB-780ML":    "750ml_bb",
    "POUCH-750ML": "750ml_pouch",
    "POUCH-780ML": "750ml_pouch",
}

# Ingredient keywords for each costed category (bones, mirepoix, mushroom)
_COGS_INGREDIENT_KEYWORDS = {
    "bones": [
        "bone", "knuckle", "neck", "back", "foot", "feet", "marrow",
        "oxtail", "carcass", "drumstick", "wing", "frame", "rib",
        "femur", "tibia", "chicken", "beef", "turkey", "bison", "pork",
    ],
    "mushroom": [
        "mushroom", "shiitake", "porcini", "portobello", "cremini",
        "reishi", "chaga", "maitake", "enoki", "chanterelle",
    ],
}

_COGS_SEED = {
    # ── Production volume ──────────────────────────────────────────────────────
    "units_per_kettle": {
        "475ml": 290, "750ml_ss": 190, "876ml": 144,
        "750ml_fz": 180, "750ml_bb": 180, "750ml_pouch": 180
    },
    "kettles_per_day":          3,    # full production
    "production_days_per_month":28,
    "slow_months_per_year":     4,
    "slow_kettles_per_day":     1.5,  # kettles/day during slow months (NOT a %)

    # ── Overhead — split into fixed and variable lines ─────────────────────────
    "overhead_fixed": [
        {"name": "Bookkeeper",            "monthly": 900.00},
        {"name": "Cathy",                 "monthly": 100.00},
        {"name": "Accountant",            "monthly": 200.00},
        {"name": "Rent",                  "monthly": 4000.00},
        {"name": "Phone",                 "monthly": 96.00},
        {"name": "Internet",              "monthly": 151.00},
        {"name": "Business Insurance",    "monthly": 368.12},
        {"name": "Business License",      "monthly": 247.00},
        {"name": "Sync Data Storage",     "monthly": 16.00},
        {"name": "Fastmail",              "monthly": 36.00},
        {"name": "Banking Fee",           "monthly": 22.50},
    ],
    "overhead_variable": [
        {"name": "Enbridge (gas)",        "monthly": 1400.00},
        {"name": "Hydro Meter #977",      "monthly": 1859.11},
        {"name": "Hydro Meter #550",      "monthly": 491.25},
        {"name": "Water",                 "monthly": 162.82},
        {"name": "Linen Service",         "monthly": 200.00},
        {"name": "Garbage Pick-up",       "monthly": 47.58},
        {"name": "Garbage Disposal",      "monthly": 520.00},
        {"name": "Green Bin & Recycling", "monthly": 50.00},
    ],

    # ── Labour (+8% uplift on hourly roles for vacation/tax/WSIB) ─────────────
    "labour": [
        # Salaried: use type="salary", annual_salary field
        {"role": "President (Chris Kaspiris)", "type": "salary", "annual_salary": 40000,
         "weekly_cost": 769.23},
        # Hourly: loaded_rate = base * 1.08, weekday + weekend hrs separate
        {"role": "CEO (Michel Salvador)",      "type": "hourly", "base_hourly": 50.00,
         "uplift_pct": 8, "weekday_hours": 40, "weekend_hours": 0},
        {"role": "Head of Operations",         "type": "hourly", "base_hourly": 37.00,
         "uplift_pct": 8, "weekday_hours": 40, "weekend_hours": 0},
        {"role": "Broth 1 (Kitchen Lead)",     "type": "hourly", "base_hourly": 28.00,
         "uplift_pct": 8, "weekday_hours": 40, "weekend_hours": 16},
        {"role": "Broth 2 (Kitchen Staff)",    "type": "hourly", "base_hourly": 23.00,
         "uplift_pct": 8, "weekday_hours": 40, "weekend_hours": 16},
    ],

    # ── Supplies ───────────────────────────────────────────────────────────────
    "supplies": [
        {"name": "Sabrina (kitchen/cleaning)", "monthly": 1155.00},
        {"name": "Staples",                    "monthly": 80.00},
        {"name": "Nella",                      "monthly": 300.00},
        {"name": "Uline",                      "monthly": 500.00},
        {"name": "Dymo",                       "monthly": 130.00},
    ],

    # ── Maintenance ───────────────────────────────────────────────────────────
    "maintenance_monthly": 1500.00,

    # ── Equipment depreciation ────────────────────────────────────────────────
    "equipment": [
        {"name": "Rational Combi Ovens (×2)",  "cost": 50000,  "life_years": 15},
        {"name": "Kettles (×3, used)",          "cost": 54000,  "life_years": 8},
        {"name": "Delta HVAC",                  "cost": 173455, "life_years": 20},
        {"name": "Dishwashers (×3)",            "cost": 10000,  "life_years": 8},
        {"name": "Label Machine",               "cost": 25000,  "life_years": 12},
        {"name": "Fridges",                     "cost": 10000,  "life_years": 12},
        {"name": "Freezer (upstairs)",          "cost": 10000,  "life_years": 15},
        {"name": "Freezer (downstairs)",        "cost": 20000,  "life_years": 15},
    ],

    # ── Debt repayment ────────────────────────────────────────────────────────
    "debt_repayment_monthly": 16666.00,

    # ── Bones $/kg — actual recipe kg read from recipe card ───────────────────
    "bones": {
        "chicken": {"price_per_kg": 2.20, "base_kg": 50, "extra_kg": 25},
        "beef":    {"price_per_kg": 3.50, "base_kg": 60, "extra_kg": 15},
        "turkey":  {"price_per_kg": 4.40, "base_kg": 50,
                    "whole_turkey_cost_per_batch": 180.00},  # 2 whole turkeys
    },

    # ── Mushroom — two types ──────────────────────────────────────────────────
    "mushroom_fresh":     {"price_per_kg": 11.66, "kg_per_batch": 16.0},
    "mushroom_specialty": {"price_per_kg": 22.00,  "kg_per_batch": 3.0},

    # ── Mirepoix prices $/kg (used for per-recipe veg cost from recipe card) ──
    "mirepoix": {
        "onion":  {"price_per_kg": 1.20},
        "carrot": {"price_per_kg": 1.50},
        "celery": {"price_per_kg": 2.20},
    },

    # ── Other ingredients (flat $/batch — salt + spices) ──────────────────────
    # These are NOT per-ingredient from recipe — kept as flat batch cost
    # Mirepoix: $2.40/kg × 9.5kg = $22.80
    # Salt: $6/kg × 0.675kg = $4.05
    # Spices: $40/kg × 0.1kg = $4.00
    "other_ingredients_per_batch": 30.85,

    # ── Packaging $/unit ──────────────────────────────────────────────────────
    "packaging": {
        "475ml":       {"container": 1.00, "lid": 0.35, "box": 0.00},
        "750ml_ss":    {"container": 1.00, "lid": 0.35, "box": 0.00},
        "876ml":       {"container": 1.10, "lid": 0.35, "box": 0.00},
        "750ml_fz":    {"container": 0.35, "lid": 0.00, "box": 0.16},
        "750ml_bb":    {"container": 0.35, "lid": 0.00, "box": 0.16},
        "750ml_pouch": {"container": 1.00, "lid": 0.00, "box": 0.00},
    },

    # ── Labels $/unit (base cost + hand-application surcharge where applicable)
    # Hand-apply surcharge: $80/day allocated across units/day for that format
    "labels": {
        "475ml":       {"cost": 0.49, "hand_applied": False, "hand_apply_surcharge": 0.00},
        "750ml_ss":    {"cost": 0.43, "hand_applied": True,  "hand_apply_surcharge": 0.14},
        "876ml":       {"cost": 0.49, "hand_applied": False, "hand_apply_surcharge": 0.00},
        "750ml_fz":    {"cost": 0.81, "hand_applied": True,  "hand_apply_surcharge": 0.15},
        "750ml_bb":    {"cost": 0.00, "hand_applied": False, "hand_apply_surcharge": 0.00},
        "750ml_pouch": {"cost": 0.00, "hand_applied": False, "hand_apply_surcharge": 0.00},
    },
}


COGS_SCHEMA_VERSION = 4  # v4: added mirepoix to seed  # increment when seed structure changes

def _load_cogs():
    """Load COGS data. Always validates structure against current seed version."""
    data = _load_json(COGS_PATH, {}) if os.path.exists(COGS_PATH) else {}

    # Force reseed if version mismatch OR any required structural key missing
    required_keys = {"overhead_fixed", "overhead_variable", "slow_kettles_per_day",
                     "supplies", "mushroom_fresh", "mushroom_specialty",
                     "other_ingredients_per_batch", "mirepoix"}
    needs_migration = (
        data.get("schema_version", 0) < COGS_SCHEMA_VERSION
        or not required_keys.issubset(data.keys())
    )

    if needs_migration:
        migrated = {k: v for k, v in _COGS_SEED.items()}  # fresh copy of seed
        # Preserve scalar user edits if present and non-zero
        for key in ("maintenance_monthly", "debt_repayment_monthly",
                    "other_ingredients_per_batch"):
            if data.get(key) and float(data[key]) > 0:
                migrated[key] = data[key]
        # Preserve equipment list if it has the correct structure
        if (data.get("equipment") and isinstance(data["equipment"], list)
                and all("life_years" in e and "cost" in e for e in data["equipment"])):
            migrated["equipment"] = data["equipment"]
        migrated["schema_version"] = COGS_SCHEMA_VERSION
        _save_json(COGS_PATH, migrated)
        return migrated

    return data


def _compute_cogs_matrix(c, recipe_overrides=None):
    """Compute the 24-cell COGS matrix from inputs.
    Matches the logic in COGS_Master_Updated.xlsx exactly.

    recipe_overrides: {bones_kg, mushroom_kg, broth_type, is_mushroom_broth}
    Other ingredients (mirepoix etc.) are flat $/batch from other_ingredients_per_batch.
    """
    # ── Annual kettle runs (spreadsheet logic: slow months use slow_kettles_per_day)
    days_mo     = float(c.get("production_days_per_month", 28))
    full_k      = float(c.get("kettles_per_day", 3))
    slow_k      = float(c.get("slow_kettles_per_day", 1.5))
    slow_mo     = float(c.get("slow_months_per_year", 4))
    full_mo     = 12 - slow_mo
    annual_runs = (full_mo * full_k * days_mo) + (slow_mo * slow_k * days_mo)
    if annual_runs <= 0:
        annual_runs = 840

    # ── Fixed costs per kettle run ─────────────────────────────────────────────
    # Overhead: fixed + variable, allocated per run
    oh_fixed   = sum(float(x.get("monthly", 0)) for x in c.get("overhead_fixed", []))
    oh_var     = sum(float(x.get("monthly", 0)) for x in c.get("overhead_variable", []))
    # Fallback: old single overhead list if migrating from old format
    oh_legacy  = sum(float(x.get("monthly", 0)) for x in c.get("overhead", []))
    overhead_mo = oh_fixed + oh_var + oh_legacy
    overhead_annual = overhead_mo * 12

    # Labour — salaried vs hourly with +uplift%
    labour_annual = 0.0
    for r in c.get("labour", []):
        if r.get("type") == "salary":
            labour_annual += float(r.get("annual_salary", 0))
        else:
            loaded = float(r.get("base_hourly", 0)) * (1 + float(r.get("uplift_pct", 8)) / 100)
            weekly = loaded * (float(r.get("weekday_hours", 0)) + float(r.get("weekend_hours", 0)))
            labour_annual += weekly * 52

    # Supplies — list of line items
    supplies_list = c.get("supplies", [])
    if supplies_list:
        supplies_annual = sum(float(x.get("monthly", 0)) for x in supplies_list) * 12
    else:
        supplies_annual = float(c.get("supplies_monthly", 0)) * 12

    maint_annual = float(c.get("maintenance_monthly", 0)) * 12
    dep_annual   = sum(float(e.get("cost", 0)) / max(1, float(e.get("life_years", 1)))
                       for e in c.get("equipment", []))
    debt_annual  = float(c.get("debt_repayment_monthly", 0)) * 12

    total_annual = overhead_annual + labour_annual + supplies_annual + maint_annual + dep_annual + debt_annual
    cost_per_run = total_annual / annual_runs

    # ── Other ingredients (flat $/batch) ──────────────────────────────────────
    other_per_batch = float(c.get("other_ingredients_per_batch",
                                   c.get("minor_ingredients_per_batch", 30.85)))

    matrix = {}
    broth_types = ["Chicken", "Beef", "Turkey", "Mushroom"]

    for bt in broth_types:
        bt_key = bt.lower()
        matrix[bt] = {}

        for size_key, units in c.get("units_per_kettle", {}).items():
            units = max(1, int(units))

            fixed_pu = cost_per_run / units
            other_pu = other_per_batch / units

            bone_pu    = 0.0
            mush_pu    = 0.0

            if recipe_overrides:
                bones_kg    = float(recipe_overrides.get("bones_kg", 0))
                mushroom_kg = float(recipe_overrides.get("mushroom_kg", 0))

                # Bone cost — each bone type priced individually
                # Broth type only determines matrix row; actual cost uses per-type kg × $/kg
                bone_prices   = c.get("bones", {})
                bp            = bone_prices.get(bt_key, {})

                chicken_kg_r  = float(recipe_overrides.get("chicken_kg", 0))
                beef_kg_r     = float(recipe_overrides.get("beef_kg", 0))
                turkey_kg_r   = float(recipe_overrides.get("turkey_kg", 0))
                untyped_kg    = float(recipe_overrides.get("untyped_kg", 0))

                chicken_price = float(bone_prices.get("chicken", {}).get("price_per_kg", 0))
                beef_price    = float(bone_prices.get("beef",    {}).get("price_per_kg", 0))
                turkey_price  = float(bone_prices.get("turkey",  {}).get("price_per_kg", 0))
                broth_price   = float(bp.get("price_per_kg", 0))  # for untyped bones

                bone_pu = (
                    chicken_kg_r * chicken_price +
                    beef_kg_r    * beef_price +
                    turkey_kg_r  * turkey_price +
                    untyped_kg   * broth_price
                ) / max(units, 1)

                # Mirepoix cost is included in other_ingredients_per_batch (flat)
                # No separate per-kg calculation to avoid double-counting

                # Turkey flat cost: add when any turkey bones present
                if turkey_kg_r > 0 or bt_key == "turkey":
                    whole_cost = float(bone_prices.get("turkey", {}).get("whole_turkey_cost_per_batch", 0))
                    bone_pu   += whole_cost / max(units, 1)

                # Mushroom: from recipe kg if mushroom broth,
                # OR use flat $/batch from seed if no kg provided
                if bt_key == "mushroom":
                    # Use blended $/kg rate × actual recipe mushroom kg
                    mf = c.get("mushroom_fresh", {})
                    ms = c.get("mushroom_specialty", {})
                    mf_kg = float(mf.get("kg_per_batch", 16))
                    ms_kg = float(ms.get("kg_per_batch", 3))
                    total_mush_kg = mf_kg + ms_kg
                    blended_mush_rate = (
                        float(mf.get("price_per_kg", 11.66)) * mf_kg +
                        float(ms.get("price_per_kg", 22.00)) * ms_kg
                    ) / max(total_mush_kg, 0.001)
                    # Use recipe kg if available, else fall back to standard batch total
                    effective_mush_kg = mushroom_kg if mushroom_kg > 0 else total_mush_kg
                    mush_pu = (effective_mush_kg * blended_mush_rate) / units
            else:
                # Base matrix (no recipe): use spreadsheet's known bone amounts for reference
                # These are the base kg from the spreadsheet
                bone_prices = c.get("bones", {})
                bp          = bone_prices.get(bt_key, {})
                # Read base_kg from cogs data if present, else use spreadsheet defaults
                _DEFAULT_BASE_KG = {"chicken": 50, "beef": 60, "turkey": 50, "mushroom": 0}
                base_kg     = float(bp.get("base_kg", _DEFAULT_BASE_KG.get(bt_key, 0)))
                bone_price  = float(bp.get("price_per_kg", 0))
                bone_pu     = (base_kg * bone_price) / units
                if bt_key == "turkey":
                    whole_cost = float(bp.get("whole_turkey_cost_per_batch", 180))
                    bone_pu   += whole_cost / units
                if bt_key == "mushroom":
                    mf = c.get("mushroom_fresh", {})
                    ms = c.get("mushroom_specialty", {})
                    mf_kg = float(mf.get("kg_per_batch", 16))
                    ms_kg = float(ms.get("kg_per_batch", 3))
                    total_mush_kg = mf_kg + ms_kg
                    blended_mush_rate = (
                        float(mf.get("price_per_kg", 11.66)) * mf_kg +
                        float(ms.get("price_per_kg", 22.00)) * ms_kg
                    ) / max(total_mush_kg, 0.001)
                    mush_pu = (total_mush_kg * blended_mush_rate) / units

            # Packaging
            pack = c.get("packaging", {}).get(size_key, {})
            pack_pu = sum(float(pack.get(k, 0)) for k in ("container", "lid", "box"))

            # Label
            lbl     = c.get("labels", {}).get(size_key, {})
            label_pu = float(lbl.get("cost", 0)) + float(lbl.get("hand_apply_surcharge", 0))

            total = fixed_pu + bone_pu + mush_pu + other_pu + pack_pu + label_pu

            matrix[bt][size_key] = {
                "total": round(total, 4),
                "breakdown": {
                    "fixed":             round(fixed_pu, 4),
                    "bones":             round(bone_pu, 4),
                    "mushroom":          round(mush_pu, 4),
                    "other_ingredients": round(other_pu, 4),
                    "packaging":         round(pack_pu, 4),
                    "label":             round(label_pu, 4),
                },
            }

    return matrix, round(cost_per_run, 2), round(annual_runs / 12, 1)


def _extract_recipe_kg(recipe_data):
    """Extract costed ingredient kg from kettle_overnight section of a recipe.
    Extracts: bones (by type) and mushroom.
    All other ingredients (mirepoix, salt, spices) use the flat
    other_ingredients_per_batch cost to avoid double-counting.
    """
    result = {"bones_kg": 0.0, "mushroom_kg": 0.0}
    kettle = recipe_data.get("kettle_overnight", [])
    for item in kettle:
        if not isinstance(item, dict):
            continue
        name   = (item.get("name") or "").lower()
        unit   = (item.get("unit") or "").lower()
        amount = float(item.get("amount") or 0)
        if amount <= 0:
            continue
        # Convert to kg
        if unit == "g":
            amount /= 1000
        elif unit in ("lb", "lbs"):
            amount *= 0.4536
        elif unit in ("per l", "per_l", "ml"):
            continue  # not a weight

        kw = _COGS_INGREDIENT_KEYWORDS
        if any(kw_word in name for kw_word in kw["bones"]):
            result["bones_kg"] += amount  # total for fallback
            # Also track per-type for accurate mixed-bone pricing
            if "chicken" in name or "carcass" in name or "drumstick" in name or "wing" in name:
                result["chicken_kg"] = result.get("chicken_kg", 0.0) + amount
            elif "beef" in name or "oxtail" in name or "bison" in name:
                result["beef_kg"] = result.get("beef_kg", 0.0) + amount
            elif "turkey" in name:
                result["turkey_kg"] = result.get("turkey_kg", 0.0) + amount
            else:
                # Unknown bone type — attribute to whichever broth type is set
                result["untyped_kg"] = result.get("untyped_kg", 0.0) + amount
        elif any(kw_word in name for kw_word in kw["mushroom"]):
            result["mushroom_kg"] += amount

    return result


def _recipe_unit_cogs(recipe_data, cogs_data=None, label_supplied=False):
    """Compute unit COGS for a single recipe.
    Returns (unit_cogs, breakdown_dict) or (None, None) if recipe lacks required fields.
    """
    broth_type = (recipe_data.get("broth_type") or "").strip()
    fmt        = _normalize_format((recipe_data.get("format") or "").strip()).upper()
    if not broth_type or not fmt:
        return None, None

    size_key = _FORMAT_TO_COGS_SIZE.get(fmt)
    if not size_key:
        return None, None

    if cogs_data is None:
        cogs_data = _load_cogs()

    kg = _extract_recipe_kg(recipe_data)
    matrix, _, _ = _compute_cogs_matrix(cogs_data, recipe_overrides={**kg, "broth_type": broth_type})

    bt_matrix = matrix.get(broth_type.capitalize()) or matrix.get(broth_type)
    if not bt_matrix:
        # Try case-insensitive
        bt_matrix = next((v for k, v in matrix.items() if k.lower() == broth_type.lower()), None)
    if not bt_matrix:
        return None, None

    cell = bt_matrix.get(size_key)
    if not cell:
        return None, None

    unit_cogs = cell["total"]
    breakdown = dict(cell["breakdown"])

    if label_supplied:
        lbl = cogs_data.get("labels", {}).get(size_key, {})
        label_cost = float(lbl.get("cost", 0)) + float(lbl.get("hand_apply_surcharge", 0))
        unit_cogs  = round(unit_cogs - label_cost, 4)
        breakdown["label"] = 0.0
        breakdown["label_supplied"] = True

    return round(unit_cogs, 2), breakdown


@cogs_bp.route("/cogs")
@login_required
def cogs_page():
    return _render("cogs.html")


@cogs_bp.route("/api/cogs", methods=["GET"])
@login_required
def get_cogs():
    c = _load_cogs()
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(c)
    # Compute blended mushroom rate to expose in API
    mf = c.get("mushroom_fresh", {})
    ms = c.get("mushroom_specialty", {})
    mf_kg = float(mf.get("kg_per_batch", 16))
    ms_kg = float(ms.get("kg_per_batch", 3))
    total_mush_kg = mf_kg + ms_kg
    blended_mushroom_rate = round(
        (float(mf.get("price_per_kg", 11.66)) * mf_kg +
         float(ms.get("price_per_kg", 22.00)) * ms_kg)
        / max(total_mush_kg, 0.001), 4
    )

    return jsonify({
        "inputs": c,
        "matrix": matrix,
        "cost_per_run": cost_per_run,
        "avg_runs_per_month": avg_runs,
        "blended_mushroom_rate": blended_mushroom_rate,
        "size_labels": {
            "475ml": "475ml SS",
            "750ml_ss": "750ml SS",
            "876ml": "876ml SS",
            "750ml_fz": "750ml FZ",
            "750ml_bb": "750ml BB",
            "750ml_pouch": "750ml Pouch",
        }
    })


@cogs_bp.route("/api/cogs", methods=["PATCH"])
@login_required
def update_cogs():
    data = request.get_json() or {}
    c = _load_cogs()
    # Merge top-level keys
    for key, val in data.items():
        c[key] = val
    _save_json(COGS_PATH, c)
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(c)
    return jsonify({
        "ok": True,
        "matrix": matrix,
        "cost_per_run": cost_per_run,
        "avg_runs_per_month": avg_runs,
    })


@cogs_bp.route("/api/cogs/recipe/<path:recipe_name>", methods=["GET"])
@login_required
def get_recipe_cogs(recipe_name):
    """Return computed unit COGS for a specific recipe."""
    recipes = load_recipes()
    recipe  = recipes.get(recipe_name)
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404
    label_supplied = request.args.get("label_supplied", "false").lower() == "true"
    cogs_data = _load_cogs()
    unit_cogs, breakdown = _recipe_unit_cogs(recipe, cogs_data, label_supplied)
    kg = _extract_recipe_kg(recipe)
    return jsonify({
        "recipe":       recipe_name,
        "broth_type":   recipe.get("broth_type", ""),
        "format":       recipe.get("format", ""),
        "unit_cogs":    unit_cogs,
        "breakdown":    breakdown,
        "ingredient_kg": kg,
    })


@cogs_bp.route("/api/cogs/reset", methods=["POST"])
@login_required
def reset_cogs():
    """Force-reset cogs.json to the current seed values. Used for migration."""
    seed = dict(_COGS_SEED, schema_version=COGS_SCHEMA_VERSION)
    _save_json(COGS_PATH, seed)
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(seed)
    return jsonify({"ok": True, "cost_per_run": cost_per_run,
                    "avg_runs": avg_runs, "matrix": matrix})


@cogs_bp.route("/api/cogs/compute", methods=["POST"])
@login_required
def compute_cogs_scenario():
    """Compute COGS matrix from a scenario (modified inputs) without saving.
    Used for R&D / what-if exploration.
    """
    data   = request.get_json() or {}
    inputs = data.get("inputs", _load_cogs())
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(inputs)
    return jsonify({
        "matrix":              matrix,
        "cost_per_run":        cost_per_run,
        "avg_runs_per_month":  avg_runs,
    })


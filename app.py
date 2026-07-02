"""
Soma Bone Broth - Production Scheduler v5
Dashboard-based navigation, daily production with FINISH/START,
label generation, traceability records, master CCP reference.
"""

from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, session, redirect, url_for
from datetime import datetime, timedelta, timezone
from pdf_engine import generate_weekly_schedule_pdf, generate_daily_package_pdf, generate_filled_checklist_pdf, generate_label_pdf, generate_single_recipe_pdf, generate_all_recipes_pdf
from functools import wraps
import json
import os
import time
import threading
import logging
logger = logging.getLogger(__name__)

# Pin the process timezone so bare datetime.now()/today() across the app
# return Toronto local time (audits, sales records, FG lots, etc.).
# Without this they fall back to the host TZ — UTC on Render — which
# stamps late-evening Toronto activity onto the next calendar day.
os.environ["TZ"] = "America/Toronto"
if hasattr(time, "tzset"):
    time.tzset()
import re
import zipfile
import io
from ripe_orders import ripe_orders_bp, init_paths as _ripe_init_paths
from retail_orders import retail_orders_bp, init_paths as _retail_init_paths
from suppliers import suppliers_bp
from buyers import buyers_bp
from recipes import recipes_bp
from sales import sales_bp
from finished_goods import finished_goods_bp
from raw_materials import raw_materials_bp
from audit_tools import audit_tools_bp
from production import production_bp
from ledger import ledger_bp
import shopify_importer
import clover_importer

# Foundation layer (IO, path constants, format/SKU/date helpers) lives in
# helpers.py. Imported back so existing references resolve unchanged.
from helpers import (
    ADJUSTMENTS_PATH,
    COMPANY_INFO_PATH,
    DATA_DIR,
    DEFAULT_RM_SECTIONS,
    FORMAT_PREFIX_CANONICAL,
    FORMAT_RE,
    INVENTORY_DIR,
    ORGANIC_CONTACTS_PATH,
    ORGANIC_RUNS_PATH,
    RM_SECTIONS_PATH,
    _DEFAULT_COMPANY_INFO,
    _FILE_LOCKS,
    _FILE_LOCKS_LOCK,
    _FORMAT_SUFFIX_RE,
    _add_contact,
    _aggregate_lots_for_sku,
    _classify_format,
    _get_file_lock,
    _ingredient_section_key,
    _jar_volume_liters,
    _load_company_info,
    _sanitize_ripe_credits,
    _active_ripe_credits,
    _load_json,
    _load_rm_sections,
    _normalize_format,
    _previous_day_coords,
    _prod_date,
    _record_adjustment,
    _runs_using_raw_material,
    _save_json,
    _section_for_ingredient,
    _sku_display,
    _sku_key,
    _strip_format_suffix,
    build_display_name,
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "soma-bone-broth-2026-change-me")
app.register_blueprint(ripe_orders_bp)
app.register_blueprint(retail_orders_bp)
app.register_blueprint(suppliers_bp)
app.register_blueprint(buyers_bp)
app.register_blueprint(recipes_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(finished_goods_bp)
app.register_blueprint(raw_materials_bp)
app.register_blueprint(audit_tools_bp)
app.register_blueprint(production_bp)
app.register_blueprint(ledger_bp)

# Session lifetime — 4 hours. After this the user must log in again.
from datetime import timedelta as _timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(hours=4)
app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS can't read the cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF mitigation

APP_PASSWORD = os.environ.get("APP_PASSWORD", "soma2026")
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "")  # empty = feature disabled
VESSELS = ["K1", "K2", "K3", "115L"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
CHECKLISTS_DIR = os.path.join(DATA_DIR, "checklists")
CCP_MASTER_PATH = os.path.join(DATA_DIR, "ccp_master.json")
RECIPE_ORDER_PATH = os.path.join(DATA_DIR, "recipe_order.json")

for d in [DATA_DIR, SCHEDULES_DIR, PDF_DIR, CHECKLISTS_DIR]:
    os.makedirs(d, exist_ok=True)

DEFAULT_CCP_SECTIONS = [
    {"num": "1", "title": "RAW MATERIAL SELECTION", "items": [
        "Use fresh (walk-in fridge) or frozen bones only",
        "Transfer bones to oven racks - roast immediately, do not leave at room temp",
    ]},
    {"num": "2", "title": "EQUIPMENT CHECK", "items": [
        "Jars: sterilized (dishwasher cycle), undamaged, cleaned same shift/day",
        "Pressure canner: clean, operational, water warmed to 1.5 inch depth, rack in place",
        "New (undamaged) lids for every jar",
    ]},
    {"num": "3", "title": "FIRING / COOKING", "items": [
        "Wash vegetables under running potable water; inspect visually",
        "Heat kettle to rolling boil; reduce to maintain 96-98 C",
        "Log kettle temp 1 hour after process start - must remain above 96 C",
    ]},
    {"num": "4", "title": "CANNING", "items": [
        "Log kettle temp prior to canning - must be above 96 C",
        "Double-filter kettles; transfer to pouring pot; hot-fill jars within 30 min of transfer",
        "Fill jars to 1 inch headspace in neck; periodically verify fill level",
        "CANNER PROCEDURE - Kitchen Lead must supervise; do not leave kitchen during this step",
        "Vent canner 10 min to expel air before closing",
        "Bring to 10 psi; begin timing only once target pressure is reached",
        "Maintain steady pressure for full processing time; restart timer from zero if pressure drops",
        "Canner guidelines completed for all active kettles",
    ]},
    {"num": "5", "title": "POST-CANNING", "items": [
        "Let canner depressurize fully before opening",
        "Remove jars; cool upright on clean towel 12-24 hrs",
        "Check all lids for seal (no flex); separate any unsealed jars for re-processing or discard",
        "Label jars: product name, lot number, best before date",
        "Store in designated area at ambient temperature",
    ]},
]

# ── Auth ───────────────────────────────────────────────────────────────


def login_required(f):
    """Decorator: require an authenticated session (401 JSON for APIs, redirect for pages)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def validate_week_id(week_id):
    """Ensure week_id is a valid YYYY-MM-DD date string."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', week_id):
        return False
    try:
        datetime.strptime(week_id, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def validate_day_idx(day_idx):
    """Ensure day_idx is 0-6."""
    return 0 <= day_idx <= 6


def require_valid_week(f):
    """Decorator: reject requests with invalid week_id."""
    @wraps(f)
    def decorated(*args, **kwargs):
        week_id = kwargs.get("week_id") or (args[0] if args else None)
        if week_id and not validate_week_id(week_id):
            return jsonify({"error": "Invalid week ID"}), 400
        return f(*args, **kwargs)
    return decorated


def require_valid_day(f):
    """Decorator: reject requests with day_idx outside 0-6."""
    @wraps(f)
    def decorated(*args, **kwargs):
        day_idx = kwargs.get("day_idx")
        if day_idx is not None and not validate_day_idx(day_idx):
            return jsonify({"error": "Invalid day index"}), 400
        return f(*args, **kwargs)
    return decorated

# ── Data helpers ───────────────────────────────────────────────────────


def _load_tracking_modes():
    """Read tracking_modes.json directly (used during recipe migration).
    Returns {} if missing.

    Path note: this file moved from data/organic/ to data/inventory/ as part
    of the universal-inventory merge. After the one-time directory rename
    on startup, the file lives under INVENTORY_DIR. INVENTORY_DIR is defined
    later in the module — fall back to the legacy path if not yet bound."""
    inv_dir = globals().get("INVENTORY_DIR") or os.path.join(DATA_DIR, "inventory")
    path = os.path.join(inv_dir, "tracking_modes.json")
    # Legacy fallback for the brief window before the rename runs
    legacy_path = os.path.join(DATA_DIR, "organic", "tracking_modes.json")
    for p in (path, legacy_path):
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


def load_recipes():
    """Load the recipes dict from disk."""
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            recipes = json.load(f)
        # Pass tracking_modes for smart pack conversion.
        tracking_modes = _load_tracking_modes()
        for name, data in recipes.items():
            try:
                migrate_recipe_ingredients(data, tracking_modes)
            except Exception:
                pass
        return recipes
    return {}


def save_recipes(recipes):
    """Persist the recipes dict to disk."""
    with open(RECIPES_PATH, "w") as f:
        json.dump(recipes, f, indent=2)


def load_schedule(week_id):
    """Load a week's schedule JSON (or None)."""
    path = os.path.join(SCHEDULES_DIR, week_id + ".json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def save_schedule(week_id, data):
    """Persist a week's schedule JSON."""
    path = os.path.join(SCHEDULES_DIR, week_id + ".json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def list_schedules():
    """Return a list of all saved weekly schedules."""
    if not os.path.exists(SCHEDULES_DIR):
        return []
    files = sorted([f.replace(".json", "") for f in os.listdir(SCHEDULES_DIR) if f.endswith(".json")], reverse=True)
    return files


def load_checklist(week_id, day_idx):
    """Load a day's checklist JSON (or None)."""
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None


def save_checklist_data(week_id, day_idx, data):
    """Persist a day's checklist JSON."""
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_ccp_master():
    """Load the master CCP document."""
    if os.path.exists(CCP_MASTER_PATH):
        with open(CCP_MASTER_PATH, "r") as f:
            return json.load(f)
    return DEFAULT_CCP_SECTIONS


def save_ccp_master(sections):
    """Persist the master CCP document."""
    with open(CCP_MASTER_PATH, "w") as f:
        json.dump(sections, f, indent=2)


def load_recipe_order():
    """Load the saved recipe display order."""
    if os.path.exists(RECIPE_ORDER_PATH):
        with open(RECIPE_ORDER_PATH, "r") as f:
            return json.load(f)
    return None


def save_recipe_order(order):
    """Persist the recipe display order."""
    with open(RECIPE_ORDER_PATH, "w") as f:
        json.dump(order, f, indent=2)


def get_current_week_id():
    """Return the current week's Monday as a YYYY-MM-DD id."""
    today = datetime.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


# ── Structured Ingredient Helpers ──────────────────────────────────────
VALID_UNITS = ["kg", "g", "L", "ml", "lbs", "Bunch", "Pack", "Adjunct", "per L"]
# Units where the raw recipe amount IS deducted directly (halved for 115L).
# Only "per L" has special math (amount × batch_liters).
INGREDIENT_SECTIONS = ["kettle_overnight", "after_skim", "finishing", "add_to_jar"]

# Unit aliases for parsing (lowercase input -> canonical unit)
UNIT_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gr": "g", "gram": "g", "grams": "g",
    "l": "L", "lt": "L", "ltr": "L", "liter": "L", "liters": "L", "litre": "L", "litres": "L",
    "ml": "ml", "millilitre": "ml", "milliliter": "ml",
    "lb": "lbs", "lbs": "lbs", "pound": "lbs", "pounds": "lbs",
    "bunch": "Bunch", "bunches": "Bunch",
    "pack": "Pack", "packs": "Pack", "packet": "Pack", "packets": "Pack", "bottle": "Pack", "bottles": "Pack",
    "adjunct": "Adjunct", "adjuncts": "Adjunct",
}

# Units that were valid in prior versions but are now retired.
# During migration these either convert to "Pack" (for "each") or get flagged
# for manual review (tbsp/tsp/pinch — no safe auto-conversion to Pack).
RETIRED_UNITS_TO_PACK = {"each"}
RETIRED_UNITS_TO_REVIEW = {"tbsp", "tsp", "pinch"}

# Patterns that signal a "per L" (non-halving) item
PER_L_RE = re.compile(r"\b(per\s*l(?:iter|itre)?|g\s*/\s*l|ml\s*/\s*l)\b", re.IGNORECASE)

# Phrases like "per jar", "per bottle", "per can", "per container" that are
# inventory-counted normally — we strip these from the ingredient name without
# changing the unit interpretation.
PER_CONTAINER_RE = re.compile(
    r"\s*\bper\s+(?:jar|bottle|can|container|pack|bag|unit|piece)s?\b[\s,.\-]*",
    re.IGNORECASE,
)

# Leading amount + unit + name pattern
# Matches: "2.5 kg Celery", "4g Salt", "8 each Lemons", "1.5 L Water"
INGREDIENT_LINE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s+(.+?)\s*$"
)


def parse_ingredient_line(line):
    """Parse a free-text ingredient line into a structured ingredient object.
    Returns dict: {name, amount, unit, process, needs_review}.
    Flags needs_review=True if parsing is ambiguous."""
    original = line.strip()
    if not original:
        return None

    # Strip "per jar / per bottle / per can / per container" phrases anywhere
    # in the line — they don't affect amount/unit interpretation, just clutter the name.
    original = PER_CONTAINER_RE.sub(" ", original).strip()
    original = re.sub(r"\s{2,}", " ", original).rstrip(",").strip()
    if not original:
        return None

    # Strip leading unit + per-L phrase from name so "4g per liter Pink Salt" -> "Pink Salt".
    if PER_L_RE.search(original):
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(.+)$", original)
        if m:
            amount = float(m.group(1))
            rest = m.group(2).strip()
            rest = re.sub(
                r"^\s*(?:g|ml|kg|l)?\s*(?:per\s*l(?:iter|itre)?|g\s*/\s*l|ml\s*/\s*l)\b[\s,.\-]*",
                "",
                rest,
                flags=re.IGNORECASE,
            ).strip()
            return {
                "name": rest or original,
                "amount": amount if amount != int(amount) else int(amount),
                "unit": "per L",
                "process": "",
                "needs_review": not bool(rest),
            }
        return {
            "name": original,
            "amount": 0,
            "unit": "per L",
            "process": "",
            "needs_review": True,
        }

    # e.g. "2.5 kg Celery, diced" -> name="Celery", process="diced"
    process = ""
    body = original
    for sep in [" — ", " – ", " - "]:
        if sep in body:
            parts = body.split(sep, 1)
            body = parts[0].strip()
            process = parts[1].strip()
            break
    if not process and "," in body:
        parts = body.split(",", 1)
        # Only treat as process if the first part looks like "<num> <unit> <name>"
        if re.match(r"^\s*\d+(?:\.\d+)?\s+\S+", parts[0]):
            body = parts[0].strip()
            process = parts[1].strip()

    m = INGREDIENT_LINE_RE.match(body)
    if not m:
        # No leading number — flag for review, keep original text as name
        return {
            "name": original,
            "amount": 0,
            "unit": "",
            "process": "",
            "needs_review": True,
        }

    amount_str = m.group(1)
    unit_token = (m.group(2) or "").strip().lower()
    name = m.group(3).strip()

    try:
        amount = float(amount_str)
    except ValueError:
        amount = 0

    if unit_token and unit_token in UNIT_ALIASES:
        unit = UNIT_ALIASES[unit_token]
        needs_review = False
    elif unit_token:
        # Token is not a recognized unit — fold it back into the name
        name = unit_token + " " + name
        unit = ""
        needs_review = True
    else:
        # Bare number with no unit ("8 Lemons"). Can't safely guess unit
        # in the new scheme (no 'each'). Flag for manual review.
        unit = ""
        needs_review = True

    if amount == int(amount):
        amount = int(amount)

    return {
        "name": name,
        "amount": amount,
        "unit": unit,
        "process": process,
        "needs_review": needs_review,
    }


def format_ingredient(ing):
    """Render a structured ingredient back to a display string."""
    if not isinstance(ing, dict):
        return str(ing)
    amount = ing.get("amount", 0)
    unit = ing.get("unit", "")
    name = ing.get("name", "")
    process = ing.get("process", "")

    parts = []
    if amount:
        if isinstance(amount, float) and amount == int(amount):
            parts.append(str(int(amount)))
        else:
            parts.append(str(amount))
    if unit:
        parts.append(unit)
    if name:
        parts.append(name)

    base = " ".join(parts) if parts else name
    if process:
        return base + " — " + process
    return base


def halve_ingredient(ing):
    """Return a new ingredient with amount halved. 'per L' items are not halved."""
    if not isinstance(ing, dict):
        return ing
    new = dict(ing)
    if new.get("unit") == "per L":
        return new
    amt = new.get("amount", 0)
    try:
        halved = float(amt) / 2
        if halved == int(halved):
            halved = int(halved)
        new["amount"] = halved
    except (ValueError, TypeError):
        pass
    return new


def ingredients_match(raw_item_name, recipe_ing_name):
    """Strict-equivalence ingredient matcher.

    Returns True if the raw material name exactly matches the recipe
    ingredient name after normalizing case and collapsing internal whitespace.

    Distinct ingredients with similar names ARE NOT matched. Examples:
      "Organic Chicken Bones" vs "Chicken Bones"     → False
      "Organic Chicken Bones" vs "Chicken Bones, Organic" → False
      "Beef Bones" vs "Pasture-Raised Beef Bones"    → False

    Tolerated variations (treated as equivalent):
      "Organic Chicken Bones" vs "organic chicken bones"  → True
      "Organic Chicken Bones" vs "ORGANIC CHICKEN BONES"  → True
      "Organic  Chicken  Bones" vs "Organic Chicken Bones" → True (extra spaces)
      " Organic Chicken Bones " vs "Organic Chicken Bones" → True (trim)

    This strictness is intentional: each certification tier ("Organic",
    "Pasture Raised", "Conventional") gets its own ingredient name, and we
    never want a recipe calling for the organic version to silently pull
    from a non-organic lot just because the names overlap word-wise.
    """
    if not raw_item_name or not recipe_ing_name:
        return False
    a = " ".join(raw_item_name.lower().split())
    b = " ".join(recipe_ing_name.lower().split())
    return a == b


def is_untracked_ingredient(name):
    """Returns True for ingredients that should never be tracked as raw material
    inventory or trigger insufficient-stock warnings. Water is treated as
    unlimited — it's a measured recipe ingredient but not an inventoried supply.
    Match: any ingredient name containing the word 'water' (case-insensitive)."""
    if not name:
        return False
    return "water" in name.lower()


def is_structured_ingredient(item):
    """Check if an item is already in structured object form."""
    return isinstance(item, dict) and "name" in item and "amount" in item


# Confirmed kitchen-facing dosing units for known 'per L' ingredients (keyed by
# normalized name). These take priority over inventory inference because an
# ingredient's dosing unit can differ from how it's stocked (honey is dosed by
# weight but may be inventoried by volume) and because the strict name matcher
# would otherwise fall back to 'g' when a recipe name doesn't exactly equal the
# inventory name (e.g. "Lemon Juice" vs "Organic Lemon Juice"). Confirmed with
# the operator 2026-06-04. Anything not listed here still infers from inventory.
_PER_L_UNIT_OVERRIDES = {
    "grey salt": "g",
    "pink salt": "g",
    "ginger juice": "ml",
    "lemon juice": "ml",
    "honey": "g",
}


def _infer_per_l_unit(ingredient_name, raw_materials):
    """Infer the dosing unit ('g' or 'ml') for a 'per L' recipe ingredient.

    A confirmed override (_PER_L_UNIT_OVERRIDES) wins first; otherwise look at
    the matching raw-material lot's unit: mass units (g/kg/lbs) -> 'g', volume
    units (ml/L) -> 'ml'. Defaults to 'g' (the historical assumption) when no
    lot matches by name."""
    key = " ".join((ingredient_name or "").lower().split())
    if key in _PER_L_UNIT_OVERRIDES:
        return _PER_L_UNIT_OVERRIDES[key]
    for mat in raw_materials or []:
        if not isinstance(mat, dict):
            continue
        if ingredients_match(mat.get("item", ""), ingredient_name):
            u = (mat.get("unit") or "").strip().lower()
            if u in ("ml", "l"):
                return "ml"
            if u in ("g", "kg", "lbs"):
                return "g"
    return "g"


def _attach_per_l_units(recipe_data, raw_materials=None):
    """Annotate each 'per L' ingredient in a recipe with a 'per_l_unit' field
    ('g' or 'ml') inferred from inventory, so recipe cards and the daily
    production page can show e.g. '3 g per L'. Display-only; not persisted on
    save (the recipe editor rebuilds ingredients from its own fields)."""
    if not isinstance(recipe_data, dict):
        return recipe_data
    if raw_materials is None:
        raw_materials = _load_json(ORGANIC_RAW_PATH, [])
    for section in INGREDIENT_SECTIONS:
        for item in recipe_data.get(section, []) or []:
            if isinstance(item, dict) and (item.get("unit") or "").strip() == "per L":
                item["per_l_unit"] = _infer_per_l_unit(item.get("name", ""), raw_materials)
    return recipe_data


def _smart_upgrade_ingredient(item, tracking_modes):
    """Upgrade an already-structured ingredient to the new unit scheme.

    Returns (new_item, changed).

    Rules:
      1. If name matches a tracking_modes pack entry with matching pack_label,
         convert to {amount: 1, unit: 'Pack', process: '<original>'}.
      2. If unit is 'each', force to 'Pack' (same deduction, flag for review).
      3. If unit is tbsp/tsp/pinch, clear unit and flag for review.
      4. Otherwise leave alone.
    """
    if not is_structured_ingredient(item):
        return item, False

    name = (item.get("name") or "").strip()
    name_key = name.lower()
    unit = (item.get("unit") or "").strip()
    amount = item.get("amount", 0)
    try:
        amount_f = float(amount or 0)
    except (ValueError, TypeError):
        amount_f = 0

    # Rule 1: smart pack conversion via tracking_modes
    tm = tracking_modes.get(name_key) if tracking_modes else None
    if tm and tm.get("mode") == "pack" and unit not in ("", "Pack", "Adjunct", "Bunch", "per L"):
        pack_label = (tm.get("pack_label") or "").strip()
        line_label = _format_pack_label(
            amount_f if amount_f == int(amount_f) else amount_f,
            unit,
        ) if amount_f > 0 and unit else ""
        if pack_label and line_label and pack_label.lower() == line_label.lower():
            new = dict(item)
            new["amount"] = 1
            new["unit"] = "Pack"
            existing_process = (new.get("process") or "").strip()
            # Preserve original pack size as process note
            new["process"] = existing_process if existing_process else pack_label
            new["needs_review"] = False
            return new, True
        else:
            # Pack-tracked ingredient but label mismatch — flag for review
            new = dict(item)
            new["needs_review"] = True
            return new, True

    # Rule 2: 'each' → Pack, flagged for review
    if unit in RETIRED_UNITS_TO_PACK:
        new = dict(item)
        new["unit"] = "Pack"
        new["needs_review"] = True
        return new, True

    # Rule 3: tbsp/tsp/pinch → clear unit, flag for review
    if unit in RETIRED_UNITS_TO_REVIEW:
        new = dict(item)
        # Preserve the retired unit in process so user can decide
        retired_note = f"was {unit}"
        existing_process = (new.get("process") or "").strip()
        new["process"] = existing_process + (" — " if existing_process else "") + retired_note
        new["unit"] = ""
        new["needs_review"] = True
        return new, True

    # No change
    return item, False


def migrate_recipe_ingredients(recipe_data, tracking_modes=None):
    """In-place: convert string-format ingredient lines to structured objects,
    and upgrade structured items per the new unit scheme.
    Returns True if any changes were made."""
    if not isinstance(recipe_data, dict):
        return False
    if tracking_modes is None:
        tracking_modes = {}
    changed = False
    for section in INGREDIENT_SECTIONS:
        items = recipe_data.get(section, [])
        if not isinstance(items, list):
            continue
        new_items = []
        for item in items:
            if is_structured_ingredient(item):
                upgraded, item_changed = _smart_upgrade_ingredient(item, tracking_modes)
                new_items.append(upgraded)
                if item_changed:
                    changed = True
            elif isinstance(item, str):
                parsed = parse_ingredient_line(item)
                if parsed:
                    upgraded, _ = _smart_upgrade_ingredient(parsed, tracking_modes)
                    new_items.append(upgraded)
                    changed = True
            elif isinstance(item, dict):
                # Dict without required fields — coerce
                new_items.append({
                    "name": item.get("name", str(item)),
                    "amount": item.get("amount", 0),
                    "unit": item.get("unit", ""),
                    "process": item.get("process", ""),
                    "needs_review": True,
                })
                changed = True
        recipe_data[section] = new_items
    return changed

# ── Recipe parser ──────────────────────────────────────────────────────
# Canonical prefix casing for known format families.
# Only formats that actually appear as product SKUs belong here.
# SS = Shelf-Stable, FZ = Frozen, BB = Back Bar label variant.

# Any <letters>[sep]<number>ML suffix — case-insensitive.
# Separator can be nothing, a dash, or whitespace.

# Suffix regex used when stripping a trailing format from a recipe name.
# Matches "<separator><letters><separator><digits>ML" at end of string.


def _detect_format_in_text(text):
    """Return the canonical format found in text, or '' if none."""
    if not text:
        return ""
    m = FORMAT_RE.search(text)
    if not m:
        return ""
    return _normalize_format(m.group(0))


def parse_recipe_pdf_text(text):
    """Parse extracted recipe-PDF text into a recipe dict (or None)."""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None

    recipe = {
        "yield": None, "format": "", "brand": "", "certification": "",
        "special_instructions": [], "kettle_overnight": [],
        "after_skim": [], "finishing": [], "add_to_jar": [],
    }
    name = ""

    # First pass: extract labelled fields
    header_lines_used = set()
    for i, line in enumerate(lines):
        ll = line.lower().strip()
        if ll.startswith("brand name:"):
            recipe["brand"] = line.split(":", 1)[1].strip()
            header_lines_used.add(i)
        elif ll.startswith("recipe name:"):
            name = line.split(":", 1)[1].strip()
            header_lines_used.add(i)
        elif ll.startswith("certification:"):
            recipe["certification"] = line.split(":", 1)[1].strip()
            header_lines_used.add(i)
        elif ll.startswith("format:"):
            fmt = line.split(":", 1)[1].strip()
            recipe["format"] = _normalize_format(fmt)
            header_lines_used.add(i)
        elif "target yield" in ll:
            m = re.search(r"(\d+)", line)
            if m:
                recipe["yield"] = int(m.group(1))
            header_lines_used.add(i)

    # Fallback: if no labelled fields, use first line as name
    if not name:
        name = lines[0]
        header_lines_used.add(0)
        detected = _detect_format_in_text(name)
        if detected:
            recipe["format"] = detected

    if recipe["yield"] is None:
        recipe["yield"] = 190 if "FZ" in recipe["format"] else 150

    if recipe["format"] and not name.upper().endswith(recipe["format"].upper()):
        name = name + " " + recipe["format"]

    current_section = None
    in_special = False

    def _append_ing(section, raw_line):
        parsed = parse_ingredient_line(raw_line)
        if parsed:
            recipe[section].append(parsed)

    for i, line in enumerate(lines):
        if i in header_lines_used:
            continue
        ll = line.lower().strip()
        if "target yield" in ll:
            continue
        if ll == "special instructions:" or ll.startswith("special instructions"):
            in_special = True
            continue
        if "add to kettle overnight" in ll or ll == "start:":
            in_special = False
            current_section = "kettle_overnight"
            continue
        if "add directly to kettle after skim" in ll or "add to kettle after skim" in ll or ll == "finish:":
            current_section = "after_skim"
            continue
        if ll.startswith("water") and ("removing solids" in ll or "top kettle" in ll):
            current_section = "finishing"
            _append_ing("finishing", line)
            continue
        if "add to jar" in ll or "add to container" in ll:
            current_section = "add_to_jar"
            continue
        if any(ll.startswith(p) for p in ["no salt", "g per liter", "ml per liter"]) or "per liter" in ll or "per litre" in ll:
            if current_section != "finishing":
                current_section = "finishing"
            _append_ing("finishing", line)
            continue
        if in_special:
            recipe["special_instructions"].append(line)
            continue
        if current_section and current_section in recipe:
            _append_ing(current_section, line)

    return {"name": name, "data": recipe}

# ── Auth routes ────────────────────────────────────────────────────────


@app.route("/login")
def login_page():
    """Render the login page."""
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def login():
    """Handle login form submission and set the session."""
    data = request.json or {}
    if data.get("password") == APP_PASSWORD:
        session.permanent = True   # enables PERMANENT_SESSION_LIFETIME
        session["authenticated"] = True
        session["logged_in_at"] = datetime.now().isoformat()
        return jsonify({"success": True})
    return jsonify({"error": "Invalid password"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    """Clear the session and log out."""
    session.clear()
    return jsonify({"success": True})

# ── PWA manifest ──────────────────────────────────────────────────────


@app.route("/manifest.json")
def manifest():
    """Serve the PWA manifest.json."""
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")

# ── Page routes ────────────────────────────────────────────────────────


@app.route("/")
@login_required
def dashboard():
    """Render the dashboard."""
    return render_template("dashboard.html")


@app.route("/contacts")
@login_required
def contacts_page():
    """Render the contacts page."""
    return render_template("contacts.html")


@app.route("/company-settings")
@login_required
def company_settings_page():
    """Render the company settings page."""
    return render_template("company_settings.html")


@app.route("/api/verify-manager", methods=["POST"])
@login_required
def verify_manager():
    """Verify manager password for unlocking the Resources section.
    Returns {ok: true} if correct, {ok: false} if wrong or not set.
    """
    if not MANAGER_PASSWORD:
        return jsonify({"ok": False, "reason": "MANAGER_PASSWORD not configured"})
    data = request.get_json() or {}
    submitted = (data.get("password") or "").strip()
    import hmac as _hmac
    ok = bool(submitted) and _hmac.compare_digest(submitted.encode(), MANAGER_PASSWORD.encode())
    return jsonify({"ok": ok})


@app.route("/api/admin/backup", methods=["POST"])
@login_required
def admin_backup():
    """Return a zip of the entire DATA_DIR as a download.
    Requires manager password in JSON body: {"password": "..."}.
    """
    if not MANAGER_PASSWORD:
        return jsonify({"error": "MANAGER_PASSWORD not configured"}), 403
    payload = request.get_json() or {}
    submitted = (payload.get("password") or "").strip()
    import hmac as _hmac
    if not (submitted and _hmac.compare_digest(submitted.encode(), MANAGER_PASSWORD.encode())):
        return jsonify({"error": "Invalid manager password"}), 401

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(DATA_DIR):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, DATA_DIR)
                zf.write(full, rel)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"soma-backup-{stamp}.zip",
    )


@app.route("/certifications")
@login_required
def certifications_page():
    """Organic & compliance document storage page."""
    return render_template("certifications.html")


@app.route("/api/certifications", methods=["GET"])
@login_required
def list_certifications():
    """GET - list uploaded certification documents."""
    cert_dir = os.path.join(DATA_DIR, "certifications")
    os.makedirs(cert_dir, exist_ok=True)
    meta_path = os.path.join(cert_dir, "meta.json")
    meta = _load_json(meta_path, [])
    return jsonify(meta)


@app.route("/api/certifications/upload", methods=["POST"])
@login_required
def upload_certification():
    """POST - upload a certification document."""
    import werkzeug.utils
    cert_dir = os.path.join(DATA_DIR, "certifications")
    os.makedirs(cert_dir, exist_ok=True)
    meta_path = os.path.join(cert_dir, "meta.json")
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    label = (request.form.get("label") or "").strip()
    category = (request.form.get("category") or "General").strip()
    safe_name = werkzeug.utils.secure_filename(f.filename)
    file_id = datetime.now().strftime("%Y%m%d%H%M%S%f") + "_" + safe_name
    f.save(os.path.join(cert_dir, file_id))
    meta = _load_json(meta_path, [])
    meta.append({
        "id": file_id,
        "filename": safe_name,
        "label": label or safe_name,
        "category": category,
        "uploaded_at": datetime.now().isoformat(),
        "size": os.path.getsize(os.path.join(cert_dir, file_id)),
    })
    _save_json(meta_path, meta)
    return jsonify({"ok": True, "id": file_id})


@app.route("/api/certifications/<file_id>", methods=["DELETE"])
@login_required
def delete_certification(file_id):
    """DELETE - remove a certification document."""
    cert_dir = os.path.join(DATA_DIR, "certifications")
    meta_path = os.path.join(cert_dir, "meta.json")
    meta = _load_json(meta_path, [])
    entry = next((m for m in meta if m["id"] == file_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    try:
        os.remove(os.path.join(cert_dir, file_id))
    except FileNotFoundError:
        pass
    meta = [m for m in meta if m["id"] != file_id]
    _save_json(meta_path, meta)
    return jsonify({"ok": True})


@app.route("/api/certifications/<file_id>/download")
@login_required
def download_certification(file_id):
    """GET - download a certification document."""
    from flask import send_file
    cert_dir = os.path.join(DATA_DIR, "certifications")
    meta_path = os.path.join(cert_dir, "meta.json")
    meta = _load_json(meta_path, [])
    entry = next((m for m in meta if m["id"] == file_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    return send_file(
        os.path.join(cert_dir, file_id),
        download_name=entry["filename"],
        as_attachment=True,
    )


@app.route("/analytics")
@login_required
def analytics_page():
    """Combined Production & Sales Analytics page.
    Tabs: Production Tracker + Sales by Buyer.
    """
    buyers = _load_buyers()
    buyer_names = [b["name"] for b in buyers]
    return render_template("analytics.html", buyer_names=buyer_names)


@app.route("/analytics/buyer/<path:buyer_name>")
@login_required
def buyer_analytics_page(buyer_name):
    """Dedicated analytics page for a single buyer."""
    buyers = _load_buyers()
    buyer = next((b for b in buyers if b["name"].lower() == buyer_name.lower()), None)
    display_name = buyer["name"] if buyer else buyer_name
    return render_template("buyer_analytics.html",
                           buyer_name=display_name,
                           buyer_names=[b["name"] for b in buyers])


@app.route("/api/analytics/buyer/<path:buyer_name>")
@login_required
def api_buyer_analytics(buyer_name):
    """Full analytics for one buyer: totals, by-month, by-sku, recent orders."""
    from datetime import datetime as _dt, timedelta as _td
    from collections import defaultdict

    sales = _load_json(ORGANIC_SALES_PATH, [])

    # Resolve each sale's raw buyer / location_name using buyers.json so that
    # historical rows entered as "Parent - Location" (when the location feature
    # didn't exist yet) roll up under their parent here too. The resolver also
    # injects the inferred location onto each matched sale copy so the
    # downstream by_location aggregation works without further changes.
    resolve = _buyer_resolver(_load_buyers())
    buyer_sales = []
    for s in sales:
        canonical_buyer, canonical_loc = resolve(s.get("buyer"), s.get("location_name"))
        if canonical_buyer.lower() == buyer_name.lower():
            s_copy = dict(s)
            s_copy["location_name"] = canonical_loc
            buyer_sales.append(s_copy)

    def _revenue(s):
        lt = s.get("line_total")
        if lt is not None:
            return float(lt)
        price = float(s.get("unit_price") or 0)
        return float(s.get("quantity") or 0) * price

    def _cases(s):
        return int(s.get("quantity") or 0) // 12

    # ── Totals ────────────────────────────────────────────────────────────────
    total_revenue = sum(_revenue(s) for s in buyer_sales)
    total_cases   = sum(_cases(s) for s in buyer_sales)
    total_units   = sum(int(s.get("quantity") or 0) for s in buyer_sales)
    order_ids     = {s.get("order_id") or s.get("id") for s in buyer_sales}
    total_orders  = len(order_ids)
    avg_order_rev = round(total_revenue / total_orders, 2) if total_orders else 0
    avg_order_cases = round(total_cases / total_orders, 1) if total_orders else 0

    # ── By month ──────────────────────────────────────────────────────────────
    by_month = defaultdict(lambda: {"revenue": 0.0, "cases": 0, "units": 0, "orders": set()})
    for s in buyer_sales:
        d = (s.get("sale_date") or "")[:7]  # YYYY-MM
        if not d:
            continue
        by_month[d]["revenue"] += _revenue(s)
        by_month[d]["cases"]   += _cases(s)
        by_month[d]["units"]   += int(s.get("quantity") or 0)
        by_month[d]["orders"].add(s.get("order_id") or s.get("id"))

    months_sorted = sorted(by_month.keys())
    monthly = [
        {
            "month":   m,
            "label":   _dt.strptime(m, "%Y-%m").strftime("%b %Y"),
            "revenue": round(by_month[m]["revenue"], 2),
            "cases":   by_month[m]["cases"],
            "units":   by_month[m]["units"],
            "orders":  len(by_month[m]["orders"]),
        }
        for m in months_sorted
    ]

    # ── By year ───────────────────────────────────────────────────────────────
    by_year = defaultdict(lambda: {"revenue": 0.0, "cases": 0, "orders": set()})
    for s in buyer_sales:
        y = (s.get("sale_date") or "")[:4]
        if not y:
            continue
        by_year[y]["revenue"] += _revenue(s)
        by_year[y]["cases"]   += _cases(s)
        by_year[y]["orders"].add(s.get("order_id") or s.get("id"))
    yearly = [
        {"year": y, "revenue": round(by_year[y]["revenue"], 2),
         "cases": by_year[y]["cases"], "orders": len(by_year[y]["orders"])}
        for y in sorted(by_year.keys())
    ]

    # ── By SKU ────────────────────────────────────────────────────────────────
    by_sku = defaultdict(lambda: {"recipe": "", "format": "", "units": 0,
                                   "cases": 0, "revenue": 0.0,
                                   "months_cases":   defaultdict(int),
                                   "months_revenue": defaultdict(float)})
    for s in buyer_sales:
        sk = s.get("sku_key") or s.get("recipe") or "Unknown"
        by_sku[sk]["recipe"]  = s.get("recipe", "")
        by_sku[sk]["format"]  = s.get("format", "")
        by_sku[sk]["units"]  += int(s.get("quantity") or 0)
        by_sku[sk]["cases"]  += _cases(s)
        by_sku[sk]["revenue"]+= _revenue(s)
        m = (s.get("sale_date") or "")[:7]
        if m:
            by_sku[sk]["months_cases"][m]   += _cases(s)
            by_sku[sk]["months_revenue"][m] += _revenue(s)

    skus = sorted(
        [{"sku": k,
          **{kk: vv for kk, vv in v.items() if kk not in ("months_cases", "months_revenue")},
          "revenue":         round(v["revenue"], 2),
          "monthly_cases":   dict(v["months_cases"]),
          "monthly_revenue": {m: round(r, 2) for m, r in v["months_revenue"].items()}}
         for k, v in by_sku.items()],
        key=lambda x: -x["revenue"]
    )

    # ── By location ───────────────────────────────────────────────────────────
    # Each sale carries an optional `location_name` (set at sale-add time when
    # the buyer has a `locations[]` array; also populated from Ripe orders'
    # delivery_label). Aggregating by location lets multi-location buyers like
    # Nature's Emporium see per-store performance.
    by_location = defaultdict(lambda: {"revenue": 0.0, "cases": 0, "units": 0,
                                       "orders": set(),
                                       "months": defaultdict(lambda: {"revenue": 0.0, "cases": 0, "units": 0}),
                                       "by_sku": defaultdict(lambda: {"recipe": "", "format": "", "units": 0, "cases": 0, "revenue": 0.0})})
    for s in buyer_sales:
        loc = (s.get("location_name") or "").strip() or "(No location)"
        sk  = s.get("sku_key") or s.get("recipe") or "Unknown"
        rev = _revenue(s)
        cs  = _cases(s)
        qty = int(s.get("quantity") or 0)
        by_location[loc]["revenue"] += rev
        by_location[loc]["cases"]   += cs
        by_location[loc]["units"]   += qty
        by_location[loc]["orders"].add(s.get("order_id") or s.get("id"))
        m = (s.get("sale_date") or "")[:7]
        if m:
            by_location[loc]["months"][m]["revenue"] += rev
            by_location[loc]["months"][m]["cases"]   += cs
            by_location[loc]["months"][m]["units"]   += qty
        by_location[loc]["by_sku"][sk]["recipe"]  = s.get("recipe", "")
        by_location[loc]["by_sku"][sk]["format"]  = s.get("format", "")
        by_location[loc]["by_sku"][sk]["units"]  += qty
        by_location[loc]["by_sku"][sk]["cases"]  += cs
        by_location[loc]["by_sku"][sk]["revenue"]+= rev

    locations = sorted(
        [{"name":    k,
          "revenue": round(v["revenue"], 2),
          "cases":   v["cases"],
          "units":   v["units"],
          "orders":  len(v["orders"]),
          "monthly": {m: {"revenue": round(d["revenue"], 2),
                          "cases":   d["cases"],
                          "units":   d["units"]}
                      for m, d in v["months"].items()},
          "by_sku":  sorted(
                        [{"sku": sk, "recipe": d["recipe"], "format": d["format"],
                          "units": d["units"], "cases": d["cases"], "revenue": round(d["revenue"], 2)}
                         for sk, d in v["by_sku"].items()],
                        key=lambda x: -x["revenue"]
                     )}
         for k, v in by_location.items()],
        key=lambda x: -x["revenue"],
    )

    # ── YoY comparison ────────────────────────────────────────────────────────
    now = _dt.now().date()
    this_year  = str(now.year)
    last_year  = str(now.year - 1)
    ty_rev = by_year.get(this_year, {}).get("revenue", 0.0)
    ly_rev = by_year.get(last_year, {}).get("revenue", 0.0)
    yoy_growth = round(((ty_rev - ly_rev) / ly_rev * 100), 1) if ly_rev else None

    # Month-over-prior-year: same calendar months
    this_month = now.strftime("%Y-%m")
    prior_yr_month = (now.replace(year=now.year-1)).strftime("%Y-%m")
    tm_rev = by_month.get(this_month, {}).get("revenue", 0.0)
    pm_rev = by_month.get(prior_yr_month, {}).get("revenue", 0.0)

    # Predictive: annualise YTD using months elapsed
    months_elapsed = now.month
    ytd_rev = sum(
        by_month.get(f"{this_year}-{str(m).zfill(2)}", {}).get("revenue", 0.0)
        for m in range(1, now.month + 1)
    )
    predicted_annual = round((ytd_rev / months_elapsed * 12), 2) if months_elapsed and ytd_rev else None

    # Sufficient data flag: need at least 3 months of sales
    has_sufficient_data = len(months_sorted) >= 3

    return jsonify({
        "buyer": buyer_name,
        "totals": {
            "revenue":          round(total_revenue, 2),
            "cases":            total_cases,
            "units":            total_units,
            "orders":           total_orders,
            "avg_order_revenue": avg_order_rev,
            "avg_order_cases":   avg_order_cases,
        },
        "monthly":   monthly,
        "yearly":    yearly,
        "by_sku":    skus,
        "locations": locations,
        "predictive": {
            "has_sufficient_data": has_sufficient_data,
            "yoy_growth_pct":      yoy_growth,
            "this_year_revenue":   round(ty_rev, 2),
            "last_year_revenue":   round(ly_rev, 2),
            "this_month_revenue":  round(tm_rev, 2),
            "prior_year_month_revenue": round(pm_rev, 2),
            "predicted_annual_revenue": predicted_annual,
            "months_of_data":      len(months_sorted),
        },
    })


@app.route("/api/analytics/backfill-sale-prices", methods=["POST"])
@login_required
def backfill_sale_prices():
    """One-time backfill: add unit_price and line_total to historical sale
    records that were created before pricing was stored on sale records.
    Looks up the current price from the buyer catalogue in buyers.json.
    Returns a summary of how many records were updated.
    """
    sales   = _load_json(ORGANIC_SALES_PATH, [])
    buyers  = _load_buyers()

    price_map = {}
    for b in buyers:
        bname = (b.get("name") or "").strip().lower()
        for sku in (b.get("skus") or []):
            sk = sku.get("sku_key", "")
            price = sku.get("price")
            if sk and price is not None:
                price_map[(bname, sk)] = round(float(price), 2)

    updated = 0
    no_price = 0
    for sale in sales:
        if sale.get("unit_price") is not None:
            continue  # already has a price
        buyer_key = (sale.get("buyer") or "").strip().lower()
        sku_key   = sale.get("sku_key", "")
        price = price_map.get((buyer_key, sku_key))
        if price is None:
            no_price += 1
            continue
        qty = int(sale.get("quantity") or 0)
        sale["unit_price"] = price
        sale["line_total"]  = round(price * qty, 2)
        if "cases" not in sale:
            sale["cases"] = qty // 12
        updated += 1

    if updated:
        _save_json(ORGANIC_SALES_PATH, sales)

    return jsonify({
        "ok": True,
        "updated": updated,
        "skipped_no_price": no_price,
        "message": f"Updated {updated} records. {no_price} records had no matching buyer price and were left unchanged.",
    })


@app.route("/api/analytics/sales-by-buyer", methods=["GET"])
@login_required
def api_sales_by_buyer():
    """Aggregate sales.json by buyer, date range, and SKU."""
    sales = _load_json(ORGANIC_SALES_PATH, [])
    buyers_q = request.args.get("buyer", "").strip()
    period   = request.args.get("period", "all")  # all / ytd / 90d / 30d

    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now().date()
    if period == "30d":
        cutoff = now - _td(days=30)
    elif period == "90d":
        cutoff = now - _td(days=90)
    elif period == "ytd":
        cutoff = now.replace(month=1, day=1)
    else:
        cutoff = None

    # Roll up sales whose raw buyer is a location-suffixed form (e.g.
    # "Nature's Emporium - Newmarket") under their parent in buyers.json.
    buyers_list = _load_buyers()
    resolve = _buyer_resolver(buyers_list)
    # Map each buyer's canonical name -> its registered location names, so the
    # sales cards can list the company's locations (or nothing if it has none).
    loc_map = {}
    for _b in buyers_list:
        _nm = (_b.get("name") or "").strip()
        if _nm:
            loc_map[_nm] = [(_l.get("name") or "").strip()
                            for _l in (_b.get("locations") or [])
                            if (_l.get("name") or "").strip()]

    # Aggregate
    by_buyer = {}
    for sale in sales:
        raw_buyer = (sale.get("buyer") or "Unknown").strip()
        buyer, _resolved_loc = resolve(raw_buyer, sale.get("location_name"))
        if not buyer:
            buyer = "Unknown"
        if buyers_q and buyer.lower() != buyers_q.lower():
            continue
        sale_date = (sale.get("sale_date") or "")[:10]
        if cutoff and sale_date and sale_date < cutoff.isoformat():
            continue
        if buyer not in by_buyer:
            by_buyer[buyer] = {"buyer": buyer, "orders": set(), "units": 0, "revenue": 0.0, "by_sku": {}}
        qty   = int(sale.get("quantity") or 0)
        # unit_price may be None for pre-catalogue records — fall back to 0
        price = float(sale.get("unit_price") or sale.get("price") or sale.get("unit_selling_price") or 0)
        # Use stored line_total if available (more accurate), else compute from price * qty
        line_total_stored = sale.get("line_total")
        sku   = sale.get("sku_key") or sale.get("recipe") or "Unknown"
        order_id = sale.get("order_id") or sale.get("id")
        by_buyer[buyer]["orders"].add(order_id)
        by_buyer[buyer]["units"]   += qty
        revenue_this = float(line_total_stored) if line_total_stored is not None else qty * price
        by_buyer[buyer]["revenue"] += revenue_this
        if sku not in by_buyer[buyer]["by_sku"]:
            by_buyer[buyer]["by_sku"][sku] = {"sku": sku,
                "recipe": sale.get("recipe",""), "format": sale.get("format",""),
                "units": 0, "revenue": 0.0}
        by_buyer[buyer]["by_sku"][sku]["units"]   += qty
        by_buyer[buyer]["by_sku"][sku]["revenue"] += revenue_this

    result = []
    for b_data in sorted(by_buyer.values(), key=lambda x: -x["revenue"]):
        skus = sorted(b_data["by_sku"].values(), key=lambda s: -s["units"])
        result.append({
            "buyer":   b_data["buyer"],
            "orders":  len(b_data["orders"]),
            "units":   b_data["units"],
            "cases":   b_data["units"] // 12,
            "revenue": round(b_data["revenue"], 2),
            "locations": loc_map.get(b_data["buyer"], []),
            "by_sku":  [{**s, "revenue": round(s["revenue"],2)} for s in skus],
        })
    return jsonify({"buyers": result, "period": period})


@app.route("/buyers/<bid>/edit")
@login_required
def buyer_edit_page(bid):
    """Render the buyer edit page."""
    buyers = _load_buyers()
    buyer = next((b for b in buyers if b["id"] == bid), None)
    if not buyer:
        return "Buyer not found", 404

    # Group catalog by Brand → Format (SS → FZ → BB → Other) → Recipe name
    flat = _all_sku_catalog()
    FORMAT_ORDER = ["SS", "FZ", "BB"]
    FORMAT_LABELS = {"SS": "Shelf Stable", "FZ": "Frozen", "BB": "Back Bar"}

    brand_fmt_dict = {}  # {brand: {fmt_prefix: [skus]}}
    for sku in flat:
        brand = (sku.get("brand") or "Other").strip() or "Other"
        fmt   = _normalize_format(sku.get("format") or "")
        prefix = fmt[:2].upper() if fmt else "Other"
        prefix = prefix if prefix in FORMAT_ORDER else "Other"
        if brand not in brand_fmt_dict:
            brand_fmt_dict[brand] = {}
        if prefix not in brand_fmt_dict[brand]:
            brand_fmt_dict[brand][prefix] = []
        brand_fmt_dict[brand][prefix].append(sku)

    sku_catalog = []
    for brand in sorted(brand_fmt_dict.keys()):
        for prefix in FORMAT_ORDER + ["Other"]:
            skus_in = brand_fmt_dict[brand].get(prefix)
            if not skus_in:
                continue
            skus_sorted = sorted(skus_in, key=lambda s: s.get("recipe","").lower())
            sku_catalog.append({
                "format_prefix": prefix,
                "format_label":  FORMAT_LABELS.get(prefix, "Other"),
                "brand":         brand,
                "skus":          skus_sorted,
            })

    return render_template("buyer_edit.html", buyer=buyer, sku_catalog=sku_catalog)


@app.route("/ccp-master")
@login_required
def ccp_master_page():
    """Render the master CCP page."""
    return render_template("master_ccp.html")


# ── Recipe API ─────────────────────────────────────────────────────────
# Recipe routes now live in recipes.py (recipes_bp). The PHOTOS_DIR setup and
# the serve_photo route below stay here because they're shared infrastructure
# (recipes.py reaches PHOTOS_DIR via app.PHOTOS_DIR).
# Photo upload
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)


@app.route("/api/photos/<filename>")
@login_required
def serve_photo(filename):
    """Serve a recipe photo from PHOTOS_DIR."""
    return send_from_directory(PHOTOS_DIR, filename)

# ── Schedule API ───────────────────────────────────────────────────────


# ── Generate PDFs ──────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
@login_required
def generate_pdfs():
    """POST - generate the weekly schedule + daily package PDFs."""
    data = request.json or {}
    week_id = data.get("week_id", get_current_week_id())
    schedule = data.get("schedule", {})
    notes = data.get("notes", "")
    daily_notes = data.get("daily_notes", {})

    save_schedule(week_id, {"schedule": schedule, "notes": notes, "daily_notes": daily_notes})

    try:
        week_start = datetime.strptime(week_id, "%Y-%m-%d")
        recipes = load_recipes()
        ccp = load_ccp_master()

        logo_path = os.path.join(app.static_folder, "logo.jpg")
        if not os.path.exists(logo_path):
            logo_path = None

        week_pdf_dir = os.path.join(PDF_DIR, week_id)
        os.makedirs(week_pdf_dir, exist_ok=True)

        generated = []

        filename = "Weekly_Schedule.pdf"
        filepath = os.path.join(week_pdf_dir, filename)
        generate_weekly_schedule_pdf(filepath, week_start, schedule, recipes, notes, logo_path)
        generated.append(filename)

        for day_idx in range(7):
            day_key = str(day_idx)
            day_schedule = schedule.get(day_key, {})
            has_production = any(day_schedule.get(v) for v in VESSELS)
            if not has_production:
                continue

            date = week_start + timedelta(days=day_idx)
            filename = DAYS[day_idx] + "_Production.pdf"
            filepath = os.path.join(week_pdf_dir, filename)
            generate_daily_package_pdf(filepath, date, day_schedule, recipes, logo_path)
            generated.append(filename)

        return jsonify({"success": True, "files": generated, "week_id": week_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        try:
            _check_organic_schedule(week_id, schedule)
        except Exception:
            pass

# ── PDF downloads ──────────────────────────────────────────────────────


@app.route("/api/pdf/<week_id>/<filename>", methods=["GET"])
@login_required
@require_valid_week
def download_pdf(week_id, filename):
    """GET - download a previously generated PDF."""
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    return send_from_directory(week_pdf_dir, filename, as_attachment=True)


@app.route("/api/pdfs/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def list_pdfs(week_id):
    """GET - list generated PDFs for a week."""
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify([])
    files = sorted(os.listdir(week_pdf_dir))
    return jsonify([f for f in files if f.endswith(".pdf")])


@app.route("/api/pdfs/<week_id>/download-all", methods=["GET"])
@login_required
@require_valid_week
def download_all_pdfs(week_id):
    """GET - download all of a week's PDFs as a zip."""
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify({"error": "No PDFs found"}), 404
    pdf_files = [f for f in sorted(os.listdir(week_pdf_dir)) if f.endswith(".pdf")]
    if not pdf_files:
        return jsonify({"error": "No PDFs found"}), 404
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in pdf_files:
            zf.write(os.path.join(week_pdf_dir, f), f)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"Soma_Production_{week_id}.zip")

# ── 115L Halving Helper ──────────────────────────────────────────────


def _halve_for_115L(recipe_data):
    """Return a copy of recipe_data with quantities halved for the 115L vessel.
    Structured ingredients with unit='per L' are NOT halved.
    """
    import copy
    halved = copy.deepcopy(recipe_data)

    if halved.get("yield"):
        try:
            halved["yield"] = round(int(halved["yield"]) / 2)
        except (ValueError, TypeError):
            pass

    # Migrate first (safety: in case a legacy recipe slipped through)
    migrate_recipe_ingredients(halved)

    for section in INGREDIENT_SECTIONS:
        items = halved.get(section, [])
        if isinstance(items, list):
            halved[section] = [halve_ingredient(it) for it in items]

    halved["_halved"] = True
    return halved

# ── Daily Production API ──────────────────────────────────────────────


# ── Label Generation ──────────────────────────────────────────────────
@app.route("/api/label", methods=["POST"])
@login_required
def generate_label():
    """POST /api/label - generate a product label PDF."""
    data = request.json or {}
    brand_name = data.get("brand_name", "")
    recipe_name = data.get("recipe_name", "")
    recipe_format = data.get("recipe_format", "")
    lot = data.get("lot", "")
    production_date = data.get("production_date", "")

    if not recipe_name:
        return jsonify({"error": "Missing recipe name"}), 400

    try:
        prod_date = datetime.strptime(production_date, "%d/%m/%Y")
    except Exception:
        prod_date = datetime.today()

    best_before = prod_date + timedelta(days=365)

    # Brand is passed as a separate field to the label PDF (own line above).
    recipe_format_display = build_display_name(
        {"brand": "", "format": recipe_format},
        recipe_name=recipe_name,
    )

    label_buffer = io.BytesIO()
    generate_label_pdf(label_buffer, brand_name, recipe_format_display, lot, best_before.strftime("%d/%m/%Y"))
    label_buffer.seek(0)

    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', recipe_name)
    return send_file(label_buffer, mimetype="application/pdf", as_attachment=True,
                     download_name="Label_" + safe_name + "_" + lot + ".pdf")

# ── Digital Checklists ─────────────────────────────────────────────────


# ── Checklist Status ──────────────────────────────────────────────────
def _has_meaningful_data(checklist_data):
    """Check if a checklist has any real user input, not just empty auto-save."""
    sections = checklist_data.get("sections", {})
    for sec_key, sec_data in sections.items():
        if isinstance(sec_data, dict):
            for item_key, item_val in sec_data.items():
                if item_val:
                    return True
        elif sec_data:
            return True
    temps = checklist_data.get("temps", {})
    for key, val in temps.items():
        if val and str(val).strip():
            return True
    for field in ["signoff_kitchen"]:
        if checklist_data.get(field, "").strip():
            return True
    if checklist_data.get("notes", "").strip():
        return True
    production = checklist_data.get("production", {})
    for key, val in production.items():
        if val and str(val).strip() and str(val).strip() != "0":
            return True
    for field in ["produced", "bb_produced"]:
        prod = checklist_data.get(field, {})
        for key, val in prod.items():
            if val and str(val).strip() and str(val).strip() != "0":
                return True
    if checklist_data.get("kettles_end", "").strip() and checklist_data.get("kettles_end", "").strip() != "0":
        return True
    return False


# ── Master CCP ─────────────────────────────────────────────────────────
@app.route("/api/ccp-master", methods=["GET"])
@login_required
def get_ccp_master():
    """GET - return the master CCP document."""
    return jsonify(load_ccp_master())


@app.route("/api/ccp-master", methods=["POST"])
@login_required
def update_ccp_master():
    """POST - update the master CCP document."""
    data = request.json or {}
    save_ccp_master(data)
    return jsonify({"success": True})


# ── Traceability ──────────────────────────────────────────────────────
WEEKLY_SIGNOFFS_PATH = os.path.join(DATA_DIR, "weekly_signoffs.json")


def _load_weekly_signoffs():
    """Load the weekly HOO sign-off records."""
    return _load_json(WEEKLY_SIGNOFFS_PATH, {})


def _save_weekly_signoffs(data):
    """Persist the weekly HOO sign-off records."""
    _save_json(WEEKLY_SIGNOFFS_PATH, data)


def _week_completion_state(week_id):
    """Return ('all_complete' | 'partial' | 'none') for a week, plus the
    list of scheduled-and-complete day_idxs vs scheduled-but-incomplete.

    'all_complete' means every day that had a schedule entry is also a
    completed checklist. (Days with no scheduled vessels don't block.)
    """
    schedule_data = load_schedule(week_id) or {}
    sched = (schedule_data.get("schedule") or {}) if schedule_data else {}
    scheduled_days = []
    for d_idx in range(7):
        day = sched.get(str(d_idx), {}) or {}
        if any((day.get(v) or "").strip() for v in VESSELS):
            scheduled_days.append(d_idx)
    if not scheduled_days:
        return "none", [], []
    complete = []
    incomplete = []
    for d_idx in scheduled_days:
        cl = load_checklist(week_id, d_idx)
        if cl and cl.get("completed"):
            complete.append(d_idx)
        else:
            incomplete.append(d_idx)
    if not incomplete:
        return "all_complete", complete, incomplete
    return "partial", complete, incomplete


# ── Production Tracker ────────────────────────────────────────────────
# ── Production tracker helpers ────────────────────────────────────────
# Buckets that appear in the tracker breakdown. Ordered for stacked-bar rendering
# (bottom to top): SS sizes first, then Frozen, Other, Kettle's End, BB.
TRACKER_BUCKETS = ["SS-876ML", "SS-750ML", "SS-473ML", "FZ", "Other", "Kettles End", "BB"]


def _empty_buckets():
    """Return a zeroed production-tracker bucket dict."""
    return {b: 0 for b in TRACKER_BUCKETS}


def _get_previous_day_schedule(week_id, d_idx, schedule_cache=None):
    """Return the schedule dict for the day BEFORE (week_id, d_idx).

    This is what was started yesterday, i.e. what's being finished/produced today.

    For d_idx > 0: returns schedule[d_idx - 1] of this week.
    For d_idx == 0 (Monday): crosses to previous week's Sunday (d_idx == 6).
    Returns {} if no schedule data available.
    """
    if d_idx > 0:
        sched = schedule_cache if schedule_cache is not None else (load_schedule(week_id) or {})
        if sched and sched.get("schedule"):
            return sched["schedule"].get(str(d_idx - 1), {}) or {}
        return {}
    try:
        prev_week_start = datetime.strptime(week_id, "%Y-%m-%d") - timedelta(days=7)
        prev_week_id = prev_week_start.strftime("%Y-%m-%d")
    except ValueError:
        return {}
    prev_sched = load_schedule(prev_week_id) or {}
    if prev_sched and prev_sched.get("schedule"):
        return prev_sched["schedule"].get("6", {}) or {}
    return {}


def _day_buckets(week_id, d_idx, recipes_cache=None, schedule_cache=None):
    """Return per-bucket totals for a single (week_id, day_idx).

    KEY SEMANTIC: "Amount Produced" entered on day D refers to units that
    FINISHED on day D — meaning the recipe started on day D-1. We therefore
    look up the PREVIOUS day's schedule to classify format, not today's.

    BB units and Kettle's End are their own buckets regardless of recipe.
    """
    buckets = _empty_buckets()
    cl = load_checklist(week_id, d_idx)
    if not cl:
        return buckets, False

    if recipes_cache is None:
        recipes = load_recipes()
    else:
        recipes = recipes_cache

    # Look up PREVIOUS day's schedule (with cross-week Monday fallback)
    prev_day_schedule = _get_previous_day_schedule(week_id, d_idx, schedule_cache)

    # Amount Produced per vessel → bucket by prev-day's recipe format
    produced = cl.get("produced") or {}
    for vessel_id, amount in produced.items():
        try:
            amt = int(amount)
        except (ValueError, TypeError):
            continue
        if amt <= 0:
            continue
        # The recipe that was STARTED yesterday and is being FINISHED today
        recipe_name = prev_day_schedule.get(vessel_id, "")
        recipe_data = recipes.get(recipe_name) if recipe_name else None
        fmt = (recipe_data or {}).get("format", "")
        bucket = _classify_format(fmt)
        buckets[bucket] = buckets.get(bucket, 0) + amt

    bb = cl.get("bb_produced") or {}
    for vessel_id, amount in bb.items():
        try:
            amt = int(amount)
        except (ValueError, TypeError):
            continue
        if amt > 0:
            buckets["BB"] += amt

    try:
        ke = int(cl.get("kettles_end", 0) or 0)
    except (ValueError, TypeError):
        ke = 0
    if ke > 0:
        buckets["Kettles End"] += ke

    return buckets, _has_meaningful_data(cl)


def _sum_buckets(target, source):
    """Add all bucket values from source into target in-place."""
    for k, v in source.items():
        target[k] = target.get(k, 0) + v


def _bucket_total(buckets):
    """Sum the values across a production-tracker bucket dict."""
    return sum(buckets.values())


def _week_totals(week_id):
    """Calculate bucketed totals for a single week. Returns dict with bucket
    keys + 'total'. Used by month/year endpoints."""
    schedule_data = load_schedule(week_id) or {}
    recipes = load_recipes()
    buckets = _empty_buckets()
    for d_idx in range(7):
        day_b, _ = _day_buckets(week_id, d_idx,
                                recipes_cache=recipes,
                                schedule_cache=schedule_data)
        _sum_buckets(buckets, day_b)
    out = dict(buckets)
    out["total"] = _bucket_total(buckets)
    return out


# ── Init ──────────────────────────────────────────────────────────────
# Inventory data paths.
#
# Historical context: prior to the universal-inventory merge (May 2026), the
# system tracked inventory only for organic-tagged production. Files lived
# under data/organic/. After the merge, all production tracks inventory
# regardless of certification, and files live under data/inventory/.
#
# The constants below still use the ORGANIC_ prefix because they are
# referenced in dozens of places throughout app.py; renaming them inline
# would make this diff massive. They now point at the inventory/ paths.
# A startup migration moves data/organic/ → data/inventory/ if needed.
ORGANIC_DIR = INVENTORY_DIR  # legacy alias — see comment above
ORGANIC_RAW_PATH = os.path.join(INVENTORY_DIR, "raw_materials.json")
ORGANIC_FG_PATH = os.path.join(INVENTORY_DIR, "finished_goods.json")
ORGANIC_SALES_PATH = os.path.join(INVENTORY_DIR, "sales.json")

SKU_META_PATH = os.path.join(INVENTORY_DIR, "sku_meta.json")  # PAR levels
# Manual inventory adjustments log (additions and subtractions outside of
# production runs and sales). Each entry records what changed, why, and
# which LOT(s) were drained or created. Used for audit traceability.
AUDITS_PATH      = os.path.join(INVENTORY_DIR, "audits.json")
# Camera-scan request log: per-day rolling counter for daily-limit enforcement
# plus an audit trail of every scan (success or failure).
RM_RECEIPT_PHOTOS_DIR = os.path.join(DATA_DIR, "rm_receipt_photos")
os.makedirs(RM_RECEIPT_PHOTOS_DIR, exist_ok=True)
# Raw material section organization. User-defined sections + per-ingredient
# assignment. Pre-seeded with the 6-section structure on first load.


def _migrate_organic_to_inventory():
    """One-time migration: rename data/organic/ → data/inventory/ if applicable.

    Idempotent: safe to call on every startup. Three states handled:
      1. Only data/organic/ exists → rename it to data/inventory/
      2. Only data/inventory/ exists → no action (already migrated)
      3. Both exist → don't touch; assume manual intervention required (rare)
      4. Neither exists → no action (fresh install)
    """
    legacy_path = os.path.join(DATA_DIR, "organic")
    new_path = INVENTORY_DIR
    legacy_exists = os.path.isdir(legacy_path)
    new_exists = os.path.isdir(new_path)

    if legacy_exists and not new_exists:
        try:
            os.rename(legacy_path, new_path)
            print(f"[migration] Renamed {legacy_path} → {new_path}")
        except Exception as e:
            print(f"[migration] Failed to rename: {e}")
    elif legacy_exists and new_exists:
        # Both exist — leave alone, log a warning so admin can investigate
        print(f"[migration] WARNING: both {legacy_path} and {new_path} exist. "
              f"Manual review needed. Using new path.")


_migrate_organic_to_inventory()
os.makedirs(INVENTORY_DIR, exist_ok=True)
_ripe_init_paths(INVENTORY_DIR)  # wire Ripe orders sale logic to Soma's inventory
_retail_init_paths(INVENTORY_DIR)  # wire SBBC retail orders sale logic likewise


def _autotag_existing_organic_data():
    """One-time data tag: stamp existing pre-merge entries with
    certification: 'Organic'.

    Before the merge, the system only tracked organic production, so any
    existing record without a certification field is by definition organic.
    Idempotent — only touches records that lack the certification field.
    Runs once per file at startup.
    """
    files_to_check = [
        ORGANIC_RAW_PATH, ORGANIC_RUNS_PATH, ORGANIC_FG_PATH, ORGANIC_SALES_PATH
    ]
    for path in files_to_check:
        if not os.path.exists(path):
            continue
        try:
            data = _load_json(path, [])
            if not isinstance(data, list):
                continue
            changed = 0
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if "certification" not in entry:
                    entry["certification"] = "Organic"
                    changed += 1
            if changed:
                _save_json(path, data)
                print(f"[autotag] Stamped 'Organic' on {changed} entries in {path}")
        except Exception as e:
            print(f"[autotag] Failed for {path}: {e}")

# Per-path threading locks — one lock per file, created on demand.


# ── Organic Page ──────────────────────────────────────────────────────
# ── Organic: Get ingredient list from organic recipes ─────────────────
# Fallback master list used only if no organic recipes exist yet.
ORGANIC_INGREDIENTS_FALLBACK = [
    {"name": "Chicken Bones", "unit": "kg"},
    {"name": "Ginger Juice", "unit": "ml"},
    {"name": "Honey", "unit": "ml"},
    {"name": "Lemon Juice", "unit": "ml"},
    {"name": "Lemons", "unit": "Pack"},
    {"name": "Pink Salt", "unit": "g"},
    {"name": "Turmeric Juice", "unit": "Pack"},
]

ORGANIC_CUSTOM_ITEMS_PATH = os.path.join(ORGANIC_DIR, "custom_ingredients.json")


def _format_pack_label(amount, unit):
    """Format a label like '750 ml' or '8 kg' from amount+unit. Used by the
    migrator's smart-upgrade step to recognize pack labels from legacy config."""
    if amount == int(amount):
        amount = int(amount)
    if unit:
        return f"{amount} {unit}"
    return str(amount)


# ── Raw material section organization ─────────────────────────────────
# User-defined sections (created via /api/organic/raw-materials/sections)
# replace the previous hardcoded heuristic. Each ingredient is explicitly
# assigned to a section by name+unit key. Unassigned ingredients land in
# a special "Unassigned" section that's always shown last.

# Default section list seeded on first load. User can rename/reorder/add/
# delete after that — these are just a starting point.
# Special "Unassigned" section. Not stored in user list; surfaced
# separately so users can see what still needs classifying.
UNASSIGNED_SECTION_ID = "_unassigned"
UNASSIGNED_SECTION_NAME = "Unassigned"


# ── Organic: Raw Material Inventory (FIFO) ────────────────────────────
# The RM inventory + invoice + receipt-photo ROUTES now live in raw_materials.py
# (raw_materials_bp). The constants/helpers below stay here (shared with the
# consumption chain + reconcile tools) and are reached via app.-qualification.


# ── One-time reconciliation: rebuild raw-material consumption ─────────────
# After the date-aware FIFO fix, historical lots may still carry deductions
# that were attributed under the old date-blind rule (a lot consumed by a run
# that predates its delivery). This rebuild replays every completed run from a
# clean slate so all 'remaining' balances become date-honest. Operated via the
# /admin/reconcile-raw control page (Preview then Apply).
def _rebuild_raw_material_consumption(materials, runs, recipes):
    """Deterministically rebuild every lot's 'remaining':
      1) reset each lot to its full received quantity
      2) replay every completed run in chronological order (oldest start date
         first, then vessel), each drawing only from date-eligible lots
         (received on or before the run's start date), oldest-first.
    Mutates `materials` ('remaining') and `runs` ('ingredients_used') in place.
    Returns {runs_replayed, warnings}. Does NOT save — the caller decides."""
    for mat in materials:
        try:
            mat["remaining"] = round(float(mat.get("quantity") or 0), 4)
        except (ValueError, TypeError):
            mat["remaining"] = 0

    completed = [r for r in runs
                 if r.get("status") == "completed" and (r.get("amount_produced") or 0) > 0]

    def _start_key(r):
        di = r.get("day_idx")
        return ((r.get("week_id") or ""),
                di if isinstance(di, int) else 99,
                (r.get("vessel") or ""))
    completed.sort(key=_start_key)

    all_warnings = []
    replayed = 0
    for run in completed:
        recipe_data = recipes.get(run.get("recipe", "")) or {}
        if not recipe_data:
            continue
        run_start_date = _run_start_date_str(run.get("week_id"), run.get("day_idx"))
        eligible_mats = _eligible_lots_for_date(materials, run_start_date)
        try:
            amount = int(run.get("amount_produced") or 0)
        except (ValueError, TypeError):
            amount = 0
        ingredients_used, warns = _deduct_run_ingredients(
            recipe_data, run.get("vessel", ""), run.get("recipe", ""), amount, eligible_mats)
        run["ingredients_used"] = ingredients_used
        all_warnings.extend(warns)
        replayed += 1

    return {"runs_replayed": replayed, "warnings": all_warnings}

# Audit/traceability ROUTES (reconcile-raw, trace, stock-exceptions, mass-balance)
# now live in audit_tools.py (audit_tools_bp); the helpers above + below stay here.


# ── Organic: Invoices (standalone module, keyed by supplier + date + LOT#s) ──
INVOICES_DIR = os.path.join(ORGANIC_DIR, "invoices")
os.makedirs(INVOICES_DIR, exist_ok=True)
INVOICES_INDEX_PATH = os.path.join(ORGANIC_DIR, "invoices.json")

ALLOWED_INVOICE_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_INVOICE_BYTES = 10 * 1024 * 1024  # 10 MB

INVOICE_MIME_MAP = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _invoice_mime(filename):
    """Return the MIME type for a stored invoice file, by extension."""
    ext = os.path.splitext(filename)[1].lower()
    return INVOICE_MIME_MAP.get(ext, "application/octet-stream")


def _save_invoice_file_bytes(prefix, file_storage):
    """Save uploaded invoice, return (filename, metadata). Raises ValueError on bad input."""
    if not file_storage or not file_storage.filename:
        raise ValueError("No file provided")
    original_name = file_storage.filename
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_INVOICE_EXT:
        allowed = ", ".join(sorted(ALLOWED_INVOICE_EXT))
        raise ValueError(f"Unsupported file type '{ext}'. Allowed: {allowed}")
    try:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    except Exception:
        size = 0
    if size > MAX_INVOICE_BYTES:
        raise ValueError(f"File too large ({size} bytes). Max {MAX_INVOICE_BYTES} bytes.")
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]", "_", prefix or "inv")
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    stored_name = f"{safe_prefix}_{ts}{ext}"
    path = os.path.join(INVOICES_DIR, stored_name)
    file_storage.save(path)
    try:
        actual_size = os.path.getsize(path)
    except OSError:
        actual_size = size
    return stored_name, {
        "original_name": original_name,
        "size_bytes": actual_size,
        "mime_type": _invoice_mime(stored_name),
    }


def _remove_invoice_file(filename):
    """Delete a stored invoice file from disk (best-effort)."""
    if not filename:
        return False
    safe = os.path.basename(filename)
    if safe != filename:
        return False
    path = os.path.join(INVOICES_DIR, safe)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


def _parse_lots_field(form_data):
    """Pull LOT#s from a multipart form. Accepts repeated 'lots[]' or 'lots' fields,
    or a single comma-separated 'lots' string."""
    lots = []
    try:
        for v in form_data.getlist("lots[]"):
            if v and v.strip():
                lots.append(v.strip())
        for v in form_data.getlist("lots"):
            if v and v.strip():
                for item in v.split(","):
                    if item.strip():
                        lots.append(item.strip())
    except AttributeError:
        # Plain dict fallback
        raw = form_data.get("lots", "")
        if raw:
            for item in str(raw).split(","):
                if item.strip():
                    lots.append(item.strip())
    # Dedupe preserving order
    seen = set()
    out = []
    for l in lots:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _cleanup_legacy_invoices():
    """One-time cleanup on startup: wipe old per-raw-material invoices and files."""
    try:
        materials = _load_json(ORGANIC_RAW_PATH, [])
        dirty = False
        for m in materials:
            if "invoice" in m:
                m.pop("invoice", None)
                dirty = True
        if dirty:
            _save_json(ORGANIC_RAW_PATH, materials)
        # Wipe invoice files that don't match any record in the new index
        known = {r.get("filename", "") for r in _load_json(INVOICES_INDEX_PATH, [])}
        if os.path.exists(INVOICES_DIR):
            for fname in os.listdir(INVOICES_DIR):
                if fname not in known:
                    try:
                        os.remove(os.path.join(INVOICES_DIR, fname))
                    except OSError:
                        pass
    except Exception:
        pass


_cleanup_legacy_invoices()

# ── Organic: Contacts (suppliers, buyers, distributors) ───────────────


@app.route("/api/organic/contacts", methods=["GET"])
@login_required
def get_organic_contacts():
    """GET /api/organic/contacts - return saved organic supplier/buyer contacts."""
    return jsonify(_load_json(ORGANIC_CONTACTS_PATH, {}))

# ── Organic: Production Runs ──────────────────────────────────────────


def _run_start_date_str(week_id, day_idx):
    """ISO date (YYYY-MM-DD) a run started on, or None if week_id is malformed."""
    try:
        return (datetime.strptime(week_id, "%Y-%m-%d")
                + timedelta(days=int(day_idx))).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _eligible_lots_for_date(materials, run_start_date):
    """Lots a run may consume: received on or before run_start_date (undated
    lots are always eligible so stock is never stranded), oldest-first (FIFO).
    Returns the SAME dict references as `materials`, so in-place 'remaining'
    edits persist and are saved by the caller."""
    def _eligible(mat):
        if run_start_date is None:
            return True
        dr = (mat.get("date_received") or "").strip()
        if not dr:
            return True
        return dr <= run_start_date
    return sorted(
        [m for m in materials if _eligible(m)],
        key=lambda m: ((m.get("date_received") or ""), (m.get("created_at") or "")),
    )


def _deduct_run_ingredients(recipe_data, vessel, recipe_name, amount, eligible_mats):
    """Deduct one production run's raw materials from `eligible_mats`, oldest-first.
    Mutates each consumed lot's 'remaining' in place. Returns
    (ingredients_used, warnings). Shared by the live completion path and the
    one-time reconciliation rebuild so both apply identical logic."""
    ingredients_used = []
    warnings = []
    if amount <= 0:
        return ingredients_used, warnings

    is_115L = vessel == "115L"
    half_factor = 0.5 if is_115L else 1.0
    try:
        recipe_yield = int(recipe_data.get("yield") or 0)
    except (ValueError, TypeError):
        recipe_yield = 0
    jar_l = _jar_volume_liters(recipe_data) or 0.75
    batch_liters = recipe_yield * jar_l * half_factor

    for section in INGREDIENT_SECTIONS:
        items = recipe_data.get(section, [])
        for item in items:
            if not is_structured_ingredient(item):
                continue
            if item.get("needs_review"):
                continue
            item_name = (item.get("name") or "").strip()
            if not item_name:
                continue
            # Untracked ingredients (e.g. water) are unlimited — never
            # deduct from inventory and never flag insufficient stock.
            if is_untracked_ingredient(item_name):
                continue
            try:
                recipe_amount = float(item.get("amount") or 0)
            except (ValueError, TypeError):
                recipe_amount = 0
            if recipe_amount <= 0:
                continue
            unit = (item.get("unit") or "").strip()
            if not unit:
                continue
            if unit == "per L":
                qty_needed = recipe_amount * batch_liters
                display_unit = "g"
            else:
                qty_needed = recipe_amount * half_factor
                display_unit = unit
            if qty_needed <= 0:
                continue
            qty_remaining_to_deduct = round(qty_needed, 4)
            # Pass 1: name + exact unit
            for mat in eligible_mats:
                if mat["remaining"] <= 0:
                    continue
                if not ingredients_match(mat["item"], item_name):
                    continue
                mat_unit = (mat.get("unit") or "").strip()
                if unit != "per L" and mat_unit != display_unit:
                    continue
                deduct = min(qty_remaining_to_deduct, mat["remaining"])
                mat["remaining"] = round(mat["remaining"] - deduct, 4)
                qty_remaining_to_deduct = round(qty_remaining_to_deduct - deduct, 4)
                ingredients_used.append({
                    "item": mat["item"],
                    "supplier_lot": mat["supplier_lot"],
                    "quantity_used": deduct,
                    "unit": mat_unit or display_unit,
                    "raw_material_id": mat["id"],
                    # Snapshot supplier + receive date INTO the batch record so
                    # one-step-back traceability survives even if the lot entry
                    # is later edited or removed.
                    "supplier": mat.get("supplier", ""),
                    "date_received": mat.get("date_received", ""),
                })
                if qty_remaining_to_deduct <= 0:
                    break
            # Pass 2: name-only fallback
            if qty_remaining_to_deduct > 0:
                for mat in eligible_mats:
                    if mat["remaining"] <= 0:
                        continue
                    if not ingredients_match(mat["item"], item_name):
                        continue
                    mat_unit = (mat.get("unit") or "").strip()
                    if unit != "per L" and mat_unit == display_unit:
                        continue
                    deduct = min(qty_remaining_to_deduct, mat["remaining"])
                    mat["remaining"] = round(mat["remaining"] - deduct, 4)
                    qty_remaining_to_deduct = round(qty_remaining_to_deduct - deduct, 4)
                    ingredients_used.append({
                        "item": mat["item"],
                        "supplier_lot": mat["supplier_lot"],
                        "quantity_used": deduct,
                        "unit": mat_unit or display_unit,
                        "raw_material_id": mat["id"],
                        "supplier": mat.get("supplier", ""),
                        "date_received": mat.get("date_received", ""),
                    })
                    if qty_remaining_to_deduct <= 0:
                        break
            if qty_remaining_to_deduct > 0:
                ingredients_used.append({
                    "item": item_name,
                    "supplier_lot": "INSUFFICIENT_STOCK",
                    "quantity_used": qty_remaining_to_deduct,
                    "unit": display_unit,
                    "negative": True,
                })
                warnings.append({
                    "kind": "insufficient_stock",
                    "vessel": vessel,
                    "recipe": recipe_name,
                    "ingredient": item_name,
                    "shortfall": qty_remaining_to_deduct,
                    "unit": display_unit,
                    "message": (f"Insufficient {item_name}: short by "
                                f"{qty_remaining_to_deduct} {display_unit} "
                                f"on {vessel} ({recipe_name}). "
                                f"Production proceeded with what was on hand."),
                })
    return ingredients_used, warnings


def _complete_organic_run(finish_week_id, finish_day_idx, produced_data):
    """Process organic production amounts entered on the FINISH day.

    Semantic: 'Amount Produced' entered on day D is the output of the recipe
    that was STARTED on day D-1. So this function:
      1. Finds organic runs scheduled on the previous day (start day)
      2. Matches each run's vessel against produced_data[vessel]
      3. Deducts raw materials based on amount produced
      4. Creates / updates a finished goods entry, timestamped to the FINISH day
         with LOT# = production(start) date + 365 days, matching the case label
    Idempotent: re-saving updates in place. Sales already made against an
    edited entry are preserved (quantity_remaining = new_qty - already_sold).
    """
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    materials = _load_json(ORGANIC_RAW_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, []) if os.path.exists(ORGANIC_SALES_PATH) else []
    recipes = load_recipes()

    warnings = []

    # Runs frozen by a zero-day reset: their FG output is already embodied in the
    # reset baselines, so we must NOT regenerate it (doing so doubled inventory).
    # Fetched once; degrade to no-freeze on any error so a save/boot never crashes.
    try:
        import ledger
        _frozen_runs = ledger._reset_frozen_run_ids()
    except Exception:
        _frozen_runs = set()

    start_week_id, start_day_idx = _previous_day_coords(finish_week_id, finish_day_idx)

    try:
        finish_date = datetime.strptime(finish_week_id, "%Y-%m-%d") + timedelta(days=finish_day_idx)
    except ValueError:
        finish_date = datetime.now()
    # LOT# must match the physical case label, which /api/label prints as the
    # production (start) date + 365. Deriving it from the FINISH date instead put
    # the system one day ahead of the case on any batch produced one day and
    # packaged the next — the systemic "case 140527 vs system 150527" mismatch.
    # Use the start date; fall back to finish_date only if it's unparseable.
    try:
        start_date = datetime.strptime(start_week_id, "%Y-%m-%d") + timedelta(days=int(start_day_idx))
    except (ValueError, TypeError):
        start_date = finish_date
    expiry_lot = (start_date + timedelta(days=365)).strftime("%d%m%y")

    produced = (produced_data or {}).get("produced") or {}

    # A batch can only contain raw material that was on hand when it was made.
    # Every run in this call started on (start_week_id, start_day_idx), so a lot
    # is eligible only if it was received on or before that date. This keeps
    # deductions date-honest: receiving a new delivery never back-fills an
    # earlier run, and re-saving a later kettle can't shift an earlier kettle
    # onto a newer lot. Among eligible lots we consume oldest-first (true FIFO).
    run_start_date = _run_start_date_str(start_week_id, start_day_idx)
    eligible_mats = _eligible_lots_for_date(materials, run_start_date)

    for run in runs:
        if run.get("week_id") != start_week_id or run.get("day_idx") != start_day_idx:
            continue
        if run.get("id") in _frozen_runs:
            # Completed as of a reset cutover — its output is in the reset baseline.
            # Skip entirely: never regenerate FG or touch raw for a frozen run.
            continue
        # Universal inventory: process every scheduled run, regardless of
        # the recipe's certification status. The recipe is our source of truth
        # for the certification tag that travels onto FG entries.
        recipe_name = run.get("recipe", "")
        recipe_data = recipes.get(recipe_name, {})
        if not recipe_data:
            continue

        vessel = run["vessel"]

        vid = vessel.replace("(", "").replace(")", "")
        try:
            amount = int(produced.get(vid, 0))
        except (ValueError, TypeError):
            amount = 0

        # finish_week + finish_day + vessel)
        fg_id = f"fg_{finish_week_id}_{finish_day_idx}_{vessel}"
        existing_fg = next((f for f in fg if f.get("id") == fg_id), None)

        # If amount is 0 and no prior FG exists, skip entirely (no production)
        if amount <= 0 and not existing_fg:
            continue

        # Raw-material record is FROZEN once a batch is completed: an ordinary
        # re-save (still producing) must not rewrite what was recorded as consumed,
        # which is what previously let a later recipe edit silently alter past
        # batches. We compute the deduction only on FIRST completion, or when
        # reversing (amount<=0). The deliberate "recompute everything" path is the
        # reconcile tool, not an ordinary save. Note: the raw charge is per-batch
        # (independent of jars produced), so freezing it while still letting the
        # finished-goods quantity update below is correct.
        was_completed = run.get("status") == "completed"
        prev_used = run.get("ingredients_used") or []
        freeze = was_completed and amount > 0 and bool(prev_used)

        if freeze:
            ingredients_used = prev_used  # keep the snapshot taken at completion
        else:
            # Reversal or first/again-from-scratch completion: undo any prior
            # deduction, then deduct fresh from the current recipe + eligible lots.
            if was_completed and prev_used:
                for used in prev_used:
                    if used.get("negative"):
                        continue  # Insufficient-stock markers don't restore inventory
                    rm_id = used.get("raw_material_id")
                    qty = used.get("quantity_used", 0)
                    if rm_id and qty:
                        for mat in materials:
                            if mat.get("id") == rm_id:
                                mat["remaining"] = round(mat.get("remaining", 0) + qty, 4)
                                break
            ingredients_used, ded_warnings = _deduct_run_ingredients(
                recipe_data, vessel, recipe_name, amount, eligible_mats)
            warnings.extend(ded_warnings)

        run["status"] = "completed" if amount > 0 else "scheduled"
        run["ingredients_used"] = ingredients_used
        run["amount_produced"] = amount
        run["completed_at"] = datetime.now().isoformat() if amount > 0 else None
        run["finish_week_id"] = finish_week_id
        run["finish_day_idx"] = finish_day_idx

        # Compute already-sold quantity for this FG so quantity_remaining is correct on edit
        already_sold = 0
        for s in sales:
            # Legacy path: single fg_id on sale record
            if s.get("fg_id") == fg_id:
                try:
                    already_sold += int(s.get("quantity", 0))
                except (ValueError, TypeError):
                    pass
                continue
            # New path: per-lot breakdown in lots[] array
            for lot_entry in (s.get("lots") or []):
                for b in (lot_entry.get("breakdown") or []):
                    if b.get("fg_id") == fg_id:
                        try:
                            already_sold += int(b.get("quantity", 0))
                        except (ValueError, TypeError):
                            pass

        # remaining quantity inconsistent with (produced - sold), the user has
        # manually adjusted it (e.g., for breakage). Re-saving daily production
        # will overwrite that adjustment — warn the user.
        if existing_fg and amount > 0:
            prev_produced = int(existing_fg.get("quantity_produced") or 0)
            prev_remaining = int(existing_fg.get("quantity_remaining") or 0)
            implied_remaining = max(0, prev_produced - already_sold)
            if existing_fg.get("last_adjusted_at") or prev_remaining != implied_remaining:
                manual_delta = implied_remaining - prev_remaining
                if manual_delta != 0:
                    warnings.append({
                        "kind": "lot_adjust_overwritten",
                        "vessel": vessel,
                        "recipe": recipe_name,
                        "manual_delta": manual_delta,
                        "message": (f"A manual stock adjustment on {vessel} "
                                    f"({recipe_name}) of {-manual_delta:+d} units "
                                    f"was overwritten by re-saving production. "
                                    f"If breakage / loss still applies, re-enter "
                                    f"the adjustment from Finished Goods."),
                    })

        if amount > 0:
            new_remaining = max(0, amount - already_sold)
            new_fg = {
                "id": fg_id,
                "run_id": run["id"],
                "recipe": recipe_name,
                "brand": recipe_data.get("brand", ""),
                "format": recipe_data.get("format", ""),
                "certification": (recipe_data.get("certification") or "").strip(),
                "lot": expiry_lot,
                "quantity_produced": amount,
                "quantity_remaining": new_remaining,
                "vessel": vessel,
                "week_id": finish_week_id,
                "day_idx": finish_day_idx,
                "start_week_id": start_week_id,
                "start_day_idx": start_day_idx,
                "created_at": existing_fg.get("created_at") if existing_fg else datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            if existing_fg:
                # Update in place — but explicitly clear last_adjusted_at since
                for k, v in new_fg.items():
                    existing_fg[k] = v
                existing_fg.pop("last_adjusted_at", None)
            else:
                fg.append(new_fg)
        else:
            if existing_fg:
                fg = [f for f in fg if f.get("id") != fg_id]

    _save_json(ORGANIC_RUNS_PATH, runs)
    _save_json(ORGANIC_RAW_PATH, materials)
    _save_json(ORGANIC_FG_PATH, fg)
    return warnings

# ── Organic: Finished Goods ──────────────────────────────────────────


def _group_fg_by_sku(fg):
    """Aggregate FG entries into one dict per (brand, recipe, format).
    Returns list of dicts sorted by display name. Includes certification
    inherited from any of the underlying entries (consistent within a SKU).

    NOTE: this only includes SKUs that have at least one FG entry. For the
    inventory list view (where we want to show every active recipe even with
    zero stock), use `_group_fg_with_catalog` instead.
    """
    groups = {}
    recipes_cache = None
    for entry in fg:
        key = _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", ""))
        if key not in groups:
            cert = (entry.get("certification") or "").strip()
            if not cert:
                if recipes_cache is None:
                    recipes_cache = load_recipes()
                rec = recipes_cache.get(entry.get("recipe") or "", {})
                cert = (rec.get("certification") or "").strip()
            groups[key] = {
                "sku_key": key,
                "brand": entry.get("brand", ""),
                "recipe": entry.get("recipe", ""),
                "format": entry.get("format", ""),
                "certification": cert,
                "display": _sku_display(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", "")),
                "total_produced": 0,
                "total_remaining": 0,
                "lot_count": 0,
                "active_lot_count": 0,
                "has_baseline": False,
            }
        g = groups[key]
        try:
            g["total_produced"] += int(entry.get("quantity_produced") or 0)
            g["total_remaining"] += int(entry.get("quantity_remaining") or 0)
        except (ValueError, TypeError):
            pass
        if entry.get("migration_baseline"):
            g["has_baseline"] = True

    lots_per_group = {}
    for entry in fg:
        key = _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", ""))
        lot = entry.get("lot", "")
        lots_per_group.setdefault(key, {})
        lots_per_group[key].setdefault(lot, {"produced": 0, "remaining": 0})
        try:
            lots_per_group[key][lot]["produced"] += int(entry.get("quantity_produced") or 0)
            lots_per_group[key][lot]["remaining"] += int(entry.get("quantity_remaining") or 0)
        except (ValueError, TypeError):
            pass
    for key, g in groups.items():
        g["lot_count"] = len(lots_per_group.get(key, {}))
        g["active_lot_count"] = sum(1 for v in lots_per_group.get(key, {}).values() if v["remaining"] > 0)

    out = list(groups.values())
    out.sort(key=lambda r: (r["brand"], r["recipe"], r["format"]))
    return out


def _group_fg_with_catalog(fg, recipes):
    """Like _group_fg_by_sku, but joins against the recipe catalog so EVERY
    active (non-archived) recipe appears as a SKU, even ones with zero stock
    or no production history. SKUs with no FG entries get total_produced=0,
    total_remaining=0, and certification pulled from the recipe definition.

    This is the source for the inventory list view: a complete product
    catalog with current stock levels next to each row.
    """
    base = {g["sku_key"]: g for g in _group_fg_by_sku(fg)}

    # Add catalog entries for any active recipe NOT already in base
    for recipe_name, recipe in (recipes or {}).items():
        if recipe.get("archived"):
            continue
        brand = (recipe.get("brand") or "").strip()
        fmt = (recipe.get("format") or "").strip()
        cert = (recipe.get("certification") or "").strip()
        key = _sku_key(brand, recipe_name, fmt)
        if key in base:
            if not base[key].get("certification") and cert:
                base[key]["certification"] = cert
            continue
        # No FG entries for this recipe — synthesize a zero-stock catalog row
        base[key] = {
            "sku_key": key,
            "brand": brand,
            "recipe": recipe_name,
            "format": fmt,
            "certification": cert,
            "display": _sku_display(brand, recipe_name, fmt),
            "total_produced": 0,
            "total_remaining": 0,
            "lot_count": 0,
            "active_lot_count": 0,
            "has_baseline": False,
            "catalog_only": True,  # signal: never produced or all entries deleted
        }

    def _fmt_sort_key(fmt):
        """SS (shelf-stable) before FZ (frozen) before anything else."""
        f = (fmt or "").upper()
        if f.startswith("SS"): return "0"
        if f.startswith("FZ"): return "1"
        return "2" + f

    out = list(base.values())
    out.sort(key=lambda r: (
        (r.get("brand") or "").lower(),
        _fmt_sort_key(r.get("format") or ""),
        (r.get("recipe") or "").lower(),
    ))
    return out


# ── Organic: Finished Goods + manual adjustments ────────────────────────────
# These routes now live in finished_goods.py (finished_goods_bp). The grouping
# helpers (_group_fg_by_sku, _group_fg_with_catalog) stay above; the FG path
# constants stay in app.py — the blueprint reaches them via app.ORGANIC_FG_PATH.


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY AUDIT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _load_audits():
    """Load the food-safety audits list."""
    return _load_json(AUDITS_PATH, [])


def _save_audits(data):
    """Persist the food-safety audits list."""
    _save_json(AUDITS_PATH, data)


@app.route("/audit/<kind>")
@login_required
def audit_page(kind):
    """Render the audit page for 'rm' or 'fg'."""
    if kind not in ("rm", "fg"):
        return "Invalid audit type", 400
    return render_template("audit.html", kind=kind)


@app.route("/audits")
@login_required
def audits_history():
    """Read-only history of completed audits, newest first.

    Strips the heavy 'items' / 'results' / 'current_idx' fields — the
    'adjustments' summary is what's useful to look back at."""
    audits = _load_audits()
    rows = []
    for a in audits:
        if a.get("status") != "completed":
            continue
        rows.append({
            "id":            a.get("id"),
            "kind":          a.get("kind"),
            "brand":         a.get("brand"),
            "section":       a.get("section"),
            "started_at":    a.get("started_at"),
            "completed_at":  a.get("completed_at"),
            "adjustments":   a.get("adjustments") or [],
        })
    rows.sort(key=lambda r: r.get("completed_at") or "", reverse=True)
    return render_template("audits.html", audits=rows)


@app.route("/api/audit/start", methods=["POST"])
@login_required
def start_audit():
    """Start a new audit. Both kinds are now stateless — the audit doc is
    returned to the client and only persisted when /complete is called.

    FG body: { kind: 'fg', brand: str }
    RM body: { kind: 'rm', section: str }  (one section per audit)
    Back-compat: 'categories: [name]' is accepted in place of brand/section.
    """
    data = request.get_json() or {}
    kind = data.get("kind")
    if kind not in ("rm", "fg"):
        return jsonify({"error": "kind must be rm or fg"}), 400

    if kind == "fg":
        brand = (data.get("brand") or "").strip()
        if not brand:
            cats = data.get("categories") or []
            if cats:
                brand = (cats[0] or "").strip()
        if not brand:
            return jsonify({"error": "brand required for FG audit"}), 400

        items = _build_fg_audit_items(brand)
        audit = {
            "id":         "audit_" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "kind":       "fg",
            "status":     "in_progress",
            "brand":      brand,
            "started_at": datetime.now().isoformat(),
            "items":      items,
            "current_idx": 0,
            "results":    {},
        }
        return jsonify({"ok": True, "audit": audit})

    # RM: stateless, one section per audit
    section = (data.get("section") or "").strip()
    if not section:
        cats = data.get("categories") or []
        if cats:
            section = (cats[0] or "").strip()
    if not section:
        return jsonify({"error": "section required for RM audit"}), 400

    items = _build_rm_audit_items(section)
    audit = {
        "id":         "audit_" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "kind":       "rm",
        "status":     "in_progress",
        "section":    section,
        "started_at": datetime.now().isoformat(),
        "items":      items,
        "current_idx": 0,
        "results":    {},
    }
    return jsonify({"ok": True, "audit": audit})


def _collect_recipe_ingredients():
    """Return a set of (name, unit) tuples for every ingredient referenced
    across all recipes (kettle_overnight / after_skim / finishing /
    add_to_jar sections).

    The (name, unit) tuple is the canonical key used everywhere else in
    the codebase — section assignments, the grouped-raw-materials view,
    receipt validation. The audit must use the same key shape or
    section assignments won't resolve.
    """
    recipes = load_recipes()
    pairs = set()
    for r in recipes.values():
        if not isinstance(r, dict):
            continue
        for sec_key in INGREDIENT_SECTIONS:
            for ing in (r.get(sec_key) or []):
                if not isinstance(ing, dict):
                    continue
                name = (ing.get("name") or "").strip()
                if not name:
                    continue
                unit = (ing.get("unit") or "").strip()
                pairs.add((name, unit))
    return pairs


def _build_rm_audit_items(section_name):
    """Build per-(name,unit) audit list for RM, scoped to a single section.

    Master list = (name, unit) pairs from recipes ∪ inventory history.
    Section lookup uses _section_for_ingredient(name, unit, ...) — the
    same key shape (`name|unit`) the rest of the system uses to store
    assignments. Items without a section assignment go to 'Unassigned'.

    Each row aggregates remaining across all lots matching both name
    AND unit (so 'Carrots|kg' and 'Carrots|lb' are separate rows, matching
    the data model). Zero-stock items are included so the auditor can
    record a baseline surplus.

    Item id is `name|unit` (the same key shape used everywhere else).
    """
    materials = _load_json(ORGANIC_RAW_PATH, [])
    sections_data = _load_rm_sections()
    sections_by_id = {s["id"]: s["name"] for s in sections_data.get("sections", [])}
    section_name_norm = (section_name or "").strip()

    # Master list: (name, unit) pairs from recipes ∪ inventory history
    pairs = set(_collect_recipe_ingredients())
    in_recipes = set(pairs)  # snapshot before we add inventory-only pairs
    for mat in materials:
        name = (mat.get("item") or "").strip()
        if not name:
            continue
        unit = (mat.get("unit") or "").strip()
        pairs.add((name, unit))

    items_map = {}
    for name, unit in pairs:
        section_id = _section_for_ingredient(name, unit, sections_data)
        section = sections_by_id.get(section_id, "") or "Unassigned"
        if section_name_norm and section != section_name_norm:
            continue

        # All lots for this exact (name, unit) with stock on hand
        lots = [m for m in materials
                if (m.get("item") or "").strip() == name
                and (m.get("unit") or "").strip() == unit
                and float(m.get("remaining") or 0) > 0]
        lots.sort(key=lambda m: m.get("date_received", ""))

        system_qty = round(sum(float(l.get("remaining") or 0) for l in lots), 4)

        key = _ingredient_section_key(name, unit)
        items_map[key] = {
            "id":         key,           # "name|unit"
            "name":       name,
            "unit":       unit,
            "section":    section,
            "system_qty": system_qty,
            "lot_count":  len(lots),
            "in_recipes": (name, unit) in in_recipes,
        }

    items = sorted(items_map.values(), key=lambda x: (x["name"].lower(), x["unit"].lower()))
    return items


def _build_fg_audit_items(brand):
    """Build per-SKU audit items for FG inventory, scoped to a single brand.

    SKU master list = recipes.json filtered by brand, unioned with any FG
    history for the brand (catches legacy SKUs whose recipe card was removed).
    Each item row represents one SKU (brand|recipe|format) with system_qty
    summed across matching FG lots. SKUs with zero on hand are included so
    the auditor can record a baseline-lot surplus if physical stock exists.
    """
    brand_norm = (brand or "").strip()
    recipes = load_recipes()
    fg = _load_json(ORGANIC_FG_PATH, [])

    sku_map = {}  # sku_key -> item dict

    for recipe_name, r in recipes.items():
        if (r.get("brand") or "").strip() != brand_norm:
            continue
        fmt = (r.get("format") or "").strip()
        key = _sku_key(brand_norm, recipe_name, fmt)
        sku_map[key] = {
            "id":            key,
            "recipe":        recipe_name,
            "brand":         brand_norm,
            "format":        fmt,
            "certification": (r.get("certification") or "").strip(),
            "system_qty":    0,
            "lot_count":     0,
            "in_recipes":    True,
        }

    for f in fg:
        if (f.get("brand") or "").strip() != brand_norm:
            continue
        recipe_name = (f.get("recipe") or "").strip()
        fmt         = (f.get("format") or "").strip()
        remaining   = int(f.get("quantity_remaining") or 0)
        key = _sku_key(brand_norm, recipe_name, fmt)
        if key not in sku_map:
            # Legacy SKU — not in current recipe set
            sku_map[key] = {
                "id":            key,
                "recipe":        recipe_name,
                "brand":         brand_norm,
                "format":        fmt,
                "certification": (f.get("certification") or "").strip(),
                "system_qty":    0,
                "lot_count":     0,
                "in_recipes":    False,
            }
        if remaining > 0:
            sku_map[key]["system_qty"] += remaining
            sku_map[key]["lot_count"]  += 1

    fmt_order = {"SS": 0, "FZ": 1, "BB": 2}
    items = sorted(
        sku_map.values(),
        key=lambda x: (
            fmt_order.get((x["format"] or "")[:2].upper(), 9),
            x["recipe"],
        ),
    )
    return items


@app.route("/api/audit/active/<kind>", methods=["GET"])
@login_required
def get_active_audit(kind):
    """Both audit kinds are stateless — no resume — so this always
    returns null. Endpoint retained for back-compat with stale clients."""
    return jsonify({"audit": None})


@app.route("/api/audit/<audit_id>/complete", methods=["POST"])
@login_required
def complete_audit(audit_id):
    """Complete an audit — apply all counted adjustments to inventory.

    Both kinds are now stateless — the audit doc was not persisted at /start.
    The doc is constructed fresh here from the request body, applied, then
    written to audits.json with status='completed'.

    FG body: { kind: 'fg', brand: str,   started_at?, results }
    RM body: { kind: 'rm', section: str, started_at?, results }

    Guards against double-complete via existing completed-status check.
    """
    data = request.get_json() or {}
    audits = _load_audits()
    audit = next((a for a in audits if a["id"] == audit_id), None)

    if audit and audit.get("status") == "completed":
        return jsonify({"error": "Audit already completed"}), 409

    if not audit:
        kind = (data.get("kind") or "").strip()
        if kind == "fg":
            brand = (data.get("brand") or "").strip()
            if not brand:
                return jsonify({"error": "brand required for FG audit"}), 400
            audit = {
                "id":          audit_id,
                "kind":        "fg",
                "status":      "in_progress",
                "brand":       brand,
                "started_at":  data.get("started_at") or datetime.now().isoformat(),
                "items":       _build_fg_audit_items(brand),
                "current_idx": 0,
                "results":     {},
            }
        elif kind == "rm":
            section = (data.get("section") or "").strip()
            if not section:
                return jsonify({"error": "section required for RM audit"}), 400
            audit = {
                "id":          audit_id,
                "kind":        "rm",
                "status":      "in_progress",
                "section":     section,
                "started_at":  data.get("started_at") or datetime.now().isoformat(),
                "items":       _build_rm_audit_items(section),
                "current_idx": 0,
                "results":     {},
            }
        else:
            return jsonify({"error": "Audit not found"}), 404
        audits.append(audit)

    audit["results"].update(data.get("results", {}))
    kind = audit["kind"]
    adjustments = []

    if kind == "rm":
        adjustments = _apply_rm_audit(audit)
    else:
        adjustments = _apply_fg_audit(audit)

    audit["status"]       = "completed"
    audit["completed_at"] = datetime.now().isoformat()
    audit["adjustments"]  = adjustments
    _save_audits(audits)
    return jsonify({"ok": True, "adjustments": len(adjustments), "audit": audit})


def _apply_rm_audit(audit):
    """Apply RM audit results — FIFO drain on shortage, baseline lot on surplus.

    Per-item-name model (RM is bulk; auditors count totals, not per-lot).
      - diff < 0  → drain oldest lots first (FIFO) until shortfall covered.
                    Matches how production deduction already consumes lots,
                    so traceability fidelity is unchanged.
      - diff > 0  → create a new RM row with supplier='Audit baseline',
                    supplier_lot='BASELINE-YYYYMMDD', no PO, no cost.
                    Keeps real supplier lots honest — surplus doesn't
                    get falsely attributed to the newest receipt.
      - diff == 0 → no change, no log entry.

    Unit for the baseline lot resolves: newest lot's unit > recipe unit > ''.
    counted == None means the item was skipped (not counted).
    """
    materials = _load_json(ORGANIC_RAW_PATH, [])
    adjustments = []
    new_rows = []
    now_iso = datetime.now().isoformat()
    today = datetime.now().strftime("%Y-%m-%d")

    for result_key, result in audit["results"].items():
        counted = result.get("counted")
        if counted is None:
            continue
        try:
            counted = round(float(counted), 4)
        except (ValueError, TypeError):
            continue
        if counted < 0:
            continue

        # Parse "name|unit" key
        if "|" in result_key:
            item_name, unit_key = result_key.split("|", 1)
        else:
            # Legacy key (name only) — match any unit
            item_name, unit_key = result_key, None
        item_name = item_name.strip()
        if unit_key is not None:
            unit_key = unit_key.strip()

        lots = [m for m in materials
                if (m.get("item") or "").strip() == item_name
                and (unit_key is None or (m.get("unit") or "").strip() == unit_key)
                and float(m.get("remaining") or 0) > 0]
        lots.sort(key=lambda m: m.get("date_received", ""))

        system_total = round(sum(float(l.get("remaining") or 0) for l in lots), 4)
        diff = round(counted - system_total, 4)
        if diff == 0:
            continue

        baseline_lot_code = None
        if diff < 0:
            to_remove = abs(diff)
            for lot in lots:
                if to_remove <= 0:
                    break
                avail = float(lot.get("remaining") or 0)
                take = min(avail, to_remove)
                lot["remaining"] = round(avail - take, 4)
                lot["last_adjusted_at"] = now_iso
                to_remove = round(to_remove - take, 4)
        else:
            unit = unit_key or ""
            if not unit and lots:
                unit = (lots[-1].get("unit") or "").strip()

            baseline_lot_code = "BASELINE-" + datetime.now().strftime("%Y%m%d")
            new_id = (datetime.now().strftime("%Y%m%d%H%M%S")
                      + "_audit_" + str(len(new_rows)))
            new_rows.append({
                "id":             new_id,
                "item":           item_name,
                "supplier":       "Audit baseline",
                "date_received":  today,
                "supplier_lot":   baseline_lot_code,
                "quantity":       diff,
                "unit":           unit,
                "remaining":      diff,
                "created_at":     now_iso,
                "audit_baseline": True,
                "audit_id":       audit["id"],
            })

        adjustments.append({
            "item":         item_name,
            "unit":         unit_key or "",
            "system_qty":   system_total,
            "counted":      counted,
            "diff":         diff,
            "baseline_lot": baseline_lot_code,
        })

    if new_rows:
        materials.extend(new_rows)
    _save_json(ORGANIC_RAW_PATH, materials)

    for i, adj in enumerate(adjustments):
        _record_adjustment({
            "id":           "audit_rm_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + str(i),
            "kind":         "audit_rm",
            "item":         adj["item"],
            "unit":         adj["unit"],
            "system_qty":   adj["system_qty"],
            "counted":      adj["counted"],
            "diff":         adj["diff"],
            "baseline_lot": adj["baseline_lot"],
            "audit_id":     audit["id"],
            "created_at":   now_iso,
        })

    return adjustments


def _apply_fg_audit(audit):
    """Apply FG audit results — per-SKU FIFO drain on shortage, baseline lot
    creation on surplus.

    Result keys are sku_key strings ('BRAND|RECIPE|FORMAT'). For each SKU:
      - diff < 0  → drain oldest FG lots first (FIFO) until shortfall covered.
      - diff > 0  → create a new FG row with lot 'BASELINE-YYYYMMDD',
                     source='audit_baseline', no production/raw-material links.
      - diff == 0 → no change, no log entry.

    counted == None means the SKU was skipped (not counted) and is ignored.
    """
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()
    adjustments = []
    new_rows = []
    now_iso = datetime.now().isoformat()

    for sku_key, result in audit["results"].items():
        counted = result.get("counted")
        if counted is None:
            continue  # explicit skip
        try:
            counted = int(counted)
        except (ValueError, TypeError):
            continue
        if counted < 0:
            continue

        parts = sku_key.split("|")
        if len(parts) != 3:
            continue
        brand, recipe_name, fmt = parts

        matching = [f for f in fg
                    if (f.get("brand") or "").strip() == brand
                    and (f.get("recipe") or "").strip() == recipe_name
                    and (f.get("format") or "").strip() == fmt
                    and int(f.get("quantity_remaining") or 0) > 0]
        matching.sort(key=lambda f: f.get("created_at", ""))

        system_total = sum(int(f.get("quantity_remaining") or 0) for f in matching)
        diff = counted - system_total
        if diff == 0:
            continue

        baseline_lot_code = None
        if diff < 0:
            to_remove = abs(diff)
            for f in matching:
                if to_remove <= 0:
                    break
                avail = int(f.get("quantity_remaining") or 0)
                take = min(avail, to_remove)
                f["quantity_remaining"] = avail - take
                f["last_adjusted_at"]   = now_iso
                to_remove -= take
        else:
            recipe_meta = recipes.get(recipe_name, {}) or {}
            baseline_lot_code = "BASELINE-" + datetime.now().strftime("%Y%m%d")
            new_id = ("fg_baseline_" + datetime.now().strftime("%Y%m%d%H%M%S")
                      + "_" + str(len(new_rows)))
            new_rows.append({
                "id":                 new_id,
                "recipe":             recipe_name,
                "brand":              brand,
                "format":             fmt,
                "certification":      (recipe_meta.get("certification") or "").strip(),
                "lot":                baseline_lot_code,
                "quantity_produced":  diff,
                "quantity_remaining": diff,
                "vessel":             "Audit baseline",
                "week_id":            None,
                "day_idx":            None,
                "created_at":         now_iso,
                "source":             "audit_baseline",
                "audit_id":           audit["id"],
            })

        adjustments.append({
            "sku_key":      sku_key,
            "brand":        brand,
            "recipe":       recipe_name,
            "format":       fmt,
            "system_qty":   system_total,
            "counted":      counted,
            "diff":         diff,
            "baseline_lot": baseline_lot_code,
        })

    if new_rows:
        fg.extend(new_rows)
    _save_json(ORGANIC_FG_PATH, fg)

    for i, adj in enumerate(adjustments):
        _record_adjustment({
            "id":           "audit_fg_" + datetime.now().strftime("%Y%m%d%H%M%S") + "_" + str(i),
            "kind":         "audit_fg",
            "brand":        adj["brand"],
            "recipe":       adj["recipe"],
            "format":       adj["format"],
            "system_qty":   adj["system_qty"],
            "counted":      adj["counted"],
            "diff":         adj["diff"],
            "baseline_lot": adj["baseline_lot"],
            "audit_id":     audit["id"],
            "created_at":   now_iso,
        })

    return adjustments


@app.route("/api/audit/categories/<kind>", methods=["GET"])
@login_required
def audit_categories(kind):
    """Return available categories for category selection screen."""
    if kind == "rm":
        # Sections that have any (name, unit) pair assigned to them, plus
        # 'Unassigned' if any item lacks an assignment. Uses the canonical
        # `name|unit` key shape via _section_for_ingredient — matches how
        # assignments are actually stored.
        sections_data = _load_rm_sections()
        sections_ordered = sections_data.get("sections", [])
        sections_by_id = {s["id"]: s["name"] for s in sections_ordered}

        pairs = set(_collect_recipe_ingredients())
        materials = _load_json(ORGANIC_RAW_PATH, [])
        for mat in materials:
            n = (mat.get("item") or "").strip()
            if not n:
                continue
            u = (mat.get("unit") or "").strip()
            pairs.add((n, u))

        used_sections = set()
        has_unassigned = False
        for name, unit in pairs:
            sid = _section_for_ingredient(name, unit, sections_data)
            sname = sections_by_id.get(sid, "") if sid else ""
            if sname:
                used_sections.add(sname)
            else:
                has_unassigned = True

        cats = [s["name"] for s in sections_ordered if s["name"] in used_sections]
        if has_unassigned:
            cats.append("Unassigned")
        if not cats:
            cats = ["Unassigned"]
        return jsonify(cats)
    else:
        # FG brand list = brands in recipes.json (source of truth) unioned
        # with any brand that has FG history (catches legacy/defunct recipes).
        recipes = load_recipes()
        brands = {(r.get("brand") or "").strip() for r in recipes.values()}
        fg = _load_json(ORGANIC_FG_PATH, [])
        brands.update((f.get("brand") or "").strip() for f in fg)
        brands = {b for b in brands if b}
        return jsonify(sorted(brands) or ["Unknown"])

# ── Supplier catalog ────────────────────────────────────────────────────
# suppliers.json: list of {id, name, ingredients: [{name, unit}]}
# rm_receipt_photos/: <entry_id>.<ext> — one photo per add-inventory entry


# ── RM receipt photo upload ──────────────────────────────────────────────
_RM_PHOTO_ALLOWED = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "pdf"}
_RM_PHOTO_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _compute_available_stock():
    """Per-SKU FG stock available to buyer portals (e.g. Ripe).

    Returns {sku_key: {"available", "gross", "pending", "buffer"}}:
      gross     — sum of quantity_remaining across all FG lots for the SKU
      pending   — units committed by Ripe sales not yet deducted (deducted=False).
                  New sales deduct immediately at approval, so this is only
                  non-zero for legacy records.
      buffer    — company.ripe_inventory_buffer; units withheld from the buyer
                  portal, reserved for in-house / on-site use.
      available — max(0, gross - pending - buffer); what Ripe actually sees.

    available can be 0 while physical stock equals or is below the buffer —
    that's intentional, not a bug.
    """
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, [])
    company = _load_company_info()
    buffer_units = int(company.get("ripe_inventory_buffer") or 0)

    gross_map = {}
    for entry in fg:
        key = _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", ""))
        gross_map[key] = gross_map.get(key, 0) + int(entry.get("quantity_remaining") or 0)

    # Match sales exactly first, then case-insensitively for legacy lowercased keys.
    pending_map = {}
    lower_map = {k.lower(): k for k in gross_map}
    for sale in sales:
        if sale.get("deducted") is not False:
            continue
        sale_key = sale.get("sku_key", "")
        if sale_key in gross_map:
            matched = sale_key
        elif sale_key.lower() in lower_map:
            matched = lower_map[sale_key.lower()]
        else:
            continue
        pending_map[matched] = pending_map.get(matched, 0) + int(sale.get("quantity") or 0)

    return {
        key: {
            "available": max(0, gross - pending_map.get(key, 0) - buffer_units),
            "gross":     gross,
            "pending":   pending_map.get(key, 0),
            "buffer":    buffer_units,
        }
        for key, gross in gross_map.items()
    }


@app.route("/api/internal/catalogue", methods=["GET"])
def internal_buyer_catalogue():
    """Return the product catalogue for a specific buyer — their assigned SKUs
    with buyer-specific pricing and live FG stock.
    Called by the buyer portal (e.g. Ripe) to build its order form.

    Query params:
      buyer  — buyer name (e.g. 'Ripe') or buyer id
    """
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    provided = request.headers.get("X-Internal-Key", "")
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(provided.encode(), internal_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401

    buyer_ref = (request.args.get("buyer") or "").strip()
    if not buyer_ref:
        return jsonify({"error": "buyer query param required"}), 400

    buyers = _load_buyers()
    buyer = next(
        (b for b in buyers
         if b["name"].lower() == buyer_ref.lower() or b["id"] == buyer_ref),
        None
    )
    if not buyer:
        return jsonify({"error": f"Buyer '{buyer_ref}' not found"}), 404

    stock_map = _compute_available_stock()
    meta = _load_json(SKU_META_PATH, {})
    company = _load_company_info()
    buffer_units = int(company.get("ripe_inventory_buffer") or 0)

    catalogue = []
    for sku in (buyer.get("skus") or []):
        sk = sku.get("sku_key", "")
        s = stock_map.get(sk, {})
        available = s.get("available", 0)
        m = meta.get(sk, {})
        catalogue.append({
            "sku_key":     sk,
            "name":        sku.get("recipe", ""),
            "brand":       sku.get("brand", ""),
            "format":      sku.get("format", ""),
            "display":     sku.get("display", ""),
            "position":    sku.get("position", 999),
            "price":       sku.get("price"),
            "cogs":        sku.get("cogs"),
            "margin_pct":  sku.get("margin_pct"),
            "buyer_sku":   sku.get("buyer_sku", ""),
            "active":      sku.get("active", True),
            # Live stock
            "available_units": available,
            "available_cases": available // 12,
            "par":         m.get("par"),
        })

    catalogue.sort(key=lambda x: (x["position"], x["format"], x["name"]))

    return jsonify({
        "buyer": buyer["name"],
        "buyer_id": buyer["id"],
        "catalogue": catalogue,
        "units_per_case": 12,
        "buffer_units": buffer_units,
        "ripe_credits": _active_ripe_credits(company),
        "rules": {
            "ss_small_order_threshold": int(company.get("ss_small_order_threshold") or 20),
            "fzbb_small_lead_days":     int(company.get("fzbb_small_lead_days")  or 3),
            "fzbb_large_lead_days":     int(company.get("fzbb_large_lead_days")  or 7),
            "fzbb_large_threshold":     int(company.get("fzbb_large_threshold")  or 8),
        },
    })


@app.route("/api/internal/sku-audit", methods=["GET"])
def internal_sku_audit():
    """Cross-reference Soma FG recipes against Ripe product soma_sku_key values.
    Key-gated. Returns a full match/mismatch report.
    """
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    provided = request.headers.get("X-Internal-Key", "")
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(provided.encode(), internal_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401

    recipes = load_recipes()
    soma_keys = {}  # sku_key -> {recipe_name, brand, format, display}
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        brand  = (rdata.get("brand") or "").strip()
        fmt    = _normalize_format((rdata.get("format") or "").strip())
        key    = _sku_key(brand, rname, fmt)
        soma_keys[key] = {
            "recipe_name": rname,
            "brand": brand,
            "format": fmt,
            "display": build_display_name(rdata, rname),
            "has_format_in_name": bool(FORMAT_RE.search(rname)),
        }

    fg = _load_json(ORGANIC_FG_PATH, [])
    fg_stock = {}
    for entry in fg:
        key = _sku_key(entry.get("brand",""), entry.get("recipe",""), entry.get("format",""))
        fg_stock[key] = fg_stock.get(key, 0) + int(entry.get("quantity_remaining") or 0)

    ripe_url = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
    ikey     = os.environ.get("INTERNAL_API_KEY", "")
    ripe_products = []
    ripe_error = None
    if ripe_url and ikey:
        try:
            import urllib.request as _ur
            req = _ur.Request(
                f"{ripe_url}/api/internal/products",
                headers={"X-Internal-Key": ikey},
                method="GET",
            )
            with _ur.urlopen(req, timeout=8) as resp:
                ripe_products = json.loads(resp.read())
        except Exception as e:
            ripe_error = str(e)

    ripe_audit = []
    ripe_matched_keys = set()
    for p in ripe_products:
        sk = (p.get("soma_sku_key") or "").strip()
        result = {
            "ripe_id":   p["id"],
            "ripe_name": p["name"],
            "ripe_format": p.get("format",""),
            "soma_sku_key": sk,
            "active": p.get("active", True),
        }
        if not sk:
            result["status"] = "no_key"
            result["issue"]  = "soma_sku_key not set"
        elif sk in soma_keys:
            result["status"] = "matched"
            result["soma_recipe"] = soma_keys[sk]
            result["fg_units"]    = fg_stock.get(sk, 0)
            result["fg_cases"]    = fg_stock.get(sk, 0) // 12
            ripe_matched_keys.add(sk)
            if soma_keys[sk]["has_format_in_name"]:
                result["status"] = "matched_dirty_name"
                result["issue"]  = f"Recipe name '{soma_keys[sk]['recipe_name']}' contains format suffix — should be cleaned"
        else:
            result["status"] = "broken"
            result["issue"]  = f"soma_sku_key '{sk}' not found in Soma recipes"
            suggestions = []
            sk_lower = sk.lower()
            for k, v in soma_keys.items():
                if v["recipe_name"].lower() in sk_lower or sk_lower in v["recipe_name"].lower():
                    suggestions.append(k)
            if suggestions:
                result["suggestions"] = suggestions
        ripe_audit.append(result)

    unlinked_soma = []
    for key, info in soma_keys.items():
        if key not in ripe_matched_keys:
            unlinked_soma.append({
                "sku_key": key,
                "recipe_name": info["recipe_name"],
                "brand": info["brand"],
                "format": info["format"],
                "display": info["display"],
                "fg_units": fg_stock.get(key, 0),
                "has_format_in_name": info["has_format_in_name"],
            })

    matched  = sum(1 for r in ripe_audit if r["status"] in ("matched","matched_dirty_name"))
    broken   = sum(1 for r in ripe_audit if r["status"] == "broken")
    no_key   = sum(1 for r in ripe_audit if r["status"] == "no_key")
    dirty    = sum(1 for r in ripe_audit if r["status"] == "matched_dirty_name")

    return jsonify({
        "summary": {
            "ripe_products": len(ripe_products),
            "soma_recipes":  len(soma_keys),
            "matched":  matched,
            "broken":   broken,
            "no_key":   no_key,
            "dirty_names": dirty,
            "unlinked_soma_recipes": len(unlinked_soma),
        },
        "ripe_products": ripe_audit,
        "unlinked_soma_recipes": unlinked_soma,
        "ripe_error": ripe_error,
    })


@app.route("/api/internal/fg-stock", methods=["GET"])
def internal_fg_stock():
    """Return available FG stock per SKU for Ripe portal.
    Key-gated via X-Internal-Key. Returns {sku_key: {available, par}}.
    Excludes committed (scheduled-but-not-yet-deducted) Ripe sales and the
    ripe_inventory_buffer.
    """
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    provided = request.headers.get("X-Internal-Key", "")
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(provided.encode(), internal_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401

    stock_map = _compute_available_stock()
    meta = _load_json(SKU_META_PATH, {})
    return jsonify({
        key: {"available": s["available"], "par": meta.get(key, {}).get("par")}
        for key, s in stock_map.items()
    })


# ── Company Info ──────────────────────────────────────────────────────────────

@app.route("/api/company-info", methods=["GET"])
@login_required
def get_company_info():
    """GET /api/company-info - return company info / order rules."""
    info = _load_company_info()
    # Surface credits as the canonical list, migrating a legacy v1 scalar so the
    # settings UI always renders the manageable form (persists on next save).
    if not isinstance(info.get("ripe_credits"), list):
        info["ripe_credits"] = _active_ripe_credits(info)
    return jsonify(info)


@app.route("/api/company-info", methods=["PATCH"])
@login_required
def update_company_info():
    """POST /api/company-info - update company info / order rules."""
    data = request.get_json() or {}
    info = _load_company_info()
    allowed = set(_DEFAULT_COMPANY_INFO.keys())
    _numeric_int_keys = {"ripe_inventory_buffer", "ss_small_order_threshold",
                         "fzbb_small_lead_days", "fzbb_large_lead_days",
                         "fzbb_large_threshold"}
    for k, v in data.items():
        if k not in allowed:
            continue
        if k == "ripe_credits":
            info["ripe_credits"] = _sanitize_ripe_credits(v)
            info.pop("ripe_credit", None)  # retire the legacy v1 scalar once migrated
        elif k in _numeric_int_keys:
            try:
                info[k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
        else:
            info[k] = (v or "").strip() if isinstance(v, str) else v
    _save_json(COMPANY_INFO_PATH, info)
    return jsonify({"ok": True, "info": info})

# ── Organic: Sales ───────────────────────────────────────────────────
# The sales routes now live in sales.py (sales_bp). What stays here is shared:
# _migrate_legacy_sales (called at startup) and the buyer helpers below
# (reached from sales.py via app._load_buyers, etc.).


def _migrate_legacy_sales():
    """Convert old-style sales (single fg_id) to new lots[] shape.
    Idempotent: only migrates entries that don't already have a 'lots' field."""
    if not os.path.exists(ORGANIC_SALES_PATH):
        return
    try:
        sales = _load_json(ORGANIC_SALES_PATH, [])
    except Exception:
        return
    changed = False
    for s in sales:
        if s.get("lots"):
            continue
        if s.get("fg_id") or s.get("fg_lot"):
            s["lots"] = [{
                "lot": s.get("fg_lot", ""),
                "quantity": int(s.get("quantity") or 0),
                "fg_ids": [s["fg_id"]] if s.get("fg_id") else [],
            }]
            if not s.get("sku_key"):
                s["sku_key"] = _sku_key(s.get("brand", ""), s.get("recipe", ""), s.get("format", ""))
            changed = True
    if changed:
        _save_json(ORGANIC_SALES_PATH, sales)
        try:
            print(f"[startup] organic sales migrated to multi-LOT shape")
        except Exception:
            pass


# ── Buyer catalog ────────────────────────────────────────────────────────
BUYERS_PATH = os.path.join(INVENTORY_DIR, "buyers.json")


def _all_sku_catalog():
    """Build the master SKU catalogue from active recipes."""
    recipes = load_recipes()
    catalog = []
    seen = set()
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        brand = (rdata.get("brand") or "").strip()
        fmt = (rdata.get("format") or "").strip()
        key = _sku_key(brand, rname, fmt)
        if key in seen:
            continue
        seen.add(key)
        catalog.append({"brand": brand, "recipe": rname, "format": fmt,
                         "sku_key": key, "display": _sku_display(brand, rname, fmt)})
    catalog.sort(key=lambda s: (s["brand"].lower(), s["recipe"].lower()))
    return catalog


def _load_buyers():
    """Load the buyers list from disk."""
    return _load_json(BUYERS_PATH, [])


def _save_buyers(data):
    """Persist the buyers list to disk."""
    _save_json(BUYERS_PATH, data)


def _buyer_resolver(buyers_list):
    """Build a resolver that canonicalises a raw (sale.buyer, sale.location_name)
    pair to (parent_buyer_name, location_name) using buyers.json as the truth
    table.

    Historical sales were sometimes entered with the buyer field as a
    location-suffixed string (the location feature came later). Common shapes
    seen in the wild:
        "Parent - LocationName"
        "Parent (LocationName)"
        "Natures Emporium (Woodbridge)"   <- note missing apostrophe
        "Nature's Emporium - Newmarket"
        "Nature’s Emporium"         <- smart-quote apostrophe

    To roll any of these up under the parent buyer, the resolver normalises
    apostrophe variants (curly, straight, or absent) when comparing, and
    pre-generates lookup keys in both dash and parenthesis suffix forms.

    Only rolls up when the suffix matches a REGISTERED location of the prefix
    buyer (per buyers.json) — unknown names pass through unchanged so no
    accidental mis-splitting.
    """
    def _norm(s):
        # Lowercase, strip apostrophe variants and outer whitespace. Internal
        # spacing left intact so "Healthy Planet" vs "HealthyPlanet" still
        # differ correctly.
        return (s or "").lower().replace("’", "").replace("'", "").strip()

    DASH_SEPS = [" - ", " — ", " – ", " -", "- ", "-"]
    lookup = {}
    for b in buyers_list:
        bname = (b.get("name") or "").strip()
        if not bname:
            continue
        # Parent name alone (so apostrophe-stripped variants still resolve to
        # the canonical form with the apostrophe).
        lookup.setdefault(_norm(bname), (bname, ""))
        for loc in (b.get("locations") or []):
            loc_name = (loc.get("name") or "").strip()
            if not loc_name:
                continue
            # Dash-style: "Parent - Location"
            for sep in DASH_SEPS:
                lookup.setdefault(_norm(bname + sep + loc_name), (bname, loc_name))
            # Parenthesis-style: "Parent (Location)" and "Parent(Location)"
            lookup.setdefault(_norm(bname + " (" + loc_name + ")"), (bname, loc_name))
            lookup.setdefault(_norm(bname + "(" + loc_name + ")"),  (bname, loc_name))

    def resolve(raw_buyer, sale_location_name=None):
        raw = (raw_buyer or "").strip()
        explicit_loc = (sale_location_name or "").strip()
        hit = lookup.get(_norm(raw))
        if hit:
            parent, loc_from_name = hit
            return parent, (explicit_loc or loc_from_name)
        return raw, explicit_loc

    return resolve


# ── Startup data migrations ─────────────────────────────────────────────────
_migrate_legacy_sales()
_autotag_existing_organic_data()

# ── Organic: Search / Trace ──────────────────────────────────────────


def _sale_touches_fg(s, fids):
    """Return True if sale s drew from any fg_id in fids.
    Handles both new lots[].breakdown and legacy fg_id shapes."""
    if s.get("fg_id") in fids:
        return True
    for lot in (s.get("lots") or []):
        for b in (lot.get("breakdown") or []):
            if b.get("fg_id") in fids:
                return True
        for fid in (lot.get("fg_ids") or []):
            if fid in fids:
                return True
    return False


# ── Mass-balance reconciliation (organic audit) ───────────────────────────
def _compute_mass_balance(date_from, date_to, organic_only=False):
    """Input/output reconciliation for [date_from, date_to].

    Raw materials per ingredient: Opening + Received - Consumed = Expected
    closing, compared to current stock; the discrepancy is manual adjustments /
    loss. Finished goods per SKU: Opening + Produced - Sold = Expected, vs
    current stock. Day-zero migration_baseline AND zero-day reset_baseline
    entries are folded into Opening (they're starting inventory, not
    production); after a reset the FG side is cutover-aware — pre-cutover sales
    and adjustments are skipped (already embodied in the baseline), so the
    baseline + old sales don't read as bogus discrepancy. The raw side is
    unaffected (raw materials are never reset). manual_addition entries are
    intentionally left out so they surface in the discrepancy column, which
    therefore reads as breakage / loss / logged manual adjustment. Each FG row
    also carries 'explained' (signed net of logged adjustments.json entries for
    that SKU, capped at date_to) and 'unexplained' (discrepancy - explained) so
    the gap is split into recorded adjustments vs truly unaccounted movement.

    Dates: raw received = date_received; raw consumed = the run's PRODUCTION
    (start) date; FG produced = finish date; sold = the date stock LEFT FG
    (deducted_at for Ripe approvals, else sale_date) — NOT a Ripe order's
    future delivery date. 'Opening' is derived from flows dated before the
    range. The discrepancy column is exact
    only when date_to is today (current stock is reconciled as-of-now); the
    organic-only toggle filters the finished-goods side by certification — raw
    lots aren't certified individually, so the raw table always shows all."""
    materials = _load_json(ORGANIC_RAW_PATH, [])
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, [])

    def in_range(ds): return bool(ds) and date_from <= ds <= date_to
    def before(ds):   return bool(ds) and ds < date_from

    # Zero-day reset cutover (YYYY-MM-DD or None). The reset replaced FG with
    # clean reset_baseline opening stock but LEFT sales/adjustments intact, so
    # the FG side must treat the baseline as Opening and ignore pre-cutover
    # sales/adjustments (already embodied in the baseline) — otherwise every
    # SKU shows the baseline + every old sale as bogus discrepancy. FG-side
    # ONLY: raw materials were never reset, so the raw table is untouched.
    try:
        import ledger
        cutover = ledger._reset_cutover_date()
    except Exception:
        cutover = None

    # ---- Raw materials (always all — lots carry no certification) ----
    raw = {}

    def rawrow(item, unit):
        k = (item.strip().lower(), unit)
        if k not in raw:
            raw[k] = {"item": item, "unit": unit, "opening": 0.0,
                      "received": 0.0, "consumed": 0.0, "current": 0.0}
        return raw[k]

    for m in materials:
        item = (m.get("item") or "").strip()
        if not item:
            continue
        row = rawrow(item, (m.get("unit") or "").strip())
        try: q = float(m.get("quantity") or 0)
        except (ValueError, TypeError): q = 0.0
        try: rem = float(m.get("remaining") or 0)
        except (ValueError, TypeError): rem = 0.0
        dr = (m.get("date_received") or "").strip()
        row["current"] += rem
        if before(dr):
            row["opening"] += q
        elif in_range(dr):
            row["received"] += q

    for run in runs:
        if run.get("status") != "completed":
            continue
        pdate = _run_start_date_str(run.get("week_id"), run.get("day_idx"))
        for used in (run.get("ingredients_used") or []):
            if used.get("negative"):
                continue
            item = (used.get("item") or "").strip()
            if not item:
                continue
            row = rawrow(item, (used.get("unit") or "").strip())
            try: q = float(used.get("quantity_used") or 0)
            except (ValueError, TypeError): q = 0.0
            if before(pdate):
                row["opening"] -= q
            elif in_range(pdate):
                row["consumed"] += q

    raw_rows = []
    for r in raw.values():
        exp = r["opening"] + r["received"] - r["consumed"]
        raw_rows.append({
            "item": r["item"], "unit": r["unit"],
            "opening": round(r["opening"], 3), "received": round(r["received"], 3),
            "consumed": round(r["consumed"], 3), "expected_closing": round(exp, 3),
            "current": round(r["current"], 3), "discrepancy": round(r["current"] - exp, 3),
        })
    raw_rows.sort(key=lambda x: x["item"].lower())

    # ---- Finished goods per SKU (organic-only filters by certification) ----
    fgr = {}

    def fgrow(sku, label, cert):
        if sku not in fgr:
            fgr[sku] = {"sku": sku, "label": label, "certification": cert,
                        "opening": 0, "produced": 0, "sold": 0, "current": 0}
        return fgr[sku]

    for f in fg:
        cert = (f.get("certification") or "").strip()
        if organic_only and cert.lower() != "organic":
            continue
        sku = _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        row = fgrow(sku, _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")), cert)
        try: produced = int(f.get("quantity_produced") or 0)
        except (ValueError, TypeError): produced = 0
        try: rem = int(f.get("quantity_remaining") or 0)
        except (ValueError, TypeError): rem = 0
        row["current"] += rem
        if f.get("migration_baseline") or f.get("source") == "reset_baseline":
            # Day-zero migration / zero-day reset stock = opening inventory,
            # not production (the reset baseline is a post-audit physical count).
            # Always the earliest inflow, so it lands in Opening (any sales of it
            # dated before the range net it down via the sales loop).
            row["opening"] += produced
            continue
        # Manual additions (manual_addition=True) carry no production date and
        # are DELIBERATELY left out of opening/produced so they surface in the
        # discrepancy column — that's the tool's purpose (logged adjustments).
        pdate = _run_start_date_str(f.get("week_id"), f.get("day_idx"))
        if before(pdate):
            row["opening"] += produced
        elif in_range(pdate):
            row["produced"] += produced

    for s in sales:
        cert = (s.get("certification") or "").strip()
        if organic_only and cert.lower() != "organic":
            continue
        # Reconcile each sale on the date stock actually LEFT FG, not the
        # sale_date. Ripe sales deduct at approval (deducted_at) but are dated
        # by their (often future) DELIVERY date — keying on sale_date both
        # double-counts a pre-reset order delivered after the cutover (phantom
        # surplus) and drops a just-deducted order whose delivery is beyond
        # `to` (phantom shortfall). Manual sales carry no deducted_at, so they
        # fall back to sale_date — identical to the prior behaviour.
        mdate = (s.get("deducted_at") or s.get("sale_date") or s.get("created_at") or "")[:10]
        if cutover and mdate and mdate < cutover:
            continue  # stock left before the reset — already in the baseline
        sku = s.get("sku_key") or _sku_key(s.get("brand", ""), s.get("recipe", ""), s.get("format", ""))
        row = fgrow(sku, _sku_display(s.get("brand", ""), s.get("recipe", ""), s.get("format", "")), cert)
        try: qn = int(s.get("quantity") or 0)
        except (ValueError, TypeError): qn = 0
        if before(mdate):
            row["opening"] -= qn
        elif in_range(mdate):
            row["sold"] += qn

    # ---- Attribute the FG discrepancy to logged manual adjustments ----
    # Every manual add / subtract / physical-count audit is logged in
    # adjustments.json with a signed stock effect. Current stock embeds those
    # effects, but Opening/Produced/Sold never do — so their net is exactly the
    # slice of the discrepancy that IS explained by a recorded adjustment. The
    # remainder ("unexplained") is the truly unaccounted movement (an unrecorded
    # sale, a deleted sale whose stock wasn't restored, etc.). NOT filtered on
    # the low end: Opening absorbs no adjustments, so the discrepancy carries
    # their full history; only capped at date_to so a later adjustment can't leak
    # into a past window.
    explained = {}
    adj_recipes = None  # lazy: subtract records store recipe only, no brand/format
    for adj in _load_json(ADJUSTMENTS_PATH, []):
        kind = adj.get("kind")
        adate = (adj.get("created_at") or "")[:10]
        if adate and adate > date_to:
            continue
        if cutover and adate and adate < cutover:
            continue  # pre-reset adjustment already embodied in the baseline
        try:
            if kind == "add":
                amt = float(adj.get("quantity") or 0)
                sk = _sku_key(adj.get("brand", ""), adj.get("recipe", ""), adj.get("format", ""))
            elif kind == "subtract":
                amt = -float(adj.get("quantity") or 0)
                if adj_recipes is None:
                    adj_recipes = load_recipes()
                meta = adj_recipes.get(adj.get("recipe", "")) or {}
                sk = _sku_key(meta.get("brand", ""), adj.get("recipe", ""), meta.get("format", ""))
            elif kind == "audit_fg":
                amt = float(adj.get("diff") or 0)
                sk = _sku_key(adj.get("brand", ""), adj.get("recipe", ""), adj.get("format", ""))
            else:
                continue  # audit_rm is a raw-material count, not finished goods
        except (ValueError, TypeError):
            continue
        explained[sk] = explained.get(sk, 0.0) + amt

    fg_rows = []
    for r in fgr.values():
        exp = r["opening"] + r["produced"] - r["sold"]
        disc = r["current"] - exp
        expl = explained.get(r["sku"], 0.0)
        fg_rows.append({
            "sku": r["sku"], "label": r["label"], "certification": r["certification"],
            "opening": r["opening"], "produced": r["produced"], "sold": r["sold"],
            "expected_closing": exp, "current": r["current"], "discrepancy": disc,
            "explained": round(expl), "unexplained": round(disc - expl),
        })
    fg_rows.sort(key=lambda x: x["label"].lower())

    return {"from": date_from, "to": date_to, "organic_only": organic_only,
            "raw_materials": raw_rows, "finished_goods": fg_rows}


# ── Hook: Auto-create organic runs when schedule is generated ─────────
def _check_organic_schedule(week_id, schedule):
    """Reconcile organic production runs with the current schedule.

    For each (day, vessel) in this week:
      - If the schedule has an organic recipe → ensure a scheduled run exists
        (creating it if missing, or updating recipe/lot if the recipe changed)
      - If the schedule has no organic recipe → remove any orphaned SCHEDULED run

    COMPLETED runs are NEVER touched. Once a run is completed it is part of the
    traceability record (raw materials deducted, finished goods generated) and
    must not be erased by retroactive schedule edits.
    """
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    recipes = load_recipes()
    week_start = datetime.strptime(week_id, "%Y-%m-%d")

    desired = {}  # (day_idx, vessel) -> recipe_name
    for day_key, vessels in (schedule or {}).items():
        try:
            day_idx = int(day_key)
        except (ValueError, TypeError):
            continue
        for vessel, recipe_name in (vessels or {}).items():
            if not recipe_name:
                continue
            recipe_data = recipes.get(recipe_name) or {}
            # tracked, regardless of certification (Organic, Pasture Raised,
            # Conventional, or untagged). The certification flows from the
            if not recipe_data:
                continue
            desired[(day_idx, vessel)] = recipe_name

    out = []
    seen_keys = set()
    for run in runs:
        # Only touch runs in THIS week. Other weeks' runs pass through unchanged.
        if run.get("week_id") != week_id:
            out.append(run)
            continue
        if run.get("status") == "completed":
            out.append(run)
            continue
        key = (run.get("day_idx"), run.get("vessel"))
        if key in desired:
            # Schedule still wants this slot. Update recipe if it changed.
            new_recipe = desired[key]
            if run.get("recipe") != new_recipe:
                rdata = recipes.get(new_recipe) or {}
                run["recipe"] = new_recipe
                run["brand"] = rdata.get("brand", "")
                try:
                    date = week_start + timedelta(days=key[0])
                    run["lot"] = date.strftime("%d%m%y")
                except Exception:
                    pass
            seen_keys.add(key)
            out.append(run)
        # else: scheduled run no longer matches — drop it (orphan removal)

    # Add any newly-scheduled organic slots that don't yet have a run
    for (day_idx, vessel), recipe_name in desired.items():
        if (day_idx, vessel) in seen_keys:
            continue
        if any(r.get("week_id") == week_id and r.get("day_idx") == day_idx
               and r.get("vessel") == vessel for r in out):
            continue
        date = week_start + timedelta(days=day_idx)
        lot = date.strftime("%d%m%y")
        rdata = recipes.get(recipe_name) or {}
        run = {
            "id": f"{week_id}_{day_idx}_{vessel}",
            "week_id": week_id,
            "day_idx": day_idx,
            "day_name": DAYS[day_idx] if 0 <= day_idx < 7 else "",
            "vessel": vessel,
            "recipe": recipe_name,
            "lot": lot,
            "brand": rdata.get("brand", ""),
            "status": "scheduled",
            "ingredients_used": [],
            "amount_produced": 0,
            "created_at": datetime.now().isoformat(),
        }
        out.append(run)

    _save_json(ORGANIC_RUNS_PATH, out)

# ── Hook: Complete organic runs when daily production is filed ─────────


def _check_organic_completion(finish_week_id, finish_day_idx, checklist_data):
    """When daily production is saved on the FINISH day, process any organic
    runs that were scheduled on the PREVIOUS day (which are now finishing).
    Returns a list of warning dicts for the UI to surface."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    start_week_id, start_day_idx = _previous_day_coords(finish_week_id, finish_day_idx)
    has_organic = any(
        r.get("week_id") == start_week_id and r.get("day_idx") == start_day_idx
        for r in runs
    )
    if has_organic:
        return _complete_organic_run(finish_week_id, finish_day_idx, checklist_data) or []
    return []


if not os.path.exists(RECIPES_PATH):
    from default_recipes import DEFAULT_RECIPES
    save_recipes(DEFAULT_RECIPES)

if not os.path.exists(CCP_MASTER_PATH):
    save_ccp_master(DEFAULT_CCP_SECTIONS)


def _backfill_organic_finished_goods():
    """One-time, idempotent: scan past checklists and build finished goods entries
    for organic production where they're missing. Safe to run on every startup
    because _complete_organic_run is idempotent (updates in place).

    For every existing organic run, looks at the FINISH day's checklist (run start
    day + 1, with cross-week Sunday→Monday). If that checklist has a 'produced'
    amount for the run's vessel, processes it.
    """
    if not os.path.exists(CHECKLISTS_DIR):
        return
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    if not runs:
        return
    # Group runs by (start_week, start_day) so we process each finish-day once
    by_finish = {}
    for run in runs:
        sw = run.get("week_id")
        sd = run.get("day_idx")
        if sw is None or sd is None:
            continue
        try:
            start_dt = datetime.strptime(sw, "%Y-%m-%d") + timedelta(days=int(sd))
        except (ValueError, TypeError):
            continue
        finish_dt = start_dt + timedelta(days=1)
        finish_monday = finish_dt - timedelta(days=finish_dt.weekday())
        finish_week = finish_monday.strftime("%Y-%m-%d")
        finish_day = (finish_dt - finish_monday).days
        by_finish.setdefault((finish_week, finish_day), True)

    backfilled = 0
    for (fw, fd) in by_finish:
        cl_path = os.path.join(CHECKLISTS_DIR, f"{fw}_day{fd}.json")
        if not os.path.exists(cl_path):
            continue
        try:
            with open(cl_path) as f:
                checklist = json.load(f)
        except (OSError, ValueError):
            continue
        if not (checklist or {}).get("produced"):
            continue
        try:
            _check_organic_completion(fw, fd, checklist)
            backfilled += 1
        except Exception:
            pass
    if backfilled:
        try:
            print(f"[startup] organic finished-goods backfill processed {backfilled} day(s)")
        except Exception:
            pass


_backfill_organic_finished_goods()

# ── Ripe order scheduled sale deduction ───────────────────────────────────────
# Sale records from Ripe orders carry:
#   deduction_date  — date inventory should be deducted (delivery/pickup date)
#   deducted        — False until FIFO deduction runs
#   payment_pending — True for Net14 until Stripe confirms payment
#
# This function runs on startup and is callable via API. It processes any
# sale records whose deduction_date has arrived but deduction hasn't run yet.


def _run_scheduled_deductions():
    """FIFO-deduct any Ripe sale records whose deduction_date <= today."""
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    today = datetime.now().date().isoformat()
    changed = False

    for sale in sales:
        if sale.get("deducted", True):
            continue  # already done or legacy (legacy has no deducted field → default True)
        deduction_date = sale.get("deduction_date", "")
        if not deduction_date or deduction_date > today:
            continue  # not yet due

        # Run FIFO deduction for this sale
        units_needed = int(sale.get("quantity", 0))
        recipe = sale.get("recipe", "")
        fmt = (sale.get("format") or "").upper()

        candidates = [
            f for f in fg
            if (f.get("recipe") or "") == recipe
            and (f.get("format") or "").upper() == fmt
            and int(f.get("quantity_remaining") or 0) > 0
        ]

        # Sort FIFO
        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot", ""), e.get("id", "")))

        remaining = units_needed
        lot_summary = {}
        for entry in candidates:
            if remaining <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            take = min(avail, remaining)
            entry["quantity_remaining"] = avail - take
            remaining -= take
            lot = entry.get("lot", "")
            if lot not in lot_summary:
                lot_summary[lot] = {"lot": lot, "quantity": 0, "fg_ids": [], "breakdown": []}
            lot_summary[lot]["quantity"] += take
            lot_summary[lot]["fg_ids"].append(entry["id"])
            lot_summary[lot]["breakdown"].append({"fg_id": entry["id"], "quantity": take})

        sale["lots"] = list(lot_summary.values())
        sale["fg_lot"] = list(lot_summary.keys())[0] if len(lot_summary) == 1 else ""
        sale["deducted"] = True
        sale["deducted_at"] = datetime.now().isoformat()
        if remaining > 0:
            sale["shortfall"] = remaining  # partial deduction — visible in UI
        changed = True

    if changed:
        _save_json(ORGANIC_SALES_PATH, sales)
        _save_json(ORGANIC_FG_PATH, fg)


_run_scheduled_deductions()  # run once on startup


def _seed_sku_meta_defaults():
    """Seed defaults for any SKU that has never had meta set.
    Only writes to SKUs with no entry at all — never overwrites existing
    entries, even if par or price fields are missing (they may be
    intentionally absent e.g. No PAR checkbox was ticked).
    """
    meta = _load_json(SKU_META_PATH, {})
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()
    try:
        grouped = _group_fg_with_catalog(fg, recipes)
    except Exception:
        return
    changed = False
    for g in grouped:
        key = g["sku_key"]
        if key not in meta:
            meta[key] = {"par": 100}
            changed = True
        # Never modify existing entries — user may have intentionally
        # removed par (No PAR)
    if changed:
        _save_json(SKU_META_PATH, meta)


_seed_sku_meta_defaults()


@app.route("/api/ripe-orders/run-deductions", methods=["POST"])
@login_required
def api_run_deductions():
    """Manually trigger scheduled deductions (also runs on startup)."""
    _run_scheduled_deductions()
    return jsonify({"ok": True})


@app.route("/api/ripe-orders/settle-sale/<sale_id>", methods=["POST"])
@login_required
def api_settle_ripe_sale(sale_id):
    """Mark a specific pending-payment Ripe sale as settled."""
    sales = _load_json(ORGANIC_SALES_PATH, [])
    idx = next((i for i, s in enumerate(sales) if s.get("id") == sale_id), None)
    if idx is None:
        return jsonify({"error": "Sale not found"}), 404
    sales[idx]["payment_pending"] = False
    sales[idx]["settled_at"] = datetime.now().isoformat()
    _save_json(ORGANIC_SALES_PATH, sales)
    return jsonify({"ok": True})


@app.route("/api/ripe-orders/settle-by-order/<order_id>", methods=["POST"])
def api_settle_by_order(order_id):
    """Settle all pending-payment sale records for a given Ripe order.
    Called by Ripe portal webhook relay when Stripe confirms payment.
    Authenticated via X-Internal-Key header (no session required).
    """
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    provided = request.headers.get("X-Internal-Key", "")
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(provided.encode(), internal_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401

    sales = _load_json(ORGANIC_SALES_PATH, [])
    settled = 0
    for s in sales:
        if s.get("ripe_order_id") == order_id and s.get("payment_pending"):
            s["payment_pending"] = False
            s["settled_at"] = datetime.now().isoformat()
            settled += 1
    _save_json(ORGANIC_SALES_PATH, sales)
    return jsonify({"ok": True, "settled": settled})


# ── Shopify weekly sales importer (preview-only, deploy 1) ─────────
# Reads orders from Shopify for a given week, returns aggregated SKU
# preview. Writes nothing. The 'commit' endpoint will follow in deploy 2.
@app.route("/admin/shopify-debug")
@login_required
def shopify_debug():
    """Diagnostic endpoint: exchanges configured client credentials for an
    access token, then hits Shopify's /shop.json. Reports each step's result
    without leaking secrets.
    """
    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    store = os.environ.get("SHOPIFY_STORE", "").strip()
    return jsonify(shopify_importer.debug_shop(client_id, client_secret, store))


@app.route("/admin/shopify-import")
@login_required
def shopify_import_ui():
    """Tiny control page for triggering preview + commit from the browser.
    Avoids needing curl to POST. Renders inline HTML, no template file.
    """
    # Default to the Monday of the current week, Toronto time.
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        now_local = datetime.now()
    monday = now_local - timedelta(days=now_local.weekday())
    default_week = monday.strftime("%Y-%m-%d")

    from flask import render_template_string
    return render_template_string(SHOPIFY_IMPORT_HTML, default_week=default_week)


SHOPIFY_IMPORT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Shopify Import</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  p.lede { color: #666; margin-top: 0; }
  .controls { display: flex; gap: 0.5rem; align-items: center; margin: 1rem 0; }
  input[type=date] { padding: 0.4rem; font-size: 1rem; }
  button { padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer;
           border: 1px solid #888; background: #f5f5f5; border-radius: 4px; }
  button.primary { background: #2563eb; color: white; border-color: #1d4ed8; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  pre { background: #f7f7f7; padding: 1rem; border-radius: 4px; overflow: auto;
        max-height: 60vh; font-size: 0.85rem; }
  .status { padding: 0.5rem 0.75rem; margin: 0.5rem 0; border-radius: 4px; }
  .status.ok { background: #d1fae5; color: #065f46; }
  .status.err { background: #fee2e2; color: #991b1b; }
</style>
</head>
<body>
<h1>Shopify Weekly Import</h1>
<p class="lede">
  Preview shows what would be imported (read-only). Commit writes one sale row
  per SKU to Soma, attributes them to buyer "SOMA (Shopify)", and FIFO-deducts
  FG. Re-running Commit on the same week is safe — already-imported SKUs are
  skipped.
</p>

<div class="controls">
  <label for="week">Week (Monday):</label>
  <input type="date" id="week" value="{{ default_week }}">
  <button id="preview-btn">Preview</button>
  <button id="commit-btn" class="primary">Commit</button>
</div>

<div id="status"></div>
<pre id="result">(no result yet)</pre>

<script>
function run(endpoint, method) {
  const week = document.getElementById('week').value;
  if (!week) { setStatus('err', 'Pick a Monday first.'); return; }
  const url = endpoint + '?week=' + encodeURIComponent(week);
  setStatus('', 'Running ' + method + ' ' + url + ' …');
  document.getElementById('preview-btn').disabled = true;
  document.getElementById('commit-btn').disabled = true;
  fetch(url, { method: method, credentials: 'same-origin' })
    .then(r => r.json().then(data => ({ status: r.status, data: data })))
    .then(({status, data}) => {
      const ok = status >= 200 && status < 300;
      setStatus(ok ? 'ok' : 'err',
                (ok ? 'OK' : 'Error') + ' (HTTP ' + status + ')');
      document.getElementById('result').textContent =
        JSON.stringify(data, null, 2);
    })
    .catch(e => { setStatus('err', 'Network error: ' + e); })
    .finally(() => {
      document.getElementById('preview-btn').disabled = false;
      document.getElementById('commit-btn').disabled = false;
    });
}
function setStatus(cls, msg) {
  const el = document.getElementById('status');
  el.className = 'status ' + cls;
  el.textContent = msg;
}
document.getElementById('preview-btn').onclick =
  () => run('/admin/shopify-preview', 'GET');
document.getElementById('commit-btn').onclick = () => {
  if (!confirm('Commit Shopify orders for this week to Soma sales? ' +
               'This will deduct FG inventory.')) return;
  run('/admin/shopify-commit', 'POST');
};
</script>
</body>
</html>
"""


def _shopify_commit_for_week(week_id):
    """Worker shared by the admin commit route and the cron internal route.
    Returns (response_dict, http_status_code). Pure function — no decorators,
    no request access. Callers handle auth.
    """
    if not validate_week_id(week_id):
        return {"error": "Invalid or missing 'week' parameter"}, 400

    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    store = os.environ.get("SHOPIFY_STORE", "").strip()
    if not (client_id and client_secret and store):
        return {"error": "Shopify config missing in environment"}, 500

    # Step 1: fresh preview (don't trust cached state)
    try:
        recipes = _load_json(RECIPES_PATH, {})
        preview = shopify_importer.preview_week(
            week_id, recipes, client_id, client_secret, store
        )
    except Exception as e:
        logger.exception("Shopify commit: preview phase failed for %s", week_id)
        return {"error": str(e)}, 500

    # Step 2: validate — refuse to write partial data on suspicious input
    if preview["unparseable"]:
        return {
            "error": "Unparseable SKUs in this week; refusing to commit",
            "unparseable": preview["unparseable"],
        }, 400

    unmapped = [m for m in preview["matched"] if not m["exists_in_soma"]]
    if unmapped:
        return {
            "error": "Some Shopify SKUs do not map to existing Soma recipes; "
                     "refusing to commit",
            "unmapped": [{"sku": m["sku"], "attempted_key": m["soma_key"]}
                         for m in unmapped],
        }, 400

    if not preview["matched"]:
        return {
            "ok": True,
            "message": "No SKUs to commit for this week",
            "week_id": week_id,
            "preview": preview,
        }, 200

    # Step 3: process each matched SKU
    BUYER_NAME = "SOMA (Shopify)"
    CHANNEL = "shopify"
    sale_date = preview["range_end"][:10]  # YYYY-MM-DD (Sunday)
    # One order_id per weekly import — groups the per-SKU sale rows so they
    # appear as one transaction in Soma's UI rather than N independent sales.
    # Deterministic (not timestamped) so re-runs of the same week produce the
    # same id, helping audit trails.
    order_id = f"ORD-SHOPIFY-{week_id}"

    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])

    created = []
    skipped_idempotent = []
    errors = []

    for matched in preview["matched"]:
        sku_str = matched["sku"]
        soma_key = matched["soma_key"]
        brand = matched["brand"]
        recipe = matched["recipe"]
        fmt = matched["format"]
        quantity = matched["quantity"]
        order_ids = matched["order_ids"]

        # Idempotency: skip if already imported for this (channel, week, sku)
        already = next(
            (s for s in sales
             if s.get("channel") == CHANNEL
             and s.get("week_id") == week_id
             and s.get("sku_key") == soma_key),
            None,
        )
        if already:
            skipped_idempotent.append({
                "sku": sku_str,
                "existing_sale_id": already.get("id"),
                "existing_quantity": already.get("quantity"),
            })
            continue

        # FIFO-deduct (mirrors add_organic_sale's logic)
        candidates = [
            f for f in fg
            if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == soma_key
            and (f.get("quantity_remaining") or 0) > 0
        ]
        if not candidates:
            errors.append({"sku": sku_str, "error": "No FG inventory available"})
            continue

        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot", ""), e.get("id", "")))
        total_available = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
        if quantity > total_available:
            errors.append({
                "sku": sku_str,
                "error": f"Insufficient FG: requested {quantity}, available {total_available}",
            })
            continue

        remaining = quantity
        lot_summary = {}
        for entry in candidates:
            if remaining <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            if avail <= 0:
                continue
            take = min(avail, remaining)
            entry["quantity_remaining"] = avail - take
            remaining -= take
            lot = entry.get("lot", "")
            bucket = lot_summary.setdefault(lot, {
                "lot": lot, "quantity": 0, "fg_ids": [], "breakdown": [],
            })
            bucket["quantity"] += take
            bucket["fg_ids"].append(entry.get("id"))
            bucket["breakdown"].append({"fg_id": entry.get("id"), "quantity": take})
        sale_lots = list(lot_summary.values())

        # Inherit certification from the first deducted FG entry (consistent
        # within a SKU per existing convention).
        sale_cert = ""
        for lot_entry in sale_lots:
            for b in (lot_entry.get("breakdown") or []):
                target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                if target and target.get("certification"):
                    sale_cert = target["certification"]
                    break
            if sale_cert:
                break

        sale = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales) + len(created)),
            "order_id": order_id,  # groups all SKUs in this week's import
            "sku_key": soma_key,
            "brand": brand,
            "recipe": recipe,
            "format": fmt,
            "certification": sale_cert,
            "quantity": quantity,
            "lots": sale_lots,
            "fg_lot": (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
            "fg_id": (sale_lots[0]["fg_ids"][0]
                      if len(sale_lots) == 1 and len(sale_lots[0]["fg_ids"]) == 1
                      else ""),
            "buyer": BUYER_NAME,
            "sale_date": sale_date,
            "case_lot": "",
            "po_number": "",
            "created_at": datetime.now().isoformat(),
            # Shopify-import-specific fields
            "channel": CHANNEL,
            "week_id": week_id,
            "source_order_ids": order_ids,
        }
        sales.append(sale)
        created.append(sale)

    # Step 4: persist (only if anything actually changed)
    if created:
        _save_json(ORGANIC_SALES_PATH, sales)
        _save_json(ORGANIC_FG_PATH, fg)
        _add_contact("buyer", BUYER_NAME)  # ensure buyer is registered as a contact

    return {
        "ok": True,
        "week_id": week_id,
        "order_id": order_id,
        "sale_date": sale_date,
        "buyer": BUYER_NAME,
        "channel": CHANNEL,
        "created_count": len(created),
        "skipped_count": len(skipped_idempotent),
        "error_count": len(errors),
        "created": created,
        "skipped_idempotent": skipped_idempotent,
        "errors": errors,
    }, 200


@app.route("/admin/shopify-commit", methods=["POST"])
@login_required
def shopify_commit():
    """Commit Shopify orders for a given week as Soma sale records.

    Query:
        week=YYYY-MM-DD   — Monday of the target week

    Thin wrapper around _shopify_commit_for_week. Login-required for human
    operators using the /admin/shopify-import control page. The cron job
    uses /api/internal/shopify-import-week instead (auth via X-Internal-Key).
    """
    week_id = (request.args.get("week") or "").strip()
    body, status = _shopify_commit_for_week(week_id)
    return jsonify(body), status


@app.route("/api/internal/shopify-import-week", methods=["POST"])
def shopify_internal_import_last_week():
    """Internal endpoint called by the weekly Render Cron Job.

    Auth: X-Internal-Key header must match INTERNAL_API_KEY env var.

    Computes "last fully-completed week" based on America/Toronto local time
    (e.g. when run on Mon 2026-05-25 morning, imports Mon 2026-05-18 →
    Sun 2026-05-24), then runs the same commit logic as the admin route.

    Idempotent: re-running on the same day (or twice) is safe — the underlying
    commit logic skips SKUs already imported for that (channel, week_id).
    """
    provided = (request.headers.get("X-Internal-Key") or "").strip()
    internal_key = (os.environ.get("INTERNAL_API_KEY") or "").strip()
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(
        provided.encode(), internal_key.encode()
    ):
        return jsonify({"error": "Unauthorized"}), 401

    # Compute last Monday (Toronto time) — the start of the most recent
    # fully-completed Mon–Sun block.
    try:
        from zoneinfo import ZoneInfo
        today_local = datetime.now(ZoneInfo("America/Toronto")).date()
    except Exception:
        today_local = datetime.now().date()
    last_monday = today_local - timedelta(days=today_local.weekday() + 7)
    week_id = last_monday.strftime("%Y-%m-%d")

    logger.info("Shopify weekly cron firing for week_id=%s", week_id)
    body, status = _shopify_commit_for_week(week_id)
    # Surface the computed week in the cron log even on errors
    body["computed_week_id"] = week_id
    return jsonify(body), status


@app.route("/admin/shopify-preview")
@login_required
def shopify_preview():
    """Preview what would be imported from Shopify for a given week.

    Query param:
        week=YYYY-MM-DD   — Monday of the week to import (inclusive Mon→Sun)

    Returns a JSON document showing matched SKUs, unparseable SKUs, and
    line items skipped because they had no SKU value (bundle parents etc.).
    """
    week_id = (request.args.get("week") or "").strip()
    if not validate_week_id(week_id):
        return jsonify({
            "error": "Invalid or missing 'week' parameter; "
                     "expected YYYY-MM-DD (Monday of the target week)"
        }), 400

    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    store = os.environ.get("SHOPIFY_STORE", "").strip()
    if not (client_id and client_secret and store):
        return jsonify({
            "error": "SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, or "
                     "SHOPIFY_STORE not configured in environment"
        }), 500

    try:
        recipes = _load_json(RECIPES_PATH, {})
        result = shopify_importer.preview_week(
            week_id, recipes, client_id, client_secret, store
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("Shopify preview failed for week %s", week_id)
        return jsonify({"error": str(e)}), 500


# ── Clover weekly sales importer ─────────────────────────────────
# Parallel to the Shopify importer. Reads from Clover's REST API, applies
# the same per-SKU FIFO deduction + sale-row writing as Shopify imports,
# attributes sales to buyer "SOMA (Clover)" with channel "clover".

@app.route("/admin/clover-debug")
@login_required
def clover_debug():
    """Diagnostic endpoint: hits Clover's /merchants/{mId} endpoint with the
    configured token and reports what came back. Use to isolate auth /
    merchant-ID issues without going through the orders code.
    """
    token = os.environ.get("CLOVER_API_TOKEN", "").strip()
    merchant_id = os.environ.get("CLOVER_MERCHANT_ID", "").strip()
    api_base = os.environ.get("CLOVER_API_BASE", "").strip() or None
    return jsonify(clover_importer.debug_merchant(token, merchant_id, api_base))


@app.route("/admin/clover-preview")
@login_required
def clover_preview():
    """Preview what would be imported from Clover for a given week."""
    week_id = (request.args.get("week") or "").strip()
    if not validate_week_id(week_id):
        return jsonify({
            "error": "Invalid or missing 'week' parameter; "
                     "expected YYYY-MM-DD (Monday of the target week)"
        }), 400

    token = os.environ.get("CLOVER_API_TOKEN", "").strip()
    merchant_id = os.environ.get("CLOVER_MERCHANT_ID", "").strip()
    api_base = os.environ.get("CLOVER_API_BASE", "").strip() or None
    if not (token and merchant_id):
        return jsonify({
            "error": "CLOVER_API_TOKEN or CLOVER_MERCHANT_ID not configured"
        }), 500

    try:
        recipes = _load_json(RECIPES_PATH, {})
        result = clover_importer.preview_week(
            week_id, recipes, token, merchant_id, api_base=api_base
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("Clover preview failed for week %s", week_id)
        return jsonify({"error": str(e)}), 500


def _clover_commit_for_week(week_id):
    """Worker shared by /admin/clover-commit and the cron internal route.
    Returns (response_dict, http_status_code). Pure function — no decorators,
    no request access. Callers handle auth.

    Mirrors _shopify_commit_for_week exactly except for: which importer
    module is called, which env vars are read, the channel name, the
    buyer name, and the order_id prefix. Kept as a deliberate duplicate
    (rather than an abstracted helper) so each channel's behavior is
    readable end-to-end in one place.
    """
    if not validate_week_id(week_id):
        return {"error": "Invalid or missing 'week' parameter"}, 400

    token = os.environ.get("CLOVER_API_TOKEN", "").strip()
    merchant_id = os.environ.get("CLOVER_MERCHANT_ID", "").strip()
    api_base = os.environ.get("CLOVER_API_BASE", "").strip() or None
    if not (token and merchant_id):
        return {"error": "Clover config missing in environment"}, 500

    try:
        recipes = _load_json(RECIPES_PATH, {})
        preview = clover_importer.preview_week(
            week_id, recipes, token, merchant_id, api_base=api_base
        )
    except Exception as e:
        logger.exception("Clover commit: preview phase failed for %s", week_id)
        return {"error": str(e)}, 500

    if preview["unparseable"]:
        return {
            "error": "Unparseable SKUs in this week; refusing to commit",
            "unparseable": preview["unparseable"],
        }, 400

    unmapped = [m for m in preview["matched"] if not m["exists_in_soma"]]
    if unmapped:
        return {
            "error": "Some Clover SKUs do not map to existing Soma recipes; "
                     "refusing to commit",
            "unmapped": [{"sku": m["sku"], "attempted_key": m["soma_key"]}
                         for m in unmapped],
        }, 400

    if not preview["matched"]:
        return {
            "ok": True,
            "message": "No SKUs to commit for this week",
            "week_id": week_id,
            "preview": preview,
        }, 200

    BUYER_NAME = "SOMA (Clover)"
    CHANNEL = "clover"
    sale_date = preview["range_end"][:10]
    order_id = f"ORD-CLOVER-{week_id}"

    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])

    created = []
    skipped_idempotent = []
    errors = []

    for matched in preview["matched"]:
        sku_str = matched["sku"]
        soma_key = matched["soma_key"]
        brand = matched["brand"]
        recipe = matched["recipe"]
        fmt = matched["format"]
        quantity = matched["quantity"]
        order_ids = matched["order_ids"]

        already = next(
            (s for s in sales
             if s.get("channel") == CHANNEL
             and s.get("week_id") == week_id
             and s.get("sku_key") == soma_key),
            None,
        )
        if already:
            skipped_idempotent.append({
                "sku": sku_str,
                "existing_sale_id": already.get("id"),
                "existing_quantity": already.get("quantity"),
            })
            continue

        candidates = [
            f for f in fg
            if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == soma_key
            and (f.get("quantity_remaining") or 0) > 0
        ]
        if not candidates:
            errors.append({"sku": sku_str, "error": "No FG inventory available"})
            continue

        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot", ""), e.get("id", "")))
        total_available = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
        if quantity > total_available:
            errors.append({
                "sku": sku_str,
                "error": f"Insufficient FG: requested {quantity}, available {total_available}",
            })
            continue

        remaining = quantity
        lot_summary = {}
        for entry in candidates:
            if remaining <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            if avail <= 0:
                continue
            take = min(avail, remaining)
            entry["quantity_remaining"] = avail - take
            remaining -= take
            lot = entry.get("lot", "")
            bucket = lot_summary.setdefault(lot, {
                "lot": lot, "quantity": 0, "fg_ids": [], "breakdown": [],
            })
            bucket["quantity"] += take
            bucket["fg_ids"].append(entry.get("id"))
            bucket["breakdown"].append({"fg_id": entry.get("id"), "quantity": take})
        sale_lots = list(lot_summary.values())

        sale_cert = ""
        for lot_entry in sale_lots:
            for b in (lot_entry.get("breakdown") or []):
                target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                if target and target.get("certification"):
                    sale_cert = target["certification"]
                    break
            if sale_cert:
                break

        sale = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales) + len(created)),
            "order_id": order_id,
            "sku_key": soma_key,
            "brand": brand,
            "recipe": recipe,
            "format": fmt,
            "certification": sale_cert,
            "quantity": quantity,
            "lots": sale_lots,
            "fg_lot": (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
            "fg_id": (sale_lots[0]["fg_ids"][0]
                      if len(sale_lots) == 1 and len(sale_lots[0]["fg_ids"]) == 1
                      else ""),
            "buyer": BUYER_NAME,
            "sale_date": sale_date,
            "case_lot": "",
            "po_number": "",
            "created_at": datetime.now().isoformat(),
            "channel": CHANNEL,
            "week_id": week_id,
            "source_order_ids": order_ids,
        }
        sales.append(sale)
        created.append(sale)

    if created:
        _save_json(ORGANIC_SALES_PATH, sales)
        _save_json(ORGANIC_FG_PATH, fg)
        _add_contact("buyer", BUYER_NAME)

    return {
        "ok": True,
        "week_id": week_id,
        "order_id": order_id,
        "sale_date": sale_date,
        "buyer": BUYER_NAME,
        "channel": CHANNEL,
        "created_count": len(created),
        "skipped_count": len(skipped_idempotent),
        "error_count": len(errors),
        "created": created,
        "skipped_idempotent": skipped_idempotent,
        "errors": errors,
    }, 200


@app.route("/admin/clover-commit", methods=["POST"])
@login_required
def clover_commit():
    """Commit Clover orders for a given week as Soma sale records.
    Thin wrapper around _clover_commit_for_week. Login-required for human
    operators; cron uses /api/internal/clover-import-week instead.
    """
    week_id = (request.args.get("week") or "").strip()
    body, status = _clover_commit_for_week(week_id)
    return jsonify(body), status


@app.route("/api/internal/clover-import-week", methods=["POST"])
def clover_internal_import_last_week():
    """Internal endpoint called by the weekly Render Cron Job for Clover.
    Auth: X-Internal-Key header matched against INTERNAL_API_KEY env var.
    Computes "last fully-completed week" in Toronto local time, then runs
    the commit logic.
    """
    provided = (request.headers.get("X-Internal-Key") or "").strip()
    internal_key = (os.environ.get("INTERNAL_API_KEY") or "").strip()
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(
        provided.encode(), internal_key.encode()
    ):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        from zoneinfo import ZoneInfo
        today_local = datetime.now(ZoneInfo("America/Toronto")).date()
    except Exception:
        today_local = datetime.now().date()
    last_monday = today_local - timedelta(days=today_local.weekday() + 7)
    week_id = last_monday.strftime("%Y-%m-%d")

    logger.info("Clover weekly cron firing for week_id=%s", week_id)
    body, status = _clover_commit_for_week(week_id)
    body["computed_week_id"] = week_id
    return jsonify(body), status


@app.route("/admin/clover-import")
@login_required
def clover_import_ui():
    """Tiny control page for triggering Clover preview + commit from the
    browser — same shape as /admin/shopify-import.
    """
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        now_local = datetime.now()
    monday = now_local - timedelta(days=now_local.weekday())
    default_week = monday.strftime("%Y-%m-%d")

    from flask import render_template_string
    # Reuse the Shopify control-page template but rewrite the endpoints
    # and headings via simple string substitution. Keeps both UIs in sync.
    clover_html = (SHOPIFY_IMPORT_HTML
                   .replace("Shopify Weekly Import", "Clover Weekly Import")
                   .replace('SOMA (Shopify)', 'SOMA (Clover)')
                   .replace("'/admin/shopify-preview'", "'/admin/clover-preview'")
                   .replace("'/admin/shopify-commit'", "'/admin/clover-commit'")
                   .replace("Commit Shopify orders", "Commit Clover orders"))
    return render_template_string(clover_html, default_week=default_week)


if __name__ == "__main__":
    app.run(debug=True, port=5000)


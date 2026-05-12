"""
Soma Bone Broth - Production Scheduler v5
Dashboard-based navigation, daily production with FINISH/START,
label generation, traceability records, master CCP reference.
"""

from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, session, redirect, url_for
from datetime import datetime, timedelta
from pdf_engine import generate_weekly_schedule_pdf, generate_daily_package_pdf, generate_filled_checklist_pdf, generate_label_pdf
from functools import wraps
import json
import os
import re
import zipfile
import io
from ripe_orders import ripe_orders_bp, init_paths as _ripe_init_paths

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "soma-bone-broth-2026-change-me")
app.register_blueprint(ripe_orders_bp)

# Session lifetime — 4 hours. After this the user must log in again.
from datetime import timedelta as _timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = _timedelta(hours=4)
app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS can't read the cookie
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF mitigation

APP_PASSWORD = os.environ.get("APP_PASSWORD", "soma2026")
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "")  # empty = feature disabled
VESSELS = ["K1", "K2", "K3", "115L"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
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
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            recipes = json.load(f)
        # Auto-migrate in-memory (disk unchanged until explicit save).
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
    with open(RECIPES_PATH, "w") as f:
        json.dump(recipes, f, indent=2)

def load_schedule(week_id):
    path = os.path.join(SCHEDULES_DIR, week_id + ".json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def save_schedule(week_id, data):
    path = os.path.join(SCHEDULES_DIR, week_id + ".json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def list_schedules():
    if not os.path.exists(SCHEDULES_DIR):
        return []
    files = sorted([f.replace(".json", "") for f in os.listdir(SCHEDULES_DIR) if f.endswith(".json")], reverse=True)
    return files

def load_checklist(week_id, day_idx):
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def save_checklist_data(week_id, day_idx, data):
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_ccp_master():
    if os.path.exists(CCP_MASTER_PATH):
        with open(CCP_MASTER_PATH, "r") as f:
            return json.load(f)
    return DEFAULT_CCP_SECTIONS

def save_ccp_master(sections):
    with open(CCP_MASTER_PATH, "w") as f:
        json.dump(sections, f, indent=2)

def load_recipe_order():
    if os.path.exists(RECIPE_ORDER_PATH):
        with open(RECIPE_ORDER_PATH, "r") as f:
            return json.load(f)
    return None

def save_recipe_order(order):
    with open(RECIPE_ORDER_PATH, "w") as f:
        json.dump(order, f, indent=2)

def get_current_week_id():
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
    # Collapse any double spaces / trailing commas left after stripping
    original = re.sub(r"\s{2,}", " ", original).rstrip(",").strip()
    if not original:
        return None

    # Detect "per L" items first — these keep their amount but are never halved.
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

    # Split out process hints after a comma or em-dash/hyphen
    # e.g. "2.5 kg Celery, diced" -> name="Celery", process="diced"
    process = ""
    body = original
    # Try em-dash / en-dash / " - " separator first
    for sep in [" — ", " – ", " - "]:
        if sep in body:
            parts = body.split(sep, 1)
            body = parts[0].strip()
            process = parts[1].strip()
            break
    # Then try comma (only if not already split and comma is late in the string)
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

    # Resolve unit
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

    # Clean int display
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
        # Display clean int when whole number
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
            # Exact match — auto-convert
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
                    # Apply smart upgrades to freshly-parsed items too
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
FORMAT_PREFIX_CANONICAL = {
    "SS": "SS",
    "FZ": "FZ",
    "BB": "BB",
}

# Any <letters>[sep]<number>ML suffix — case-insensitive.
# Separator can be nothing, a dash, or whitespace.
FORMAT_RE = re.compile(r"\b([A-Za-z]{1,4})[\s-]*(\d+)\s*ML\b", re.IGNORECASE)


# Suffix regex used when stripping a trailing format from a recipe name.
# Matches "<separator><letters><separator><digits>ML" at end of string.
_FORMAT_SUFFIX_RE = re.compile(
    r"[\s\-]*[A-Za-z]{1,4}[\s\-]*\d+\s*ML\s*$",
    re.IGNORECASE,
)


def _strip_format_suffix(name):
    """Remove ALL trailing format suffixes from a recipe name.
    Repeats until no more remove — handles double-appended legacy names like
    'Beef SS-750ML SS-750ML' -> 'Beef'."""
    if not name:
        return ""
    prev = None
    out = name
    while prev != out:
        prev = out
        out = _FORMAT_SUFFIX_RE.sub("", out).rstrip(" -")
    return out


def build_display_name(recipe_data, recipe_name=""):
    """Canonical display string used by every UI surface.

    Shape: '{brand}-{name-without-format}-{format}'
    Example: 'Ripe-Big Kahuna-SS-750ML'

    If brand is missing, drops that segment. If format is missing, uses the
    raw recipe_name as-is.

    Accepts either a recipe dict with keys brand/format plus a separate
    recipe_name, OR a single dict containing 'name' field for convenience.
    """
    if not isinstance(recipe_data, dict):
        return recipe_name or ""
    brand = (recipe_data.get("brand") or "").strip()
    fmt = _normalize_format((recipe_data.get("format") or "").strip())
    name = (recipe_name or recipe_data.get("name") or "").strip()

    # Strip any format suffix(es) from the name
    core = _strip_format_suffix(name)
    # If stripping removed everything (e.g. name was literally "SS-750ML"),
    # fall back to the original name
    if not core:
        core = name

    parts = []
    if brand:
        parts.append(brand)
    if core:
        parts.append(core)
    if fmt:
        parts.append(fmt)
    return "-".join(parts) if parts else name


def _normalize_format(text):
    """Turn any 'SS-473ML', 'ss473ml', 'SS 473 ml', etc. into canonical 'SS-473ML'."""
    if not text:
        return ""
    m = FORMAT_RE.search(text)
    if not m:
        return text.strip().upper()
    prefix_raw = m.group(1)
    canonical_prefix = FORMAT_PREFIX_CANONICAL.get(prefix_raw.upper(), prefix_raw.upper())
    return f"{canonical_prefix}-{m.group(2)}ML"


def _detect_format_in_text(text):
    """Return the canonical format found in text, or '' if none."""
    if not text:
        return ""
    m = FORMAT_RE.search(text)
    if not m:
        return ""
    return _normalize_format(m.group(0))


def parse_recipe_pdf_text(text):
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

    # Append format to name for unique storage key — case-insensitive check
    if recipe["format"] and not name.upper().endswith(recipe["format"].upper()):
        name = name + " " + recipe["format"]

    # Parse recipe body
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
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    if data.get("password") == APP_PASSWORD:
        session.permanent = True   # enables PERMANENT_SESSION_LIFETIME
        session["authenticated"] = True
        session["logged_in_at"] = datetime.now().isoformat()
        return jsonify({"success": True})
    return jsonify({"error": "Invalid password"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# ── PWA manifest ──────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")


# ── Page routes ────────────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/create-schedule")
@login_required
def create_schedule_page():
    return render_template("create_schedule.html")

@app.route("/weekly-schedule")
@login_required
def weekly_schedule_page():
    return render_template("weekly_view.html")

@app.route("/daily-production/<week_id>/<int:day_idx>")
@login_required
@require_valid_week
@require_valid_day
def daily_production_page(week_id, day_idx):
    return render_template("daily_production.html", week_id=week_id, day_idx=day_idx)

@app.route("/checklist/<week_id>/<int:day_idx>")
@login_required
@require_valid_week
@require_valid_day
def checklist_page(week_id, day_idx):
    return render_template("checklist.html", week_id=week_id, day_idx=day_idx)

@app.route("/recipes")
@login_required
def recipes_page():
    return render_template("recipes.html")

@app.route("/contacts")
@login_required
def contacts_page():
    return render_template("contacts.html")

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

@app.route("/certifications")
@login_required
def certifications_page():
    """Organic & compliance document storage page."""
    return render_template("certifications.html")

@app.route("/api/certifications", methods=["GET"])
@login_required
def list_certifications():
    cert_dir = os.path.join(DATA_DIR, "certifications")
    os.makedirs(cert_dir, exist_ok=True)
    meta_path = os.path.join(cert_dir, "meta.json")
    meta = _load_json(meta_path, [])
    return jsonify(meta)

@app.route("/api/certifications/upload", methods=["POST"])
@login_required
def upload_certification():
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
    buyer_sales = [
        s for s in sales
        if (s.get("buyer") or "").strip().lower() == buyer_name.lower()
    ]

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

    # ── By location ──────────────────────────────────────────────────────────
    # Get registered locations from buyer record
    buyers_list = _load_buyers()
    buyer_rec   = next((b for b in buyers_list
                        if (b.get("name") or "").strip().lower() == buyer_name.lower()), None)
    registered_locs = [l.get("name","").strip() for l in (buyer_rec or {}).get("locations", []) if l.get("name","").strip()]

    # Build per-location sales breakdown
    by_location = defaultdict(lambda: {"revenue": 0.0, "cases": 0, "units": 0, "orders": set()})
    for s in buyer_sales:
        loc = (s.get("location_name") or s.get("delivery_label") or "").strip() or "Pickup / Other"
        by_location[loc]["revenue"] += _revenue(s)
        by_location[loc]["cases"]   += _cases(s)
        by_location[loc]["units"]   += int(s.get("quantity") or 0)
        by_location[loc]["orders"].add(s.get("order_id") or s.get("id"))

    # Order locations: registered ones first (in their defined order), then any others
    loc_names_ordered = []
    for rl in registered_locs:
        if rl in by_location:
            loc_names_ordered.append(rl)
    for loc in by_location:
        if loc not in loc_names_ordered:
            loc_names_ordered.append(loc)

    locations = [
        {
            "name":    loc,
            "revenue": round(by_location[loc]["revenue"], 2),
            "cases":   by_location[loc]["cases"],
            "units":   by_location[loc]["units"],
            "orders":  len(by_location[loc]["orders"]),
        }
        for loc in loc_names_ordered
    ]
    has_locations = len(locations) > 1  # only show tabs if >1 location

    # ── By month (also per-location) ──────────────────────────────────────────
    # Store location on each monthly entry so JS can filter
    by_month_loc = defaultdict(lambda: defaultdict(lambda: {"revenue": 0.0, "cases": 0, "units": 0, "orders": set()}))
    for s in buyer_sales:
        d   = (s.get("sale_date") or "")[:7]
        loc = (s.get("location_name") or s.get("delivery_label") or "").strip() or "Pickup / Other"
        if not d:
            continue
        by_month_loc[d][loc]["revenue"] += _revenue(s)
        by_month_loc[d][loc]["cases"]   += _cases(s)
        by_month_loc[d][loc]["units"]   += int(s.get("quantity") or 0)
        by_month_loc[d][loc]["orders"].add(s.get("order_id") or s.get("id"))

    # Build monthly with per-location breakdown embedded
    monthly = []
    for m in months_sorted:
        entry = {
            "month":   m,
            "label":   _dt.strptime(m, "%Y-%m").strftime("%b %Y"),
            "revenue": round(by_month[m]["revenue"], 2),
            "cases":   by_month[m]["cases"],
            "units":   by_month[m]["units"],
            "orders":  len(by_month[m]["orders"]),
            "by_location": {
                loc: {
                    "revenue": round(by_month_loc[m][loc]["revenue"], 2),
                    "cases":   by_month_loc[m][loc]["cases"],
                    "units":   by_month_loc[m][loc]["units"],
                    "orders":  len(by_month_loc[m][loc]["orders"]),
                }
                for loc in by_month_loc[m]
            }
        }
        monthly.append(entry)

    # ── By SKU (also per-location) ────────────────────────────────────────────
    by_sku = defaultdict(lambda: {"recipe": "", "format": "", "units": 0,
                                   "cases": 0, "revenue": 0.0, "months": defaultdict(int),
                                   "by_location": defaultdict(lambda: {"units": 0, "cases": 0, "revenue": 0.0})})
    for s in buyer_sales:
        sk  = s.get("sku_key") or s.get("recipe") or "Unknown"
        loc = (s.get("location_name") or s.get("delivery_label") or "").strip() or "Pickup / Other"
        by_sku[sk]["recipe"]  = s.get("recipe", "")
        by_sku[sk]["format"]  = s.get("format", "")
        by_sku[sk]["units"]  += int(s.get("quantity") or 0)
        by_sku[sk]["cases"]  += _cases(s)
        by_sku[sk]["revenue"]+= _revenue(s)
        by_sku[sk]["by_location"][loc]["units"]   += int(s.get("quantity") or 0)
        by_sku[sk]["by_location"][loc]["cases"]   += _cases(s)
        by_sku[sk]["by_location"][loc]["revenue"] += _revenue(s)
        m = (s.get("sale_date") or "")[:7]
        if m:
            by_sku[sk]["months"][m] += _cases(s)

    skus = sorted(
        [{"sku": k,
          "recipe":  v["recipe"],
          "format":  v["format"],
          "units":   v["units"],
          "cases":   v["cases"],
          "revenue": round(v["revenue"], 2),
          "monthly_cases": dict(v["months"]),
          "by_location": {loc: {kk: round(vv,2) if kk=="revenue" else vv
                                for kk,vv in ldata.items()}
                          for loc, ldata in v["by_location"].items()},
         }
         for k, v in by_sku.items()],
        key=lambda x: -x["revenue"]
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
        "locations":     locations,
        "has_locations": has_locations,
        "totals": {
            "revenue":          round(total_revenue, 2),
            "cases":            total_cases,
            "units":            total_units,
            "orders":           total_orders,
            "avg_order_revenue": avg_order_rev,
            "avg_order_cases":   avg_order_cases,
        },
        "monthly":  monthly,
        "yearly":   yearly,
        "by_sku":   skus,
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

    # Build a lookup: (buyer_name.lower(), sku_key) → price
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

    # Aggregate
    by_buyer = {}
    for sale in sales:
        buyer = (sale.get("buyer") or "Unknown").strip()
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
            "by_sku":  [{**s, "revenue": round(s["revenue"],2)} for s in skus],
        })
    return jsonify({"buyers": result, "period": period})

@app.route("/buyers/<bid>/edit")
@login_required
def buyer_edit_page(bid):
    buyers = _load_buyers()
    buyer = next((b for b in buyers if b["id"] == bid), None)
    if not buyer:
        return "Buyer not found", 404

    # Group catalog by Brand → Format (SS → FZ → BB → Other) → Recipe name
    flat = _all_sku_catalog()
    FORMAT_ORDER = ["SS", "FZ", "BB"]
    FORMAT_LABELS = {"SS": "Shelf Stable", "FZ": "Frozen", "BB": "Back Bar"}

    # Collect brands in sorted order
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
    return render_template("master_ccp.html")

@app.route("/traceability")
@login_required
def traceability_page():
    return render_template("traceability.html")

@app.route("/production-tracker")
@login_required
def production_tracker_page():
    return render_template("production_tracker.html")


# ── Recipe API ─────────────────────────────────────────────────────────
@app.route("/api/recipes", methods=["GET"])
@login_required
def get_recipes():
    return jsonify(load_recipes())

@app.route("/api/recipes", methods=["POST"])
@login_required
def add_recipe():
    data = request.json
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    recipes = load_recipes()
    recipes[name] = data.get("data", {})
    save_recipes(recipes)
    return jsonify({"success": True})

@app.route("/api/recipes/<path:name>", methods=["GET"])
@login_required
def get_recipe(name):
    recipes = load_recipes()
    if name in recipes:
        return jsonify({"name": name, "data": recipes[name]})
    return jsonify({"error": "Recipe not found"}), 404

@app.route("/api/recipes/<path:name>", methods=["PUT"])
@login_required
def update_recipe(name):
    """Update an existing recipe. If the body's 'name' differs from the URL
    name, this is treated as a rename.

    Cascades any name/brand/format change to all downstream records:
    FG inventory, sales, production runs, schedules, sku_meta, buyers,
    and notifies the Ripe portal to update soma_sku_key.
    """
    data = request.json or {}
    recipes = load_recipes()
    new_name = (data.get("name") or name).strip()
    recipe_data = data.get("data", {})

    if name not in recipes:
        return jsonify({"error": f"Recipe '{name}' not found"}), 404

    if new_name != name and new_name in recipes:
        return jsonify({
            "error": f"A different recipe named '{new_name}' already exists. "
                     f"Choose a different name or delete the other one first."
        }), 409

    # Capture old identity before saving
    old_recipe = recipes[name]
    old_brand  = (old_recipe.get("brand") or "").strip()
    old_format = _normalize_format((old_recipe.get("format") or "").strip())
    old_sku_key = _sku_key(old_brand, name, old_format)

    new_brand  = (recipe_data.get("brand") or "").strip()
    new_format = _normalize_format((recipe_data.get("format") or "").strip())
    new_sku_key = _sku_key(new_brand, new_name, new_format)

    # Save the recipe first
    if new_name != name:
        del recipes[name]
    recipes[new_name] = recipe_data
    save_recipes(recipes)

    # ── Cascade if anything identity-related changed ─────────────────────────
    identity_changed = (new_name != name or new_brand != old_brand or new_sku_key != old_sku_key)
    cascade = {}

    if identity_changed:
        # 1. Finished goods
        fg = _load_json(ORGANIC_FG_PATH, [])
        n = 0
        for e in fg:
            if (e.get("recipe") or "").strip() == name:
                e["recipe"] = new_name
                if new_brand: e["brand"] = new_brand
                if new_format: e["format"] = new_format
                n += 1
        if n: _save_json(ORGANIC_FG_PATH, fg)
        cascade["finished_goods"] = n

        # 2. Sales records
        sales = _load_json(ORGANIC_SALES_PATH, [])
        n = 0
        for s in sales:
            if (s.get("recipe") or "").strip() == name:
                s["recipe"] = new_name
                if new_brand: s["brand"] = new_brand
                if new_format: s["format"] = new_format
                s["sku_key"] = _sku_key(s.get("brand", new_brand), new_name, s.get("format", new_format))
                n += 1
        if n: _save_json(ORGANIC_SALES_PATH, sales)
        cascade["sales"] = n

        # 3. Production runs
        runs = _load_json(ORGANIC_RUNS_PATH, [])
        n = 0
        for r in runs:
            if (r.get("recipe") or "").strip() == name:
                r["recipe"] = new_name
                n += 1
        if n: _save_json(ORGANIC_RUNS_PATH, runs)
        cascade["runs"] = n

        # 4. sku_meta — rename the key
        if old_sku_key != new_sku_key:
            meta = _load_json(SKU_META_PATH, {})
            if old_sku_key in meta:
                meta[new_sku_key] = meta.pop(old_sku_key)
                _save_json(SKU_META_PATH, meta)
                cascade["sku_meta"] = 1

        # 5. Buyers — update sku_key/recipe/display on assigned SKUs
        buyers = _load_buyers()
        n = 0
        for buyer in buyers:
            for sku in (buyer.get("skus") or []):
                if (sku.get("sku_key") or "") == old_sku_key:
                    sku["sku_key"] = new_sku_key
                    sku["recipe"] = new_name
                    if new_brand: sku["brand"] = new_brand
                    if new_format: sku["format"] = new_format
                    sku["display"] = _sku_display(sku.get("brand", new_brand), new_name, sku.get("format", new_format))
                    n += 1
        if n: _save_buyers(buyers)
        cascade["buyers_skus"] = n

        # 6. Schedule files — recipe names as slot values
        n = 0
        try:
            if os.path.isdir(SCHEDULES_DIR):
                for fname in os.listdir(SCHEDULES_DIR):
                    if not fname.endswith(".json"): continue
                    fpath = os.path.join(SCHEDULES_DIR, fname)
                    with open(fpath) as f: sched = json.load(f)
                    dirty = False
                    def _walk(obj):
                        nonlocal dirty
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if isinstance(v, str) and v == name:
                                    obj[k] = new_name; dirty = True
                                else: _walk(v)
                        elif isinstance(obj, list):
                            for i, item in enumerate(obj):
                                if isinstance(item, str) and item == name:
                                    obj[i] = new_name; dirty = True
                                else: _walk(item)
                    _walk(sched)
                    if dirty:
                        with open(fpath, "w") as f: json.dump(sched, f, indent=2)
                        n += 1
        except Exception as e:
            logger.warning("Schedule cascade failed: %s", e)
        cascade["schedules"] = n

        # 7. Notify Ripe portal to update soma_sku_key (best-effort)
        if old_sku_key != new_sku_key:
            ripe_url = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
            ikey = os.environ.get("INTERNAL_API_KEY", "")
            if ripe_url and ikey:
                try:
                    import urllib.request as _ur
                    req = _ur.Request(
                        f"{ripe_url}/api/internal/rename-sku-key",
                        data=json.dumps({"old_sku_key": old_sku_key, "new_sku_key": new_sku_key}).encode(),
                        headers={"X-Internal-Key": ikey, "Content-Type": "application/json"},
                        method="POST",
                    )
                    with _ur.urlopen(req, timeout=6) as resp:
                        cascade["ripe_products"] = json.loads(resp.read()).get("updated", 0)
                except Exception as e:
                    logger.warning("Could not notify Ripe of sku_key rename: %s", e)
                    cascade["ripe_products"] = "unreachable"

    return jsonify({"success": True, "name": new_name, "cascade": cascade})

@app.route("/api/recipes/<path:name>", methods=["DELETE"])
@login_required
def delete_recipe(name):
    recipes = load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404

    # Block deletion if any completed organic runs reference this recipe —
    # those runs are part of the traceability record and need the recipe
    # context for the tracker, traceability page, and label re-generation.
    # Suggest archiving instead.
    completed_refs = []
    try:
        runs = _load_json(ORGANIC_RUNS_PATH, [])
        for r in runs:
            if r.get("status") == "completed" and r.get("recipe") == name:
                completed_refs.append({
                    "run_id": r.get("id"),
                    "week_id": r.get("week_id"),
                    "day_idx": r.get("day_idx"),
                    "vessel": r.get("vessel"),
                })
    except Exception:
        pass
    if completed_refs:
        return jsonify({
            "error": (f"Cannot delete '{name}' — it has been used in "
                      f"{len(completed_refs)} completed organic production run(s). "
                      f"Deleting would orphan those traceability records. "
                      f"Use Archive instead to hide it from new schedules while "
                      f"preserving history."),
            "completed_runs": completed_refs,
        }), 409

    del recipes[name]
    save_recipes(recipes)
    return jsonify({"success": True})


def _schedules_using_recipe(recipe_name):
    """Return list of (week_id, day_idx, vessel) tuples where recipe_name is scheduled."""
    refs = []
    if not os.path.exists(SCHEDULES_DIR):
        return refs
    for fn in os.listdir(SCHEDULES_DIR):
        if not fn.endswith(".json"):
            continue
        week_id = fn[:-5]
        try:
            with open(os.path.join(SCHEDULES_DIR, fn)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        sched = (data or {}).get("schedule") or {}
        for d_idx_str, day in sched.items():
            if not isinstance(day, dict):
                continue
            for vessel, name in day.items():
                if name == recipe_name:
                    try:
                        d_idx = int(d_idx_str)
                    except ValueError:
                        d_idx = -1
                    refs.append({"week_id": week_id, "day_idx": d_idx, "vessel": vessel})
    return refs


@app.route("/api/recipes/<path:name>/duplicate", methods=["POST"])
@login_required
def duplicate_recipe(name):
    """Duplicate a recipe with a new name. Body: {new_name}.
    Copies all data including ingredients/yield/brand/format. If the source
    has a photo, the photo file is copied to a new filename keyed to the
    duplicate's name."""
    data = request.json or {}
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"error": "new_name required"}), 400

    recipes = load_recipes()
    if name not in recipes:
        return jsonify({"error": "Source recipe not found"}), 404
    if new_name in recipes:
        return jsonify({"error": "A recipe with that name already exists"}), 400

    # Deep copy via JSON round-trip
    new_data = json.loads(json.dumps(recipes[name]))

    # Copy photo file if present
    src_photo = new_data.get("photo")
    if src_photo:
        src_path = os.path.join(PHOTOS_DIR, src_photo)
        if os.path.exists(src_path):
            ext = os.path.splitext(src_photo)[1].lower() or ".jpg"
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', new_name)
            new_photo = safe + ext
            new_path = os.path.join(PHOTOS_DIR, new_photo)
            try:
                import shutil
                shutil.copy2(src_path, new_path)
                new_data["photo"] = new_photo
            except OSError:
                # If copy fails, drop the photo reference rather than fail the whole duplicate
                new_data.pop("photo", None)
        else:
            new_data.pop("photo", None)

    # Mark as not archived even if source was
    new_data["archived"] = False

    recipes[new_name] = new_data
    save_recipes(recipes)
    return jsonify({"success": True, "name": new_name, "data": new_data})


@app.route("/api/recipes/<path:name>/archive", methods=["POST"])
@login_required
def archive_recipe(name):
    """Mark a recipe as archived. It stays in storage so old schedules and
    tracker entries still resolve, but it's hidden from the new-schedule
    recipe picker. Returns 200 with a list of schedule references so the
    frontend can warn the user about active uses."""
    recipes = load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    recipes[name]["archived"] = True
    save_recipes(recipes)
    refs = _schedules_using_recipe(name)
    return jsonify({"success": True, "schedule_refs": refs})


@app.route("/api/recipes/<path:name>/unarchive", methods=["POST"])
@login_required
def unarchive_recipe(name):
    """Restore an archived recipe to active."""
    recipes = load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    recipes[name]["archived"] = False
    save_recipes(recipes)
    return jsonify({"success": True})


@app.route("/api/recipes/migrate-all", methods=["POST"])
@login_required
def migrate_all_recipes():
    """Force-persist structured-ingredient migration to disk for all recipes.
    Performs smart pack conversion using organic/tracking_modes.json, then
    archives that file (renamed to .archived) so future runs don't reapply it.
    Returns a report of which recipes were changed and which items need review."""
    # Read raw file (bypass auto-migration in load_recipes so we can count changes)
    raw_recipes = {}
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            raw_recipes = json.load(f)

    tracking_modes = _load_tracking_modes()

    changed_recipes = []
    review_items = []  # list of {recipe, section, index, name, reason}

    for name, data in raw_recipes.items():
        changed = migrate_recipe_ingredients(data, tracking_modes)
        if changed:
            changed_recipes.append(name)
        for section in INGREDIENT_SECTIONS:
            for idx, item in enumerate(data.get(section, [])):
                if isinstance(item, dict) and item.get("needs_review"):
                    review_items.append({
                        "recipe": name,
                        "section": section,
                        "index": idx,
                        "name": item.get("name", ""),
                        "unit": item.get("unit", ""),
                        "amount": item.get("amount", 0),
                        "process": item.get("process", ""),
                    })

    save_recipes(raw_recipes)

    # Archive tracking_modes.json so it isn't reapplied on subsequent migrations.
    # File location: see _load_tracking_modes; supports both new and legacy paths.
    inv_dir = globals().get("INVENTORY_DIR") or os.path.join(DATA_DIR, "inventory")
    candidates = [
        os.path.join(inv_dir, "tracking_modes.json"),
        os.path.join(DATA_DIR, "organic", "tracking_modes.json"),
    ]
    for tm_path in candidates:
        if os.path.exists(tm_path) and tracking_modes:
            try:
                os.rename(tm_path, tm_path + ".archived")
            except OSError:
                pass

    return jsonify({
        "success": True,
        "total": len(raw_recipes),
        "changed": len(changed_recipes),
        "changed_recipes": changed_recipes,
        "review_count": len(review_items),
        "review_items": review_items,
    })

# Photo upload
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

@app.route("/api/recipes/<path:name>/photo", methods=["POST"])
@login_required
def upload_recipe_photo(name):
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400
    file = request.files["photo"]
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png"]:
        ext = ".jpg"
    filename = safe + ext
    filepath = os.path.join(PHOTOS_DIR, filename)
    file.save(filepath)
    recipes = load_recipes()
    if name in recipes:
        recipes[name]["photo"] = filename
        save_recipes(recipes)
    return jsonify({"success": True, "photo": filename})

@app.route("/api/photos/<filename>")
@login_required
def serve_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)

# Grouped recipes for dropdowns
@app.route("/api/recipes/grouped", methods=["GET"])
@login_required
def get_recipes_grouped():
    """Return recipes grouped by brand for the schedule picker.
    Excludes archived recipes by default. Pass ?include_archived=1 to include them."""
    include_archived = request.args.get("include_archived", "0") in ("1", "true", "yes")
    recipes = load_recipes()
    order = load_recipe_order()
    groups = {}
    for name, data in recipes.items():
        if not include_archived and data.get("archived"):
            continue
        brand = data.get("brand", "Other")
        if not brand:
            brand = "Other"
        if brand not in groups:
            groups[brand] = []
        # Display shows format so the picker distinguishes SKUs with the same base name
        # e.g. "Soma-Liquid Gold-SS-876ML" vs "Soma-Liquid Gold-FZ-750ML"
        # The stored value is just the recipe name — format comes from the recipe's own field
        display = build_display_name(data, name)
        groups[brand].append({
            "name": name,
            "format": data.get("format", ""),
            "yield": data.get("yield", ""),
            "display": display,
            "certification": data.get("certification", ""),
        })
    # Apply stored order if available, otherwise sort SS first
    if order:
        ordered_groups = {}
        for brand in order.get("brand_order", []):
            if brand in groups:
                recipe_order = order.get("recipe_order", {}).get(brand, [])
                if recipe_order:
                    ordered = []
                    for rname in recipe_order:
                        match = [r for r in groups[brand] if r["name"] == rname]
                        if match:
                            ordered.append(match[0])
                    for r in groups[brand]:
                        if r not in ordered:
                            ordered.append(r)
                    ordered_groups[brand] = ordered
                else:
                    ordered_groups[brand] = groups[brand]
        for brand in groups:
            if brand not in ordered_groups:
                ordered_groups[brand] = groups[brand]
        return jsonify(ordered_groups)
    else:
        for brand in groups:
            groups[brand].sort(key=lambda x: (0 if "SS" in x.get("format", "") else 1, x["name"]))
        return jsonify(groups)

@app.route("/api/recipes/order", methods=["POST"])
@login_required
def update_recipe_order():
    data = request.json
    save_recipe_order(data)
    return jsonify({"success": True})

# Upload recipe — accepts PDF file or JSON text
@app.route("/api/recipes/upload", methods=["POST"])
@login_required
def upload_recipe():
    # Handle PDF file upload
    if request.files and "file" in request.files:
        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "No file selected"}), 400
        try:
            import pdfplumber
            pdf = pdfplumber.open(file)
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            pdf.close()
        except Exception as e:
            return jsonify({"error": f"Could not read PDF: {str(e)}"}), 400
        if not text.strip():
            return jsonify({"error": "No text found in PDF"}), 400
        parsed = parse_recipe_pdf_text(text)
        if not parsed:
            return jsonify({"error": "Could not parse recipe from PDF"}), 400
        recipes = load_recipes()
        recipes[parsed["name"]] = parsed["data"]
        save_recipes(recipes)
        return jsonify({"success": True, "name": parsed["name"], "data": parsed["data"]})

    # Handle JSON text upload (legacy)
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "No text provided"}), 400
    parsed = parse_recipe_pdf_text(text)
    if not parsed:
        return jsonify({"error": "Could not parse recipe"}), 400
    recipes = load_recipes()
    recipes[parsed["name"]] = parsed["data"]
    save_recipes(recipes)
    return jsonify({"success": True, "name": parsed["name"], "data": parsed["data"]})


# Upload recipe via JSON (manual add)
@app.route("/api/recipes/upload-json", methods=["POST"])
@login_required
def upload_recipe_json():
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    recipe_data = data.get("data", {})
    recipes = load_recipes()
    recipes[name] = recipe_data
    save_recipes(recipes)
    return jsonify({"success": True, "name": name})


# ── Schedule API ───────────────────────────────────────────────────────
@app.route("/api/schedule/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def get_schedule(week_id):
    data = load_schedule(week_id)
    if data:
        return jsonify(data)
    return jsonify({"schedule": None, "notes": ""})

@app.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    return jsonify(list_schedules())

@app.route("/api/schedule/<week_id>", methods=["DELETE"])
@login_required
@require_valid_week
def delete_schedule(week_id):
    path = os.path.join(SCHEDULES_DIR, week_id + ".json")
    if os.path.exists(path):
        os.unlink(path)
    # Remove scheduled (uncompleted) organic runs for this week.
    # Completed runs are preserved — they're traceability data.
    try:
        runs = _load_json(ORGANIC_RUNS_PATH, [])
        before = len(runs)
        runs = [r for r in runs
                if not (r.get("week_id") == week_id and r.get("status") != "completed")]
        if len(runs) != before:
            _save_json(ORGANIC_RUNS_PATH, runs)
    except Exception:
        pass
    return jsonify({"success": True})


# ── Generate PDFs ──────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
@login_required
def generate_pdfs():
    data = request.json
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

        # Weekly schedule PDF
        filename = "Weekly_Schedule.pdf"
        filepath = os.path.join(week_pdf_dir, filename)
        generate_weekly_schedule_pdf(filepath, week_start, schedule, recipes, notes, logo_path)
        generated.append(filename)

        # Daily production packages
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
        # Schedule is already saved above, so organic check can still run
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        # Always check for organic recipes in the schedule
        try:
            _check_organic_schedule(week_id, schedule)
        except Exception:
            pass


# ── PDF downloads ──────────────────────────────────────────────────────
@app.route("/api/pdf/<week_id>/<filename>", methods=["GET"])
@login_required
@require_valid_week
def download_pdf(week_id, filename):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    return send_from_directory(week_pdf_dir, filename, as_attachment=True)

@app.route("/api/pdfs/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def list_pdfs(week_id):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify([])
    files = sorted(os.listdir(week_pdf_dir))
    return jsonify([f for f in files if f.endswith(".pdf")])

@app.route("/api/pdfs/<week_id>/download-all", methods=["GET"])
@login_required
@require_valid_week
def download_all_pdfs(week_id):
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

    # Halve yield
    if halved.get("yield"):
        try:
            halved["yield"] = round(int(halved["yield"]) / 2)
        except (ValueError, TypeError):
            pass

    # Migrate first (safety: in case a legacy recipe slipped through)
    migrate_recipe_ingredients(halved)

    # Halve each structured ingredient
    for section in INGREDIENT_SECTIONS:
        items = halved.get(section, [])
        if isinstance(items, list):
            halved[section] = [halve_ingredient(it) for it in items]

    halved["_halved"] = True
    return halved


# ── Daily Production API ──────────────────────────────────────────────
@app.route("/api/daily-production/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
@require_valid_week
@require_valid_day
def get_daily_production(week_id, day_idx):
    schedule_data = load_schedule(week_id)
    recipes = load_recipes()
    checklist = load_checklist(week_id, day_idx)

    today_schedule = {}
    prev_schedule = {}

    if schedule_data and schedule_data.get("schedule"):
        today_key = str(day_idx)
        today_schedule = schedule_data["schedule"].get(today_key, {})

        if day_idx > 0:
            prev_schedule = schedule_data["schedule"].get(str(day_idx - 1), {})

    # Cross-week: Monday FINISH needs previous week's Sunday (day 6)
    if day_idx == 0:
        prev_week_start = datetime.strptime(week_id, "%Y-%m-%d") - timedelta(days=7)
        prev_week_id = prev_week_start.strftime("%Y-%m-%d")
        prev_week_data = load_schedule(prev_week_id)
        if prev_week_data and prev_week_data.get("schedule"):
            prev_schedule = prev_week_data["schedule"].get("6", {})

    # FINISH = previous day's assigned recipe (it was started yesterday, finishing today)
    # START = today's assigned recipe (what we're starting/prepping today)
    finish_kettles = {}
    start_kettles = {}

    for vessel in VESSELS:
        # FINISH: previous day's recipe (started yesterday, finishing today)
        prev_recipe_name = prev_schedule.get(vessel, "")
        if prev_recipe_name and prev_recipe_name.strip():
            prev_recipe_data = recipes.get(prev_recipe_name, {})
            if prev_recipe_data:
                details = _halve_for_115L(prev_recipe_data) if vessel == "115L" else prev_recipe_data
                finish_kettles[vessel] = {
                    "recipe": prev_recipe_name,
                    "details": details,
                    "halved": vessel == "115L",
                }

        # START: today's assigned recipe (starting today, will be finished tomorrow)
        today_recipe_name = today_schedule.get(vessel, "")
        if today_recipe_name and today_recipe_name.strip():
            today_recipe_data = recipes.get(today_recipe_name, {})
            if today_recipe_data:
                details = _halve_for_115L(today_recipe_data) if vessel == "115L" else today_recipe_data
                start_kettles[vessel] = {
                    "recipe": today_recipe_name,
                    "details": details,
                    "halved": vessel == "115L",
                }

    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    date = week_start + timedelta(days=day_idx)
    prev_date = date - timedelta(days=1)

    # Get daily notes for this day
    daily_notes = ""
    if schedule_data and schedule_data.get("daily_notes"):
        daily_notes = schedule_data["daily_notes"].get(str(day_idx), "")

    # LOT# = expiry date (production date + 365 days) in ddmmyy format.
    # prev_lot is used when generating labels for the recipe being finished today,
    # which was started yesterday → expiry = yesterday + 365 days.
    prev_expiry = prev_date + timedelta(days=365)
    today_expiry = date + timedelta(days=365)

    return jsonify({
        "date": date.strftime("%A, %d/%m/%Y"),
        "day_name": DAYS[day_idx],
        "prev_date": prev_date.strftime("%d/%m/%Y"),
        "prev_lot": prev_expiry.strftime("%d%m%y"),
        "lot": today_expiry.strftime("%d%m%y"),
        "today_lot": today_expiry.strftime("%d%m%y"),
        "finish": finish_kettles,
        "start": start_kettles,
        "checklist": checklist,
        "notes": schedule_data.get("notes", "") if schedule_data else "",
        "daily_notes": daily_notes,
    })

@app.route("/api/daily-production/<week_id>/<int:day_idx>/save", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def save_daily_production(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    save_checklist_data(week_id, day_idx, data)
    # Process any organic runs scheduled on the previous day —
    # the produced amounts entered today are the finish of yesterday's runs.
    warnings = []
    try:
        warnings = _check_organic_completion(week_id, day_idx, data) or []
    except Exception:
        pass
    return jsonify({"success": True, "warnings": warnings})


# ── Label Generation ──────────────────────────────────────────────────
@app.route("/api/label", methods=["POST"])
@login_required
def generate_label():
    data = request.json
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

    # Build the canonical 'recipe-format' portion using the shared helper.
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
@app.route("/api/checklist/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
@require_valid_week
@require_valid_day
def get_checklist_route(week_id, day_idx):
    data = load_checklist(week_id, day_idx)
    schedule_data = load_schedule(week_id)
    day_info = {}
    if schedule_data and schedule_data.get("schedule"):
        day_key = str(day_idx)
        if day_key in schedule_data["schedule"]:
            day_info = schedule_data["schedule"][day_key]
    return jsonify({"checklist": data, "day_info": day_info})

@app.route("/api/checklist/<week_id>/<int:day_idx>", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def save_checklist_route(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    save_checklist_data(week_id, day_idx, data)
    return jsonify({"success": True})

@app.route("/api/checklist/<week_id>/<int:day_idx>/complete", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def complete_checklist(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    data["completed"] = True
    save_checklist_data(week_id, day_idx, data)

    schedule_data = load_schedule(week_id)
    day_info = {}
    if schedule_data and schedule_data.get("schedule"):
        day_key = str(day_idx)
        if day_key in schedule_data["schedule"]:
            day_info = schedule_data["schedule"][day_key]

    active_vessels = []
    for vessel in VESSELS:
        recipe = day_info.get(vessel, "")
        if recipe:
            active_vessels.append({"vessel": vessel, "recipe": recipe})

    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    date = week_start + timedelta(days=day_idx)

    logo_path = os.path.join(app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None

    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    os.makedirs(week_pdf_dir, exist_ok=True)
    filename = DAYS[day_idx] + "_Completed_Checklist.pdf"
    pdf_path = os.path.join(week_pdf_dir, filename)
    generate_filled_checklist_pdf(pdf_path, date, active_vessels, data, logo_path)

    # Check if any organic runs need completing
    warnings = []
    try:
        warnings = _check_organic_completion(week_id, day_idx, data) or []
    except Exception:
        pass

    return jsonify({"success": True, "filename": filename, "warnings": warnings})


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
    # Check produced and bb_produced
    for field in ["produced", "bb_produced"]:
        prod = checklist_data.get(field, {})
        for key, val in prod.items():
            if val and str(val).strip() and str(val).strip() != "0":
                return True
    if checklist_data.get("kettles_end", "").strip() and checklist_data.get("kettles_end", "").strip() != "0":
        return True
    return False

@app.route("/api/checklist-status/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def checklist_status(week_id):
    statuses = {}
    for d_idx in range(7):
        data = load_checklist(week_id, d_idx)
        if data and data.get("completed"):
            statuses[str(d_idx)] = "completed"
        elif data and _has_meaningful_data(data):
            statuses[str(d_idx)] = "in_progress"
        else:
            statuses[str(d_idx)] = "not_started"
    return jsonify(statuses)


# ── Master CCP ─────────────────────────────────────────────────────────
@app.route("/api/ccp-master", methods=["GET"])
@login_required
def get_ccp_master():
    return jsonify(load_ccp_master())

@app.route("/api/ccp-master", methods=["POST"])
@login_required
def update_ccp_master():
    data = request.json
    save_ccp_master(data)
    return jsonify({"success": True})


# ── Traceability ──────────────────────────────────────────────────────
WEEKLY_SIGNOFFS_PATH = os.path.join(DATA_DIR, "weekly_signoffs.json")


def _load_weekly_signoffs():
    return _load_json(WEEKLY_SIGNOFFS_PATH, {})


def _save_weekly_signoffs(data):
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


@app.route("/api/traceability/<week_id>/summary", methods=["GET"])
@login_required
@require_valid_week
def get_week_summary(week_id):
    """Return a structured summary of a week's production for the HOO review modal.
    Includes: total production, CCP completion, notes flagged, inconsistencies.
    """
    schedule_data = load_schedule(week_id) or {}
    sched = (schedule_data.get("schedule") or {}) if schedule_data else {}
    recipes_data = load_recipes()

    days_summary = []
    total_produced = {}
    all_notes = []
    ccp_flags = []
    missing_signoffs = []

    for d_idx in range(7):
        cl = load_checklist(week_id, d_idx)
        if not cl or not cl.get("completed"):
            continue

        day_info = sched.get(str(d_idx), {}) or {}
        day_name = DAYS[d_idx]

        # Scheduled vessels
        scheduled = {v: day_info.get(v, "").strip() for v in VESSELS if day_info.get(v, "").strip()}

        # Production recorded
        produced = cl.get("produced", {}) or {}
        bb_produced = cl.get("bb_produced", {}) or {}

        # Aggregate production
        day_prod = {}
        for v, recipe in scheduled.items():
            if recipe:
                qty = int(produced.get(v) or 0)
                if qty:
                    day_prod[recipe] = day_prod.get(recipe, 0) + qty
                    total_produced[recipe] = total_produced.get(recipe, 0) + qty

        # Notes
        notes = (cl.get("notes") or "").strip()
        if notes:
            all_notes.append({"day": day_name, "note": notes})

        # Check CCP sections — look for any items explicitly marked No/False
        sections = cl.get("sections", {}) or {}
        day_ccp_issues = []
        for sec_key, sec_data in sections.items():
            if isinstance(sec_data, dict):
                for item_key, item_val in sec_data.items():
                    if item_val is False or item_val == "no" or item_val == "No":
                        day_ccp_issues.append(f"{sec_key}: {item_key}")
        if day_ccp_issues:
            ccp_flags.append({"day": day_name, "issues": day_ccp_issues})

        # Check kitchen staff sign-off (the only required sign-off in current workflow)
        kitchen = (cl.get("signoff_kitchen") or "").strip()
        if scheduled and not kitchen:
            missing_signoffs.append(day_name)

        days_summary.append({
            "day":           day_name,
            "day_idx":       d_idx,
            "scheduled":     list(scheduled.values()),
            "produced":      day_prod,
            "notes":         notes,
            "kitchen_signoff": kitchen,
            "has_ccp_flags": len(day_ccp_issues) > 0,
        })

    # Build inconsistency flags
    flags = []
    if missing_signoffs:
        flags.append(f"Missing kitchen sign-off: {', '.join(missing_signoffs)}")
    if ccp_flags:
        for cf in ccp_flags:
            flags.append(f"{cf['day']} CCP issue: {'; '.join(cf['issues'][:3])}")

    return jsonify({
        "week_id": week_id,
        "days": days_summary,
        "total_produced": total_produced,
        "notes": all_notes,
        "flags": flags,
        "ccp_flags": ccp_flags,
        "all_clear": len(flags) == 0 and len(all_notes) == 0,
    })


@app.route("/api/traceability", methods=["GET"])
@login_required
def get_traceability():
    weeks = list_schedules()
    signoffs = _load_weekly_signoffs()
    records = []
    for week_id in weeks:
        week_record = {"week_id": week_id, "days": []}
        schedule_data = load_schedule(week_id)
        for d_idx in range(7):
            cl = load_checklist(week_id, d_idx)
            if cl and cl.get("completed"):
                day_info = {}
                if schedule_data and schedule_data.get("schedule"):
                    day_info = schedule_data["schedule"].get(str(d_idx), {})
                certification = ""
                if day_info:
                    recipes = load_recipes()
                    for v in VESSELS:
                        rname = day_info.get(v, "")
                        if rname and rname in recipes:
                            cert = recipes[rname].get("certification", "")
                            if cert:
                                certification = cert
                                break
                week_record["days"].append({
                    "day_idx": d_idx,
                    "day_name": DAYS[d_idx],
                    "completed": True,
                    "last_updated": cl.get("last_updated", ""),
                    "certification": certification,
                })
        if week_record["days"]:
            # Annotate with completion state + HOO signoff
            state, complete_idxs, incomplete_idxs = _week_completion_state(week_id)
            week_record["completion_state"] = state
            week_record["scheduled_complete_days"] = complete_idxs
            week_record["scheduled_incomplete_days"] = [
                {"day_idx": di, "day_name": DAYS[di]} for di in incomplete_idxs
            ]
            week_record["hoo_signoff"] = signoffs.get(week_id) or None
            records.append(week_record)
    return jsonify(records)


@app.route("/api/traceability/<week_id>/<int:day_idx>", methods=["DELETE"])
@login_required
@require_valid_week
@require_valid_day
def delete_traceability_record(week_id, day_idx):
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    if os.path.exists(path):
        os.unlink(path)
        pdf_path = os.path.join(PDF_DIR, week_id, DAYS[day_idx] + "_Completed_Checklist.pdf")
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
        return jsonify({"success": True})
    return jsonify({"error": "Record not found"}), 404


@app.route("/api/weekly-signoff/<week_id>", methods=["POST"])
@login_required
@require_valid_week
def sign_off_week(week_id):
    """Head of Operations confirms a week's production records.

    Requires that all days that had a schedule entry have completed checklists.
    Body: {name, notes (optional)}
    """
    data = request.json or {}
    name = (data.get("name") or "").strip()
    notes = (data.get("notes") or "").strip()
    if not name:
        return jsonify({"error": "Name required to sign off"}), 400

    state, _, incomplete = _week_completion_state(week_id)
    if state == "none":
        return jsonify({"error": "No production scheduled for this week"}), 400
    if state == "partial":
        labels = ", ".join(DAYS[di] for di in incomplete)
        return jsonify({
            "error": f"Cannot sign off — {len(incomplete)} scheduled day(s) still incomplete: {labels}"
        }), 400

    signoffs = _load_weekly_signoffs()
    signoffs[week_id] = {
        "name": name,
        "notes": notes,
        "signed_at": datetime.now().isoformat(),
    }
    _save_weekly_signoffs(signoffs)
    return jsonify({"success": True, "signoff": signoffs[week_id]})


@app.route("/api/weekly-signoff/<week_id>", methods=["DELETE"])
@login_required
@require_valid_week
def unsign_week(week_id):
    """Reverse a weekly sign-off (HOO can undo)."""
    signoffs = _load_weekly_signoffs()
    if week_id in signoffs:
        del signoffs[week_id]
        _save_weekly_signoffs(signoffs)
    return jsonify({"success": True})


# ── Production Tracker ────────────────────────────────────────────────
# ── Production tracker helpers ────────────────────────────────────────
# Buckets that appear in the tracker breakdown. Ordered for stacked-bar rendering
# (bottom to top): SS sizes first, then Frozen, Other, Kettle's End, BB.
TRACKER_BUCKETS = ["SS-876ML", "SS-750ML", "SS-473ML", "FZ", "Other", "Kettles End", "BB"]


def _classify_format(recipe_format):
    """Map any format string (canonical or not) to a bucket.
    Returns one of: 'SS-876ML', 'SS-750ML', 'SS-473ML', 'FZ', 'Other'.

    Normalizes first so 'ss-750ml', 'SS750ML', 'SS 750 ML', 'SS-750 ml' all
    match the same bucket. Any recognizable format with an SS prefix and a
    750/876/473 ml size hits its bucket; any FZ-prefixed format goes to FZ;
    anything else is Other.
    """
    if not recipe_format:
        return "Other"
    # Use the shared parser regex to pull out prefix + size
    m = FORMAT_RE.search(recipe_format)
    if not m:
        return "Other"
    prefix = m.group(1).upper()
    size = m.group(2)
    if prefix == "SS":
        if size == "876":
            return "SS-876ML"
        if size == "750":
            return "SS-750ML"
        if size == "473":
            return "SS-473ML"
        return "Other"     # unknown SS size (e.g. 250, 1000)
    if prefix == "FZ":
        return "FZ"
    return "Other"         # BB-*, iQ-*, or any other prefix


def _empty_buckets():
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
    # Monday → look back to last week's Sunday
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

    # Resolve recipes (cached across week)
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

    # BB produced (all into BB regardless of format)
    bb = cl.get("bb_produced") or {}
    for vessel_id, amount in bb.items():
        try:
            amt = int(amount)
        except (ValueError, TypeError):
            continue
        if amt > 0:
            buckets["BB"] += amt

    # Kettle's End
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


@app.route("/api/production-tracker/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def get_production_tracker(week_id):
    """Return per-day bucketed totals for a week. Each day contains per-bucket
    counts plus legacy flat keys (produced/bb/kettles_end) for backward compat."""
    schedule_data = load_schedule(week_id) or {}
    recipes = load_recipes()

    daily_totals = []
    for d_idx in range(7):
        buckets, has_data = _day_buckets(
            week_id, d_idx,
            recipes_cache=recipes, schedule_cache=schedule_data,
        )
        entry = {
            "day_idx": d_idx,
            "day_name": DAYS[d_idx],
            "buckets": dict(buckets),
            "total": _bucket_total(buckets),
            "has_data": has_data,
        }
        daily_totals.append(entry)
    return jsonify(daily_totals)


@app.route("/api/production-tracker/<week_id>/other-details", methods=["GET"])
@login_required
@require_valid_week
def get_tracker_other_details(week_id):
    """Diagnostic: return every production entry in this week that classified
    as 'Other', with the reason. Uses the SAME attribution logic as the tracker:
    looks up the PREVIOUS day's recipe (what finished today), not today's start."""
    recipes = load_recipes()
    rows = []
    for d_idx in range(7):
        cl = load_checklist(week_id, d_idx)
        if not cl or not cl.get("produced"):
            continue
        prev_day_sched = _get_previous_day_schedule(week_id, d_idx)
        for vessel_id, amount in (cl.get("produced") or {}).items():
            try:
                amt = int(amount)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            recipe_name = prev_day_sched.get(vessel_id, "")
            recipe_data = recipes.get(recipe_name) if recipe_name else None
            fmt = (recipe_data or {}).get("format", "")
            bucket = _classify_format(fmt)
            if bucket != "Other":
                continue
            if not recipe_name:
                reason = "no_schedule_entry"
                detail = f"No recipe scheduled yesterday for {vessel_id} — can't classify today's production"
            elif recipe_data is None:
                reason = "recipe_not_found"
                detail = f"Yesterday's scheduled recipe '{recipe_name}' no longer exists"
            elif not fmt:
                reason = "recipe_missing_format"
                detail = f"Recipe '{recipe_name}' has no format field set"
            else:
                reason = "unrecognized_format"
                detail = f"Format '{fmt}' is not SS-876/750/473ML or FZ-*"
            rows.append({
                "day_idx": d_idx,
                "day_name": DAYS[d_idx],
                "vessel": vessel_id,
                "amount": amt,
                "scheduled_recipe": recipe_name or None,
                "recipe_format": fmt or None,
                "reason": reason,
                "detail": detail,
            })
    return jsonify(rows)


@app.route("/api/production-tracker/month/<year_month>", methods=["GET"])
@login_required
def get_production_tracker_month(year_month):
    """Return weekly totals for a given month (format: YYYY-MM)."""
    if not re.match(r'^\d{4}-\d{2}$', year_month):
        return jsonify({"error": "Invalid month format, use YYYY-MM"}), 400
    try:
        year, month = int(year_month[:4]), int(year_month[5:7])
        # Find all Mondays in this month
        from calendar import monthrange
        _, days_in_month = monthrange(year, month)
        first_day = datetime(year, month, 1)
        last_day = datetime(year, month, days_in_month)

        # Find the Monday on or before the 1st
        start_monday = first_day - timedelta(days=first_day.weekday())
        weeks = []
        current = start_monday
        while current <= last_day:
            wid = current.strftime("%Y-%m-%d")
            end_date = current + timedelta(days=6)
            totals = _week_totals(wid)
            totals["buckets"] = {b: totals.get(b, 0) for b in TRACKER_BUCKETS}
            totals["week_id"] = wid
            totals["label"] = current.strftime("%b %d") + " - " + end_date.strftime("%b %d")
            # Strip raw bucket keys now that they're nested under 'buckets'
            for b in TRACKER_BUCKETS:
                totals.pop(b, None)
            weeks.append(totals)
            current += timedelta(days=7)
        return jsonify(weeks)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/production-tracker/year/<int:year>", methods=["GET"])
@login_required
def get_production_tracker_year(year):
    """Return monthly totals for a given year."""
    if year < 2020 or year > 2099:
        return jsonify({"error": "Invalid year"}), 400
    months = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m in range(1, 13):
        from calendar import monthrange
        _, days_in_month = monthrange(year, m)
        first_day = datetime(year, m, 1)
        last_day = datetime(year, m, days_in_month)
        start_monday = first_day - timedelta(days=first_day.weekday())

        month_buckets = _empty_buckets()
        current = start_monday
        seen_weeks = set()
        while current <= last_day:
            wid = current.strftime("%Y-%m-%d")
            if wid not in seen_weeks:
                seen_weeks.add(wid)
                wt = _week_totals(wid)
                for bucket in TRACKER_BUCKETS:
                    month_buckets[bucket] += wt.get(bucket, 0)
            current += timedelta(days=7)

        month_total = {
            "buckets": dict(month_buckets),
            "total": _bucket_total(month_buckets),
            "month": m,
            "label": month_names[m - 1],
        }
        months.append(month_total)
    return jsonify(months)


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
INVENTORY_DIR = os.path.join(DATA_DIR, "inventory")
ORGANIC_DIR = INVENTORY_DIR  # legacy alias — see comment above
ORGANIC_RAW_PATH = os.path.join(INVENTORY_DIR, "raw_materials.json")
ORGANIC_RUNS_PATH = os.path.join(INVENTORY_DIR, "production_runs.json")
ORGANIC_FG_PATH = os.path.join(INVENTORY_DIR, "finished_goods.json")
ORGANIC_SALES_PATH = os.path.join(INVENTORY_DIR, "sales.json")
ORGANIC_CONTACTS_PATH = os.path.join(INVENTORY_DIR, "contacts.json")
COMPANY_INFO_PATH = os.path.join(INVENTORY_DIR, "company_info.json")

_DEFAULT_COMPANY_INFO = {
    "name": "Soma Bone Broth",
    "address": "",
    "city": "",
    "phone": "",
    "email": "",
    "website": "",
    "registration": "",
    "notes": "",
    "ripe_inventory_buffer": 12,   # units withheld from Ripe's visible stock
    # Ripe order rules — editable in Company Settings
    "ss_min_cases_delivery": 40,   # min SS cases for delivery orders
    "fzbb_small_lead_days":  3,    # min days notice for FZ/BB ≤ threshold
    "fzbb_large_lead_days":  7,    # min days notice for FZ/BB ≥ threshold
    "fzbb_large_threshold":  8,    # cases at which large lead time applies
}
SKU_META_PATH = os.path.join(INVENTORY_DIR, "sku_meta.json")  # PAR levels + prices
# Manual inventory adjustments log (additions and subtractions outside of
# production runs and sales). Each entry records what changed, why, and
# which LOT(s) were drained or created. Used for audit traceability.
ADJUSTMENTS_PATH = os.path.join(INVENTORY_DIR, "adjustments.json")
AUDITS_PATH      = os.path.join(INVENTORY_DIR, "audits.json")
EQUIPMENT_PATH   = os.path.join(DATA_DIR, "equipment.json")
COGS_PATH        = os.path.join(DATA_DIR, "cogs.json")
# Camera-scan request log: per-day rolling counter for daily-limit enforcement
# plus an audit trail of every scan (success or failure).
SUPPLIERS_PATH = os.path.join(INVENTORY_DIR, "suppliers.json")
RM_RECEIPT_PHOTOS_DIR = os.path.join(DATA_DIR, "rm_receipt_photos")
os.makedirs(RM_RECEIPT_PHOTOS_DIR, exist_ok=True)
# Raw material section organization. User-defined sections + per-ingredient
# assignment. Pre-seeded with the 6-section structure on first load.
RM_SECTIONS_PATH = os.path.join(INVENTORY_DIR, "rm_sections.json")


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


def _load_json(path, default=None):
    """Read a JSON file. Returns `default` (or [] if not given) when the file
    is missing.

    CONCURRENCY: This is a plain read with no file locking. The Soma app uses
    JSON files for all persistent state and assumes a single-user, low-
    concurrency environment. Two simultaneous writes are LAST-WRITE-WINS and
    can lose data — but in practice the kitchen has one user editing at a
    time, so this constraint is acceptable. If multi-user concurrent editing
    becomes a requirement, switch to SQLite (file-locked) or a real DB.
    """
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    """Write JSON. See _load_json's docstring for the concurrency caveat —
    no locking, last-write-wins on simultaneous writes."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Organic Page ──────────────────────────────────────────────────────
@app.route("/organic")
@login_required
def organic_page():
    return render_template("organic.html")


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


def _jar_volume_liters(recipe_data):
    """Parse the jar volume in liters from a recipe's format string.
    'SS-750ML' / 'FZ-750ML' / 'BB-750ML' -> 0.75, 'SS-876ML' -> 0.876,
    'SS-473ML' -> 0.473. Returns None if unparseable (caller falls back)."""
    fmt = (recipe_data.get("format") or "").upper()
    m = re.search(r"(\d+)\s*ML", fmt)
    if m:
        return int(m.group(1)) / 1000.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*L\b", fmt)
    if m:
        return float(m.group(1))
    return None


@app.route("/api/organic/ingredients", methods=["GET"])
@login_required
def organic_ingredients():
    """Return unique (name, unit) pairs pulled from organic-certified recipes.

    Each entry: {name, unit, recipe_amount}
    - recipe_amount is informational (first-seen amount for this name+unit)
    - Skips needs_review items.
    - Custom user-added ingredients merged in.
    """
    recipes = load_recipes()
    derived = {}  # key: (name_lower, unit) -> entry

    for rname, rdata in recipes.items():
        # Universal inventory: pull ingredients from every recipe regardless of
        # certification status. Each recipe contributes its tracked ingredients.
        if rdata.get("archived"):
            continue
        for section in INGREDIENT_SECTIONS:
            for item in rdata.get(section, []):
                if not is_structured_ingredient(item):
                    continue
                if item.get("needs_review"):
                    continue
                ing_name = (item.get("name") or "").strip()
                unit = (item.get("unit") or "").strip()
                # Untracked ingredients (e.g. water) are unlimited — they
                # appear on recipe cards but are never tracked as raw materials.
                if is_untracked_ingredient(ing_name):
                    continue
                try:
                    amount = float(item.get("amount") or 0)
                except (ValueError, TypeError):
                    amount = 0
                if not ing_name or not unit or amount <= 0:
                    continue
                if amount == int(amount):
                    amount = int(amount)
                key = (ing_name.lower(), unit)
                if key not in derived:
                    derived[key] = {
                        "name": ing_name,
                        "unit": unit,
                        "recipe_amount": amount,
                    }

    all_items = list(derived.values())

    if not all_items:
        all_items = [dict(i, recipe_amount=0) for i in ORGANIC_INGREDIENTS_FALLBACK]

    # Merge custom user-added items (never duplicate by (name, unit))
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    existing = {(i["name"].lower(), i.get("unit", "")) for i in all_items}
    for c in custom:
        name = c.get("name", "")
        unit = c.get("unit", "")
        if not name or not unit:
            continue
        key = (name.lower(), unit)
        if key in existing:
            continue
        all_items.append({"name": name, "unit": unit, "recipe_amount": 0})
        existing.add(key)

    all_items.sort(key=lambda x: (x["name"], x["unit"]))
    return jsonify(all_items)


# ── Raw material section organization ─────────────────────────────────
# User-defined sections (created via /api/organic/raw-materials/sections)
# replace the previous hardcoded heuristic. Each ingredient is explicitly
# assigned to a section by name+unit key. Unassigned ingredients land in
# a special "Unassigned" section that's always shown last.

# Default section list seeded on first load. User can rename/reorder/add/
# delete after that — these are just a starting point.
DEFAULT_RM_SECTIONS = [
    {"id": "bones",     "name": "Bones"},
    {"id": "fresh_veg", "name": "Fresh Vegetables"},
    {"id": "herbs",     "name": "Herbs"},
    {"id": "adjuncts",  "name": "Adjuncts, Packs, & Juice"},
    {"id": "mushrooms", "name": "Mushrooms"},
    {"id": "spices_other", "name": "Spices & Other"},
]

# Special "Unassigned" section. Not stored in user list; surfaced
# separately so users can see what still needs classifying.
UNASSIGNED_SECTION_ID = "_unassigned"
UNASSIGNED_SECTION_NAME = "Unassigned"


def _ingredient_section_key(name, unit):
    """Storage key for ingredient assignment lookups."""
    return f"{(name or '').strip()}|{(unit or '').strip()}"


def _load_rm_sections():
    """Load the sections+assignments file, seeding defaults on first access."""
    if not os.path.exists(RM_SECTIONS_PATH):
        seed = {
            "sections": [
                {"id": s["id"], "name": s["name"], "order": i}
                for i, s in enumerate(DEFAULT_RM_SECTIONS)
            ],
            "assignments": {},
        }
        _save_json(RM_SECTIONS_PATH, seed)
        return seed

    data = _load_json(RM_SECTIONS_PATH, None)
    if not isinstance(data, dict):
        # Corrupt or unexpected — re-seed
        seed = {
            "sections": [
                {"id": s["id"], "name": s["name"], "order": i}
                for i, s in enumerate(DEFAULT_RM_SECTIONS)
            ],
            "assignments": {},
        }
        _save_json(RM_SECTIONS_PATH, seed)
        return seed

    # Defensive normalization
    sections = data.get("sections")
    if not isinstance(sections, list):
        sections = []
    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    return {"sections": sections, "assignments": assignments}


def _section_for_ingredient(name, unit, sections_data):
    """Return the section id this ingredient is assigned to, or None."""
    key = _ingredient_section_key(name, unit)
    section_id = sections_data.get("assignments", {}).get(key)
    if not section_id:
        return None
    # Verify the section still exists; if it was deleted, treat as unassigned
    if not any(s.get("id") == section_id for s in sections_data.get("sections", [])):
        return None
    return section_id


# Bone broth domain keyword → section_id mapping for auto-assignment
# Keys are section NAMES (not IDs) — matched case-insensitively against live sections.
# This makes the mapping robust regardless of what section IDs were used when
# the sections were first created in the UI on Render.
_RM_KEYWORD_MAP = {
    "Bones": [
        "bone", "knuckle", "neck", "back", "foot", "feet", "marrow", "oxtail",
        "chicken carcass", "carcass", "beef", "bison", "pork", "lamb",
        "drumstick", "wing", "frame", "rib", "femur", "tibia",
    ],
    "Fresh Vegetables": [
        "onion", "carrot", "celery", "leek", "parsnip", "turnip",
        "fennel", "shallot", "garlic", "tomato", "potato",
        "cabbage", "kale", "spinach", "zucchini", "squash",
        "beet", "beetroot", "capsicum", "celeriac",
        "vegetable", "veg",
    ],
    "Herbs": [
        "thyme", "rosemary", "parsley", "bay leaf", "bay leaves", "sage",
        "oregano", "tarragon", "chive", "basil", "dill", "mint",
        "herb", "bouquet garni", "savory", "marjoram", "lovage",
    ],
    "Mushrooms": [
        "mushroom", "shiitake", "porcini", "portobello", "cremini",
        "reishi", "chaga", "lion's mane", "oyster mushroom",
        "maitake", "enoki", "chanterelle",
    ],
    "Adjuncts, Packs, & Juice": [
        "apple cider vinegar", "vinegar", "kombu", "seaweed", "kelp",
        "miso", "tamari", "soy", "coconut aminos", "fish sauce",
        "nutritional yeast", "yeast", "salt", "peppercorn",
        "adjunct", "pre-pack", "pack", "pouch", "sachet", "juice",
        "turmeric", "ginger", "lemon", "lime", "citrus",
        "pink himalayan", "sea salt", "black salt", "apple juice",
        "coconut water", "broth concentrate", "gelatin",
    ],
    "Spices & Other": [
        "spice", "cinnamon", "clove", "star anise", "cardamom",
        "cumin", "coriander", "paprika", "chili", "chilli",
        "allspice", "nutmeg", "mace", "juniper", "fennel seed",
        "mustard seed", "fenugreek", "curry", "sumac",
        "black pepper", "white pepper", "cayenne", "smoked paprika",
    ],
}


def _infer_section_for_ingredient(name):
    """Infer the best-match section NAME for an ingredient name using keyword mapping.
    Returns a section name string (e.g. 'Bones', 'Fresh Vegetables') or None.
    """
    name_lower = (name or "").lower()
    for section_name, keywords in _RM_KEYWORD_MAP.items():
        for kw in keywords:
            if kw in name_lower:
                return section_name
    return None


@app.route("/api/organic/raw-materials/auto-assign-sections", methods=["POST"])
@login_required
def auto_assign_rm_sections():
    """Auto-assign RM ingredients to sections based on ingredient name keywords.
    Only assigns ingredients that are currently Unassigned — does not overwrite
    existing manual assignments.
    Body: { overwrite: bool } — if true, reassigns everything including already-assigned.
    Returns a summary of what was assigned.
    """
    overwrite = (request.get_json() or {}).get("overwrite", False)
    sections_data = _load_rm_sections()
    assignments   = sections_data.get("assignments", {})
    sections_list = sections_data.get("sections", [])

    # Get all known ingredients from raw materials
    materials = _load_json(ORGANIC_RAW_PATH, [])
    seen = set()
    for mat in materials:
        name = (mat.get("item") or "").strip()
        unit = (mat.get("unit") or "").strip()
        if name and unit:
            seen.add((name, unit))

    # Also include custom items
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        name = c.get("name","").strip()
        unit = c.get("unit","").strip()
        if name and unit:
            seen.add((name, unit))

    # Verify all sections exist
    valid_section_ids = {s["id"] for s in sections_list}

    assigned   = []
    skipped    = []
    unresolved = []

    for name, unit in sorted(seen):
        key = _ingredient_section_key(name, unit)
        existing = assignments.get(key)

        # Skip if already assigned to a REAL section (not Unassigned) and not overwriting
        existing_is_real = (existing and existing in valid_section_ids
                            and existing != UNASSIGNED_SECTION_ID)
        if existing_is_real and not overwrite:
            skipped.append({"name": name, "unit": unit, "section": existing})
            continue

        inferred_name = _infer_section_for_ingredient(name)
        if inferred_name:
            # Find section ID by matching name case-insensitively against live sections
            matched = next(
                (s for s in sections_list
                 if s.get("name","").strip().lower() == inferred_name.lower()),
                None
            )
            if matched:
                assignments[key] = matched["id"]
                assigned.append({"name": name, "unit": unit, "section": matched["name"]})
            else:
                # Section name from keyword map doesn't exist on Render — unresolved
                unresolved.append({"name": name, "unit": unit,
                                   "note": f"Section '{inferred_name}' not found"})
        else:
            unresolved.append({"name": name, "unit": unit})

    sections_data["assignments"] = assignments
    _save_json(RM_SECTIONS_PATH, sections_data)

    return jsonify({
        "ok":         True,
        "assigned":   len(assigned),
        "skipped":    len(skipped),
        "unresolved": len(unresolved),
        "details": {
            "assigned":   assigned,
            "unresolved": unresolved,
        },
    })


@app.route("/api/organic/raw-materials/sections", methods=["GET"])
@login_required
def get_rm_sections():
    """Return the section list + per-ingredient assignments."""
    return jsonify(_load_rm_sections())


@app.route("/api/organic/raw-materials/sections", methods=["PUT"])
@login_required
def update_rm_sections():
    """Replace the entire section list. Body: {sections: [{id, name, order}]}.

    The id of each section must be a non-empty string. If a new section is
    added, the caller may use any unique id (we don't enforce a format).
    Sections that are removed will result in their assignments being cleared
    (orphan cleanup) on save."""
    data = request.json or {}
    sections_in = data.get("sections")
    if not isinstance(sections_in, list):
        return jsonify({"error": "sections must be a list"}), 400

    # Validate + deduplicate ids
    seen_ids = set()
    cleaned = []
    for i, s in enumerate(sections_in):
        if not isinstance(s, dict):
            return jsonify({"error": f"section {i}: must be an object"}), 400
        sid = (s.get("id") or "").strip()
        sname = (s.get("name") or "").strip()
        if not sid:
            return jsonify({"error": f"section {i}: id required"}), 400
        if not sname:
            return jsonify({"error": f"section {i}: name required"}), 400
        if sid == UNASSIGNED_SECTION_ID:
            return jsonify({"error": f"section id '{UNASSIGNED_SECTION_ID}' is reserved"}), 400
        if sid in seen_ids:
            return jsonify({"error": f"duplicate section id: {sid}"}), 400
        seen_ids.add(sid)
        cleaned.append({"id": sid, "name": sname, "order": i})

    existing = _load_rm_sections()
    # Drop assignments pointing at sections that no longer exist
    new_assignments = {
        k: v for k, v in existing["assignments"].items() if v in seen_ids
    }
    saved = {"sections": cleaned, "assignments": new_assignments}
    _save_json(RM_SECTIONS_PATH, saved)
    return jsonify({"success": True, **saved})


@app.route("/api/organic/raw-materials/assignments", methods=["PUT"])
@login_required
def update_rm_assignments():
    """Update ingredient-to-section assignments. Body: {assignments: {...}}.

    Each value must be either a valid section id or empty string (meaning
    'unassigned'). Empty assignments are removed entirely so the file
    doesn't grow unbounded."""
    data = request.json or {}
    incoming = data.get("assignments")
    if not isinstance(incoming, dict):
        return jsonify({"error": "assignments must be an object"}), 400

    existing = _load_rm_sections()
    valid_ids = {s["id"] for s in existing["sections"]}

    merged = dict(existing.get("assignments") or {})
    for key, value in incoming.items():
        if not isinstance(key, str) or not key:
            continue  # skip malformed entries
        v = (value or "").strip()
        if not v:
            merged.pop(key, None)
            continue
        if v not in valid_ids:
            return jsonify({"error": f"unknown section id: {v}"}), 400
        merged[key] = v

    existing["assignments"] = merged
    _save_json(RM_SECTIONS_PATH, existing)
    return jsonify({"success": True, "assignments": merged})


@app.route("/api/organic/raw-materials/grouped", methods=["GET"])
@login_required
def get_raw_materials_grouped():
    """Catalog-aware raw materials view.

    Returns one row per unique ingredient (name + unit) — unioning the
    ingredient picker (every active recipe contributes ingredients, plus
    custom user-added items) with actual receipt LOT data.

    Each row aggregates total received and remaining across all LOTs of
    that ingredient. Ingredients with no receipts ever still appear, with
    catalog_only=true and total_remaining=0.

    The response includes a 'category' field for display grouping.
    """
    # Get the canonical ingredient list (same as the picker dropdown)
    recipes = load_recipes()
    derived = {}  # key: (name_lower, unit) -> {name, unit}
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        for section in INGREDIENT_SECTIONS:
            for item in rdata.get(section, []):
                if not is_structured_ingredient(item):
                    continue
                if item.get("needs_review"):
                    continue
                ing_name = (item.get("name") or "").strip()
                unit = (item.get("unit") or "").strip()
                if is_untracked_ingredient(ing_name):
                    continue
                if not ing_name or not unit:
                    continue
                key = (ing_name.lower(), unit)
                if key not in derived:
                    derived[key] = {"name": ing_name, "unit": unit}

    # Merge custom user-added items
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        name = c.get("name", "")
        unit = c.get("unit", "")
        if not name or not unit or is_untracked_ingredient(name):
            continue
        key = (name.lower(), unit)
        if key not in derived:
            derived[key] = {"name": name, "unit": unit}

    # Now aggregate the actual raw materials inventory by (name_lower, unit)
    materials = _load_json(ORGANIC_RAW_PATH, [])
    aggregates = {}  # key -> {total_received, total_remaining, lot_count, active_lot_count, has_baseline}
    for mat in materials:
        item = (mat.get("item") or "").strip()
        unit = (mat.get("unit") or "").strip()
        if not item or not unit:
            continue
        key = (item.lower(), unit)
        if key not in aggregates:
            aggregates[key] = {
                "total_received": 0.0,
                "total_remaining": 0.0,
                "lot_count": 0,
                "active_lot_count": 0,
                "has_baseline": False,
                "name": item,  # use whatever casing was actually stored
                "unit": unit,
            }
        try:
            aggregates[key]["total_received"] += float(mat.get("quantity") or 0)
        except (ValueError, TypeError):
            pass
        try:
            aggregates[key]["total_remaining"] += float(mat.get("remaining") or 0)
        except (ValueError, TypeError):
            pass
        aggregates[key]["lot_count"] += 1
        if (float(mat.get("remaining") or 0) > 0):
            aggregates[key]["active_lot_count"] += 1
        if mat.get("migration_baseline"):
            aggregates[key]["has_baseline"] = True
        # If aggregate doesn't yet exist in derived (orphan — recipe was renamed
        # or ingredient was deleted from custom), surface it anyway so user can
        # see and reconcile.
        if key not in derived:
            derived[key] = {"name": item, "unit": unit}

    # Merge: every catalog item gets a row (even with no receipts)
    sections_data = _load_rm_sections()
    sections_list = sections_data.get("sections", [])
    section_order = {s["id"]: i for i, s in enumerate(sections_list)}
    section_names = {s["id"]: s["name"] for s in sections_list}

    rows = []
    for key, ing in derived.items():
        agg = aggregates.get(key, {})
        # Round to 2 decimals for display; keep as float for sums
        total_received = round(agg.get("total_received", 0.0), 3)
        total_remaining = round(agg.get("total_remaining", 0.0), 3)
        # Trim trailing zeros — show 50 not 50.000, but keep 2.5 as 2.5
        if total_received == int(total_received):
            total_received = int(total_received)
        if total_remaining == int(total_remaining):
            total_remaining = int(total_remaining)

        sec_id = _section_for_ingredient(ing["name"], ing["unit"], sections_data)
        if sec_id:
            sec_name = section_names.get(sec_id, UNASSIGNED_SECTION_NAME)
            sort_idx = section_order.get(sec_id, 999)
        else:
            sec_id = UNASSIGNED_SECTION_ID
            sec_name = UNASSIGNED_SECTION_NAME
            sort_idx = 1000  # always last

        rows.append({
            "name": ing["name"],
            "unit": ing["unit"],
            "section_id": sec_id,
            "section_name": sec_name,
            "_sort_idx": sort_idx,
            "total_received": total_received,
            "total_remaining": total_remaining,
            "lot_count": agg.get("lot_count", 0),
            "active_lot_count": agg.get("active_lot_count", 0),
            "has_baseline": agg.get("has_baseline", False),
            "catalog_only": (agg.get("lot_count", 0) == 0),
        })

    # Sort: by section order first, then alphabetically by name within each
    rows.sort(key=lambda r: (r["_sort_idx"], r["name"].lower(), r["unit"]))
    # Strip internal sort field before returning
    for r in rows:
        r.pop("_sort_idx", None)
    return jsonify(rows)


@app.route("/api/organic/raw-materials/by-ingredient/<path:item>/<unit>", methods=["GET"])
@login_required
def get_raw_material_lots(item, unit):
    """Return all LOT entries for a specific ingredient name + unit.
    Sorted oldest-first (FIFO order). Used when expanding a row in the
    inventory list to see LOT-level detail."""
    materials = _load_json(ORGANIC_RAW_PATH, [])
    item_lc = item.lower()
    matching = [m for m in materials
                if (m.get("item") or "").strip().lower() == item_lc
                and (m.get("unit") or "").strip() == unit]
    matching.sort(key=lambda m: (m.get("date_received") or "", m.get("created_at") or ""))
    return jsonify(matching)


@app.route("/api/organic/ingredients", methods=["POST"])
@login_required
def add_organic_ingredient():
    """Add a custom ingredient. Body: {name, unit}."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if unit not in VALID_UNITS:
        return jsonify({"error": f"Unit must be one of: {', '.join(VALID_UNITS)}"}), 400
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    existing = {(c.get("name", "").lower(), c.get("unit", "")) for c in custom}
    key = (name.lower(), unit)
    if key in existing:
        return jsonify({"error": "Already exists"}), 400
    custom.append({"name": name, "unit": unit})
    _save_json(ORGANIC_CUSTOM_ITEMS_PATH, custom)
    return jsonify({"success": True})


# ── Organic: Raw Material Inventory (FIFO) ────────────────────────────
@app.route("/api/organic/raw-materials", methods=["GET"])
@login_required
def get_raw_materials():
    return jsonify(_load_json(ORGANIC_RAW_PATH, []))


@app.route("/api/organic/raw-materials", methods=["POST"])
@login_required
def add_raw_material():
    """Add a raw material lot. JSON body:
      {item, unit, quantity, supplier, date_received, supplier_lot, baseline (optional)}

    If baseline is true, the supplier_lot is auto-prefixed with 'BL-' and date,
    and migration_baseline=true is set on the entry. These represent physical
    counts at the moment of inventory migration, not real production receipts.
    """
    data = request.json or {}
    materials = _load_json(ORGANIC_RAW_PATH, [])
    item = (data.get("item") or "").strip()
    if not item:
        return jsonify({"error": "Item name required"}), 400
    try:
        qty = float(data.get("quantity", 0))
    except (ValueError, TypeError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    is_baseline = bool(data.get("baseline"))
    supplier_lot = (data.get("supplier_lot") or "").strip()
    if is_baseline and not supplier_lot:
        # Auto-generate BL-DDMMYY
        supplier_lot = "BL-" + datetime.now().strftime("%d%m%y")

    entry = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(materials)),
        "item": item,
        "supplier": data.get("supplier", ""),
        "date_received": data.get("date_received", "") or datetime.now().strftime("%Y-%m-%d"),
        "supplier_lot": supplier_lot,
        "quantity": qty,
        "unit": data.get("unit", ""),
        "remaining": qty,
        "created_at": datetime.now().isoformat(),
    }
    if is_baseline:
        entry["migration_baseline"] = True
    materials.append(entry)
    _save_json(ORGANIC_RAW_PATH, materials)
    supplier = data.get("supplier", "").strip()
    if supplier:
        _add_contact("supplier", supplier)
    return jsonify({"success": True, "entry": entry})


@app.route("/api/organic/raw-materials/bulk", methods=["POST"])
@login_required
def add_raw_materials_bulk():
    """Bulk-create raw material LOT entries in a single request. JSON body:
      {
        entries: [{item, unit, quantity, supplier?, supplier_lot?, date_received?, notes?}, ...],
        baseline: bool   # true for day-zero physical-count entry, false for real receipts
      }

    Used by:
      - Day-Zero baseline bulk grid (baseline=true): all entries share BL-DDMMYY
        lot, supplier defaults to '(physical count)'
      - Future camera-scan multi-row receipt entry (baseline=false): each entry
        keeps its own supplier_lot from the parsed invoice

    Behavior:
    - All entries are validated upfront against the canonical ingredient list
      (recipes + custom items). Atomic: either all valid entries save, or none.
    - Skips rows with quantity <= 0 silently (so users can leave blanks)
    - Returns count of created entries and the LOT(s) used
    """
    data = request.json or {}
    entries_in = data.get("entries") or []
    is_baseline = bool(data.get("baseline"))
    if not isinstance(entries_in, list):
        return jsonify({"error": "entries must be a list"}), 400

    # Build the canonical ingredient set: {(name_lower, unit): canonical_name}
    # This is the same set the picker populates from — recipes + custom items.
    canonical = {}
    recipes = load_recipes()
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        for section in INGREDIENT_SECTIONS:
            for it in rdata.get(section, []):
                if not is_structured_ingredient(it):
                    continue
                if it.get("needs_review"):
                    continue
                ing_name = (it.get("name") or "").strip()
                unit = (it.get("unit") or "").strip()
                if is_untracked_ingredient(ing_name):
                    continue
                if not ing_name or not unit:
                    continue
                key = (ing_name.lower(), unit)
                if key not in canonical:
                    canonical[key] = ing_name
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        nm = (c.get("name") or "").strip()
        un = (c.get("unit") or "").strip()
        if not nm or not un or is_untracked_ingredient(nm):
            continue
        key = (nm.lower(), un)
        if key not in canonical:
            canonical[key] = nm

    # Validate every entry first — atomic semantics
    to_create = []
    errors = []
    for idx, e in enumerate(entries_in):
        if not isinstance(e, dict):
            errors.append(f"row {idx}: not an object")
            continue
        item = (e.get("item") or "").strip()
        unit = (e.get("unit") or "").strip()
        if not item or not unit:
            continue  # silently skip blank rows
        try:
            qty = float(e.get("quantity") or 0)
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue  # skip blank/zero rows
        key = (item.lower(), unit)
        if key not in canonical:
            errors.append(f"row {idx}: ingredient '{item}' ({unit}) not in catalog")
            continue
        # Use the canonical casing so storage stays consistent
        to_create.append({
            "item": canonical[key],
            "unit": unit,
            "quantity": qty,
            "supplier": (e.get("supplier") or "").strip(),
            "supplier_lot": (e.get("supplier_lot") or "").strip(),
            "date_received": (e.get("date_received") or "").strip(),
            "notes": (e.get("notes") or "").strip(),
        })

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    if not to_create:
        return jsonify({"success": True, "created": 0, "entries": []})

    materials = _load_json(ORGANIC_RAW_PATH, [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    base_ts = datetime.now()
    shared_baseline_lot = "BL-" + base_ts.strftime("%d%m%y") if is_baseline else None
    suppliers_seen = set()
    created = []

    for i, item in enumerate(to_create):
        # Resolve per-entry fields based on mode
        if is_baseline:
            supplier = item["supplier"] or "(physical count)"
            supplier_lot = shared_baseline_lot
            date_received = item["date_received"] or today_str
        else:
            # Receipt mode (future scan): each entry uses its own supplier + lot
            supplier = item["supplier"]
            supplier_lot = item["supplier_lot"] or ("MAN-" + base_ts.strftime("%d%m%y"))
            date_received = item["date_received"] or today_str

        entry = {
            "id": "rm_bulk_" + base_ts.strftime("%Y%m%d%H%M%S") + f"_{i:03d}",
            "item": item["item"],
            "supplier": supplier,
            "supplier_lot": supplier_lot,
            "date_received": date_received,
            "quantity": item["quantity"],
            "remaining": item["quantity"],
            "unit": item["unit"],
            "created_at": base_ts.isoformat(),
        }
        if is_baseline:
            entry["migration_baseline"] = True
        if item["notes"]:
            entry["notes"] = item["notes"]
        materials.append(entry)
        created.append(entry)
        if supplier and supplier != "(physical count)":
            suppliers_seen.add(supplier)

    _save_json(ORGANIC_RAW_PATH, materials)
    # Record any new suppliers in the contacts file (skips duplicates internally)
    for s in suppliers_seen:
        _add_contact("supplier", s)

    return jsonify({
        "success": True,
        "created": len(created),
        "lot": shared_baseline_lot,  # only set when baseline=true
        "entries": created,
    })


def _runs_using_raw_material(entry_id):
    """Return list of completed organic runs that have deducted from this raw material entry."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    matches = []
    for r in runs:
        if r.get("status") != "completed":
            continue
        for used in (r.get("ingredients_used") or []):
            if used.get("raw_material_id") == entry_id:
                matches.append({
                    "run_id": r.get("id"),
                    "week_id": r.get("week_id"),
                    "day_idx": r.get("day_idx"),
                    "vessel": r.get("vessel"),
                    "recipe": r.get("recipe"),
                    "quantity_used": used.get("quantity_used"),
                    "unit": used.get("unit"),
                })
                break
    return matches


@app.route("/api/organic/raw-materials/<entry_id>", methods=["PUT"])
@login_required
def update_raw_material(entry_id):
    """Manually adjust the remaining quantity of a raw material entry.
    Body: {remaining}. Use for stock counts / corrections.
    Other fields (item, unit, original quantity) are NOT editable here —
    they would invalidate prior production-run deductions if changed.
    """
    data = request.json or {}
    materials = _load_json(ORGANIC_RAW_PATH, [])
    entry = next((m for m in materials if m.get("id") == entry_id), None)
    if not entry:
        return jsonify({"error": "Entry not found"}), 404
    if "remaining" not in data:
        return jsonify({"error": "remaining required"}), 400
    try:
        new_remaining = float(data.get("remaining"))
    except (ValueError, TypeError):
        return jsonify({"error": "remaining must be a number"}), 400
    entry["remaining"] = round(new_remaining, 4)
    entry["last_adjusted_at"] = datetime.now().isoformat()
    _save_json(ORGANIC_RAW_PATH, materials)
    return jsonify({"success": True, "entry": entry})


@app.route("/api/organic/raw-materials/<entry_id>/usage", methods=["GET"])
@login_required
def get_raw_material_usage(entry_id):
    """Return list of completed runs that deducted from this entry. Frontend
    uses this to warn the user before deletion."""
    return jsonify({"used_in": _runs_using_raw_material(entry_id)})


@app.route("/api/organic/raw-materials/<entry_id>", methods=["DELETE"])
@login_required
def delete_raw_material(entry_id):
    materials = _load_json(ORGANIC_RAW_PATH, [])
    materials = [m for m in materials if m.get("id") != entry_id]
    _save_json(ORGANIC_RAW_PATH, materials)
    return jsonify({"success": True})


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
    # Flask's MultiDict supports getlist
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


@app.route("/api/organic/invoices", methods=["POST"])
@login_required
def upload_invoice():
    """Create a new invoice record. Multipart form:
      supplier (str), invoice_date (YYYY-MM-DD), lots[] (repeated or comma-sep),
      file (uploaded invoice)."""
    if "file" not in request.files:
        return jsonify({"error": "File required"}), 400
    f = request.files["file"]
    supplier = (request.form.get("supplier") or "").strip()
    invoice_date = (request.form.get("invoice_date") or "").strip()
    lots = _parse_lots_field(request.form)

    if not supplier:
        return jsonify({"error": "Supplier required"}), 400
    if not invoice_date:
        return jsonify({"error": "Invoice date required"}), 400

    inv_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    try:
        filename, meta = _save_invoice_file_bytes(inv_id, f)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    record = {
        "id": inv_id,
        "supplier": supplier,
        "invoice_date": invoice_date,
        "lots": lots,
        "filename": filename,
        "original_name": meta["original_name"],
        "size_bytes": meta["size_bytes"],
        "mime_type": meta["mime_type"],
        "uploaded_at": datetime.now().isoformat(),
    }
    invoices = _load_json(INVOICES_INDEX_PATH, [])
    invoices.append(record)
    _save_json(INVOICES_INDEX_PATH, invoices)
    if supplier:
        _add_contact("supplier", supplier)
    return jsonify({"success": True, "invoice": record})


@app.route("/api/organic/invoices", methods=["GET"])
@login_required
def list_invoices():
    """Return all invoices, newest invoice_date first (ties broken by uploaded_at)."""
    lot_filter = (request.args.get("lot") or "").strip()
    invoices = _load_json(INVOICES_INDEX_PATH, [])
    if lot_filter:
        lf = lot_filter.lower()
        invoices = [inv for inv in invoices
                    if any(lf in (l or "").lower() for l in (inv.get("lots") or []))]
    invoices.sort(key=lambda r: (r.get("invoice_date", ""), r.get("uploaded_at", "")),
                  reverse=True)
    return jsonify(invoices)


@app.route("/api/organic/invoices/<inv_id>/file", methods=["GET"])
@login_required
def serve_invoice(inv_id):
    """Serve the invoice file inline (not forced download)."""
    invoices = _load_json(INVOICES_INDEX_PATH, [])
    rec = next((i for i in invoices if i.get("id") == inv_id), None)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    fname = rec.get("filename", "")
    safe = os.path.basename(fname)
    if safe != fname:
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(INVOICES_DIR, safe)
    if not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404
    return send_file(path, mimetype=_invoice_mime(safe),
                     as_attachment=False, download_name=safe)


@app.route("/api/organic/invoices/<inv_id>", methods=["DELETE"])
@login_required
def delete_invoice(inv_id):
    invoices = _load_json(INVOICES_INDEX_PATH, [])
    rec = next((i for i in invoices if i.get("id") == inv_id), None)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    _remove_invoice_file(rec.get("filename", ""))
    invoices = [i for i in invoices if i.get("id") != inv_id]
    _save_json(INVOICES_INDEX_PATH, invoices)
    return jsonify({"success": True})


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
def _add_contact(contact_type, name):
    contacts = _load_json(ORGANIC_CONTACTS_PATH, {})
    if contact_type not in contacts:
        contacts[contact_type] = []
    if name not in contacts[contact_type]:
        contacts[contact_type].append(name)
        _save_json(ORGANIC_CONTACTS_PATH, contacts)


@app.route("/api/organic/contacts", methods=["GET"])
@login_required
def get_organic_contacts():
    return jsonify(_load_json(ORGANIC_CONTACTS_PATH, {}))


# ── Organic: Production Runs ──────────────────────────────────────────
@app.route("/api/organic/production-runs", methods=["GET"])
@login_required
def get_organic_runs():
    return jsonify(_load_json(ORGANIC_RUNS_PATH, []))


def _previous_day_coords(week_id, day_idx):
    """Return (prev_week_id, prev_day_idx) for the day BEFORE (week_id, day_idx).
    For Monday (d_idx=0), crosses back to last week's Sunday (d_idx=6)."""
    if day_idx > 0:
        return week_id, day_idx - 1
    try:
        prev_week_start = datetime.strptime(week_id, "%Y-%m-%d") - timedelta(days=7)
    except ValueError:
        return week_id, day_idx
    return prev_week_start.strftime("%Y-%m-%d"), 6


def _complete_organic_run(finish_week_id, finish_day_idx, produced_data):
    """Process organic production amounts entered on the FINISH day.

    Semantic: 'Amount Produced' entered on day D is the output of the recipe
    that was STARTED on day D-1. So this function:
      1. Finds organic runs scheduled on the previous day (start day)
      2. Matches each run's vessel against produced_data[vessel]
      3. Deducts raw materials based on amount produced
      4. Creates / updates a finished goods entry, timestamped to the FINISH day
         with LOT# = finish_date + 365 days (packaging day expiry)
    Idempotent: re-saving updates in place. Sales already made against an
    edited entry are preserved (quantity_remaining = new_qty - already_sold).
    """
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    materials = _load_json(ORGANIC_RAW_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, []) if os.path.exists(ORGANIC_SALES_PATH) else []
    recipes = load_recipes()

    # Warnings surfaced to the user via the save endpoint response
    warnings = []

    # Find runs scheduled on the START day (yesterday relative to finish day)
    start_week_id, start_day_idx = _previous_day_coords(finish_week_id, finish_day_idx)

    # Compute finish-day expiry LOT# (packaging-day + 365 days)
    try:
        finish_date = datetime.strptime(finish_week_id, "%Y-%m-%d") + timedelta(days=finish_day_idx)
        expiry_lot = (finish_date + timedelta(days=365)).strftime("%d%m%y")
    except ValueError:
        finish_date = datetime.now()
        expiry_lot = (finish_date + timedelta(days=365)).strftime("%d%m%y")

    produced = (produced_data or {}).get("produced") or {}

    for run in runs:
        if run.get("week_id") != start_week_id or run.get("day_idx") != start_day_idx:
            continue
        # Universal inventory: process every scheduled run, regardless of
        # the recipe's certification status. The recipe is our source of truth
        # for the certification tag that travels onto FG entries.
        recipe_name = run.get("recipe", "")
        recipe_data = recipes.get(recipe_name, {})
        if not recipe_data:
            continue

        vessel = run["vessel"]

        # Get amount produced for this vessel from the FINISH day's checklist
        vid = vessel.replace("(", "").replace(")", "")
        try:
            amount = int(produced.get(vid, 0))
        except (ValueError, TypeError):
            amount = 0

        # Find existing finished goods entry for this run (idempotency key:
        # finish_week + finish_day + vessel)
        fg_id = f"fg_{finish_week_id}_{finish_day_idx}_{vessel}"
        existing_fg = next((f for f in fg if f.get("id") == fg_id), None)

        # If amount is 0 and no prior FG exists, skip entirely (no production)
        if amount <= 0 and not existing_fg:
            continue

        # Deduct raw materials based on the new amount.
        # If we're updating an existing entry, first restore any previously-deducted
        # materials so we can recompute clean. (Simpler: always restore, then deduct fresh.)
        prev_used = run.get("ingredients_used") or []
        if run.get("status") == "completed" and prev_used:
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

        ingredients_used = []
        is_115L = vessel == "115L"
        half_factor = 0.5 if is_115L else 1.0

        recipe_yield = 0
        try:
            recipe_yield = int(recipe_data.get("yield") or 0)
        except (ValueError, TypeError):
            recipe_yield = 0
        jar_l = _jar_volume_liters(recipe_data) or 0.75
        batch_liters = recipe_yield * jar_l * half_factor

        if amount > 0:
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
                    for mat in materials:
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
                        })
                        if qty_remaining_to_deduct <= 0:
                            break
                    # Pass 2: name-only fallback
                    if qty_remaining_to_deduct > 0:
                        for mat in materials:
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

        # Detect manual lot-adjust collision: if an existing FG entry shows a
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
                # the re-save resets the manual adjustment
                for k, v in new_fg.items():
                    existing_fg[k] = v
                existing_fg.pop("last_adjusted_at", None)
            else:
                fg.append(new_fg)
        else:
            # Amount went to zero — drop the FG entry (no production after all)
            if existing_fg:
                fg = [f for f in fg if f.get("id") != fg_id]

    _save_json(ORGANIC_RUNS_PATH, runs)
    _save_json(ORGANIC_RAW_PATH, materials)
    _save_json(ORGANIC_FG_PATH, fg)
    return warnings


# ── Organic: Finished Goods ──────────────────────────────────────────
def _sku_key(brand, recipe, fmt):
    """Stable identifier for a SKU group: 'BRAND|RECIPE|FORMAT'.
    Format is normalized so 'SS-750ML' and 'ss-750ml' collapse into one SKU.
    Separator chosen so it can't appear in any of the components."""
    return "|".join([(brand or ""), (recipe or ""), _normalize_format(fmt or "")])


def _sku_display(brand, recipe, fmt):
    """Human-readable SKU label using the canonical helper."""
    return build_display_name({"brand": brand, "format": fmt}, recipe_name=recipe)


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
            # Pull certification from the entry itself, or look up the recipe
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

    # Compute lot counts per group (distinct LOT#s)
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
            # Existing FG group — just ensure cert is set if it was missing
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


def _aggregate_lots_for_sku(fg, sku_key):
    """Return LOT-level rollup for a given SKU. One row per distinct LOT#,
    aggregating across all kettles that share that LOT.
    Sorted FIFO by production date (oldest first)."""
    rows = {}
    for entry in fg:
        key = _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", ""))
        if key != sku_key:
            continue
        lot = entry.get("lot", "")
        if lot not in rows:
            rows[lot] = {
                "lot": lot,
                "produced": 0,
                "remaining": 0,
                "production_date": None,    # finish date string YYYY-MM-DD
                "best_before": "",            # parsed ddmmyy → dd/mm/yyyy
                "vessels": set(),
                "fg_ids": [],
            }
        r = rows[lot]
        try:
            r["produced"] += int(entry.get("quantity_produced") or 0)
            r["remaining"] += int(entry.get("quantity_remaining") or 0)
        except (ValueError, TypeError):
            pass
        if entry.get("vessel"):
            r["vessels"].add(entry["vessel"])
        r["fg_ids"].append(entry.get("id"))

        # Compute production_date from finish_week_id + day_idx (preferred)
        # falling back to created_at
        prod_date = None
        wid = entry.get("week_id")
        d_idx = entry.get("day_idx")
        if wid is not None and d_idx is not None:
            try:
                pd = datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))
                prod_date = pd.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
        if not prod_date and entry.get("created_at"):
            prod_date = entry["created_at"][:10]
        if prod_date and (r["production_date"] is None or prod_date < r["production_date"]):
            r["production_date"] = prod_date

        # Parse best-before from LOT (ddmmyy → dd/mm/yyyy)
        if lot and len(lot) == 6 and lot.isdigit():
            r["best_before"] = f"{lot[0:2]}/{lot[2:4]}/20{lot[4:6]}"

    out = []
    for lot, r in rows.items():
        r["vessels"] = sorted(r["vessels"])
        out.append(r)
    # FIFO: oldest production date first; depleted lots sorted within their date
    out.sort(key=lambda r: (r["production_date"] or "9999-99-99", r["lot"]))
    return out


# ── Organic: Finished Goods endpoints ──────────────────────────────────
# ORDERING NOTE: Flask matches routes top-to-bottom, but the methods
# disambiguate /finished-goods/<fg_id> (PUT/DELETE) from /finished-goods/grouped
# (GET) and /finished-goods/sku/<key> (GET). A future maintainer adding a new
# GET on /<fg_id> would shadow grouped/sku — so if you add one, also reorder
# so /grouped and /sku/<path:sku_key> come BEFORE the generic /<fg_id> route.

@app.route("/api/organic/finished-goods", methods=["GET"])
@login_required
def get_finished_goods():
    """Returns raw per-kettle FG entries. Used by traceability/legacy callers."""
    return jsonify(_load_json(ORGANIC_FG_PATH, []))


@app.route("/api/organic/finished-goods/<fg_id>", methods=["PUT"])
@login_required
def update_finished_good(fg_id):
    """Manually adjust the remaining quantity on a finished goods entry.
    Body: {remaining}. Use for inventory corrections (loss, breakage, recount).
    The original quantity_produced and LOT# are preserved unchanged."""
    data = request.json or {}
    fg = _load_json(ORGANIC_FG_PATH, [])
    entry = next((f for f in fg if f.get("id") == fg_id), None)
    if not entry:
        return jsonify({"error": "Finished goods entry not found"}), 404
    if "remaining" not in data:
        return jsonify({"error": "remaining required"}), 400
    try:
        new_remaining = int(data.get("remaining"))
    except (ValueError, TypeError):
        return jsonify({"error": "remaining must be an integer"}), 400
    if new_remaining < 0:
        return jsonify({"error": "remaining cannot be negative"}), 400
    entry["quantity_remaining"] = new_remaining
    entry["last_adjusted_at"] = datetime.now().isoformat()
    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "entry": entry})


@app.route("/api/organic/finished-goods/<fg_id>", methods=["DELETE"])
@login_required
def delete_finished_good(fg_id):
    """Delete a finished goods entry. Sales already made against this entry
    keep their records (they stand as historical sales) but no inventory
    is restored anywhere — the FG entry is simply removed."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    if not any(f.get("id") == fg_id for f in fg):
        return jsonify({"error": "Finished goods entry not found"}), 404
    fg = [f for f in fg if f.get("id") != fg_id]
    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})


@app.route("/api/organic/finished-goods/lot-adjust", methods=["POST"])
@login_required
def adjust_lot_remaining():
    """Set the total remaining quantity for a LOT within a SKU. Body:
    {sku_key, lot, new_remaining}. The delta from current is distributed across
    underlying FG entries (same LOT can span multiple kettles).
    Used for manual stock corrections (loss, breakage, recount)."""
    data = request.json or {}
    sku_key = (data.get("sku_key") or "").strip()
    lot = (data.get("lot") or "").strip()
    if not sku_key or not lot:
        return jsonify({"error": "sku_key and lot required"}), 400
    try:
        new_remaining = int(data.get("new_remaining"))
    except (ValueError, TypeError):
        return jsonify({"error": "new_remaining must be an integer"}), 400
    if new_remaining < 0:
        return jsonify({"error": "new_remaining cannot be negative"}), 400

    fg = _load_json(ORGANIC_FG_PATH, [])
    matching = [f for f in fg
                if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
                and f.get("lot") == lot]
    if not matching:
        return jsonify({"error": "No FG entries match that SKU + LOT"}), 404
    current_total = sum(int(f.get("quantity_remaining") or 0) for f in matching)
    produced_total = sum(int(f.get("quantity_produced") or 0) for f in matching)
    delta = new_remaining - current_total

    warnings = []
    if new_remaining > produced_total:
        warnings.append({
            "kind": "exceeds_produced",
            "message": (f"Adjusted remaining ({new_remaining}) exceeds total produced "
                        f"for this LOT ({produced_total}). The adjustment was applied, "
                        f"but verify this isn't a typo."),
        })

    if delta == 0:
        return jsonify({"success": True, "current_total": current_total,
                        "new_total": new_remaining, "warnings": warnings})

    if delta < 0:
        # Reducing — drain from entries in order until delta absorbed
        to_remove = -delta
        for f in matching:
            if to_remove <= 0:
                break
            avail = int(f.get("quantity_remaining") or 0)
            take = min(avail, to_remove)
            f["quantity_remaining"] = avail - take
            f["last_adjusted_at"] = datetime.now().isoformat()
            to_remove -= take
    else:
        # Increasing — add to first entry (it's an inventory correction; we
        # don't try to redistribute proportionally because the user's intent
        # is "the LOT total should be N", and the bucket is logically one).
        first = matching[0]
        first["quantity_remaining"] = int(first.get("quantity_remaining") or 0) + delta
        first["last_adjusted_at"] = datetime.now().isoformat()

    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "previous_total": current_total,
                    "new_total": new_remaining, "warnings": warnings})


@app.route("/api/organic/finished-goods/baseline", methods=["POST"])
@login_required
def add_baseline_finished_good():
    """Add a baseline (day-zero migration) FG entry. JSON body:
      {recipe, quantity, lot (optional, defaults to BL-DDMMYY)}

    The recipe must exist in the recipes file — we look it up to populate
    brand, format, and certification automatically. The entry is flagged
    with migration_baseline=true and uses a sentinel vessel='Pre-migration'.
    """
    data = request.json or {}
    recipe_name = (data.get("recipe") or "").strip()
    if not recipe_name:
        return jsonify({"error": "recipe required"}), 400
    try:
        qty = int(data.get("quantity") or 0)
    except (ValueError, TypeError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    recipes = load_recipes()
    recipe = recipes.get(recipe_name)
    if not recipe:
        return jsonify({"error": f"Recipe '{recipe_name}' not found"}), 404

    lot = (data.get("lot") or "").strip() or ("BL-" + datetime.now().strftime("%d%m%y"))

    fg = _load_json(ORGANIC_FG_PATH, [])
    entry = {
        "id": "fg_baseline_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(len(fg)),
        "recipe": recipe_name,
        "brand": (recipe.get("brand") or "").strip(),
        "format": (recipe.get("format") or "").strip(),
        "certification": (recipe.get("certification") or "").strip(),
        "lot": lot,
        "quantity_produced": qty,
        "quantity_remaining": qty,
        "vessel": "Pre-migration",
        "week_id": None,
        "day_idx": None,
        "created_at": datetime.now().isoformat(),
        "migration_baseline": True,
    }
    fg.append(entry)
    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "entry": entry})


@app.route("/api/organic/finished-goods/baseline-bulk", methods=["POST"])
@login_required
def add_baseline_finished_goods_bulk():
    """Bulk-create baseline FG entries in a single request. JSON body:
      {entries: [{recipe, quantity, notes (optional)}, ...]}

    Used by the Day-Zero bulk grid: user fills in counts for many recipes
    at once and submits everything together.

    Behavior:
    - All entries share the same BL-DDMMYY LOT (same migration date)
    - Skips entries with quantity <= 0 (so user can leave most rows blank)
    - Validates each recipe exists; returns 400 if any are unknown
    - Atomic: either all valid entries save, or none if any validation fails
    - Returns count of entries created and the list of LOTs
    """
    data = request.json or {}
    entries_in = data.get("entries") or []
    if not isinstance(entries_in, list):
        return jsonify({"error": "entries must be a list"}), 400

    # Validate every entry first — atomic semantics
    recipes = load_recipes()
    to_create = []
    errors = []
    for idx, e in enumerate(entries_in):
        if not isinstance(e, dict):
            errors.append(f"row {idx}: not an object")
            continue
        recipe_name = (e.get("recipe") or "").strip()
        if not recipe_name:
            continue  # silently skip blank rows
        try:
            qty = int(e.get("quantity") or 0)
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue  # user left this row blank or zero — skip it
        recipe = recipes.get(recipe_name)
        if not recipe:
            errors.append(f"row {idx}: recipe '{recipe_name}' not found")
            continue
        to_create.append({
            "recipe_name": recipe_name,
            "recipe": recipe,
            "quantity": qty,
            "notes": (e.get("notes") or "").strip(),
        })

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 400

    if not to_create:
        return jsonify({"success": True, "created": 0, "entries": []})

    lot = "BL-" + datetime.now().strftime("%d%m%y")
    fg = _load_json(ORGANIC_FG_PATH, [])
    created = []
    base_ts = datetime.now()
    for i, item in enumerate(to_create):
        recipe = item["recipe"]
        # Make IDs deterministically distinct even when created within the
        # same second by appending the index.
        entry = {
            "id": "fg_baseline_" + base_ts.strftime("%Y%m%d%H%M%S") + f"_{i:03d}",
            "recipe": item["recipe_name"],
            "brand": (recipe.get("brand") or "").strip(),
            "format": (recipe.get("format") or "").strip(),
            "certification": (recipe.get("certification") or "").strip(),
            "lot": lot,
            "quantity_produced": item["quantity"],
            "quantity_remaining": item["quantity"],
            "vessel": "Pre-migration",
            "week_id": None,
            "day_idx": None,
            "created_at": base_ts.isoformat(),
            "migration_baseline": True,
        }
        if item["notes"]:
            entry["notes"] = item["notes"]
        fg.append(entry)
        created.append(entry)

    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "created": len(created), "lot": lot, "entries": created})


# ── Manual inventory adjustments ────────────────────────────────────
# These cover everyday cases AFTER day-zero migration: returns, found stock,
# breakage, spillage, theft, sampling, donations, recount discrepancies.
# Distinct from baseline (one-time) and from production runs (auto).

VALID_ADD_REASONS = ["Found stock", "Distributor return", "Recount", "Other"]
VALID_SUBTRACT_REASONS = [
    "Spillage", "Waste", "Theft", "Sample", "Donation", "Recount", "Other"
]


def _record_adjustment(record):
    """Append an adjustment record to the audit log."""
    log = _load_json(ADJUSTMENTS_PATH, [])
    log.append(record)
    _save_json(ADJUSTMENTS_PATH, log)


@app.route("/api/organic/finished-goods/manual-add", methods=["POST"])
@login_required
def manual_add_finished_good():
    """Add inventory to a SKU outside of production. Body:
      {recipe, quantity, lot (optional), reason, notes (optional)}

    Use cases: distributor return, found stock, recount-up correction.
    Auto-generates LOT 'MAN-DDMMYY' if not supplied. Records full audit
    entry in adjustments.json. Stamps manual_addition=true on the FG entry."""
    data = request.json or {}
    recipe_name = (data.get("recipe") or "").strip()
    if not recipe_name:
        return jsonify({"error": "recipe required"}), 400
    try:
        qty = int(data.get("quantity") or 0)
    except (ValueError, TypeError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    recipes = load_recipes()
    recipe = recipes.get(recipe_name)
    if not recipe:
        return jsonify({"error": f"Recipe '{recipe_name}' not found"}), 404

    reason = (data.get("reason") or "Other").strip()
    notes = (data.get("notes") or "").strip()
    lot = (data.get("lot") or "").strip() or ("MAN-" + datetime.now().strftime("%d%m%y"))

    fg = _load_json(ORGANIC_FG_PATH, [])
    entry_id = "fg_manual_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(len(fg))
    entry = {
        "id": entry_id,
        "recipe": recipe_name,
        "brand": (recipe.get("brand") or "").strip(),
        "format": (recipe.get("format") or "").strip(),
        "certification": (recipe.get("certification") or "").strip(),
        "lot": lot,
        "quantity_produced": qty,
        "quantity_remaining": qty,
        "vessel": "Manual entry",
        "week_id": None,
        "day_idx": None,
        "created_at": datetime.now().isoformat(),
        "manual_addition": True,
    }
    fg.append(entry)
    _save_json(ORGANIC_FG_PATH, fg)

    _record_adjustment({
        "id": "adj_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(qty),
        "kind": "add",
        "recipe": recipe_name,
        "brand": entry["brand"],
        "format": entry["format"],
        "quantity": qty,
        "lot": lot,
        "fg_id": entry_id,
        "reason": reason,
        "notes": notes,
        "created_at": datetime.now().isoformat(),
    })

    return jsonify({"success": True, "entry": entry, "lot": lot})


@app.route("/api/organic/finished-goods/manual-subtract", methods=["POST"])
@login_required
def manual_subtract_finished_good():
    """Drain inventory from a SKU via FIFO across its LOTs. Body:
      {recipe, quantity, reason, notes (optional)}

    Use cases: spillage, breakage, theft, samples, donations.
    Drains oldest LOTs first (matching the sales FIFO logic). Records
    full audit entry in adjustments.json including which LOTs were drained.
    Returns 400 if requested quantity exceeds available stock."""
    data = request.json or {}
    recipe_name = (data.get("recipe") or "").strip()
    if not recipe_name:
        return jsonify({"error": "recipe required"}), 400
    try:
        qty = int(data.get("quantity") or 0)
    except (ValueError, TypeError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "Quantity must be greater than 0"}), 400

    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "reason required"}), 400
    notes = (data.get("notes") or "").strip()

    fg = _load_json(ORGANIC_FG_PATH, [])
    # Find all entries for this recipe, sorted oldest-first for FIFO
    matching = [f for f in fg if (f.get("recipe") or "") == recipe_name and int(f.get("quantity_remaining") or 0) > 0]
    matching.sort(key=lambda f: (f.get("created_at") or ""))

    available = sum(int(f.get("quantity_remaining") or 0) for f in matching)
    if qty > available:
        return jsonify({
            "error": f"Insufficient stock: requested {qty}, available {available}",
            "available": available,
        }), 400

    drained = []
    remaining_to_drain = qty
    for entry in matching:
        if remaining_to_drain <= 0:
            break
        avail = int(entry.get("quantity_remaining") or 0)
        take = min(avail, remaining_to_drain)
        entry["quantity_remaining"] = avail - take
        drained.append({
            "fg_id": entry.get("id"),
            "lot": entry.get("lot"),
            "quantity": take,
        })
        remaining_to_drain -= take

    _save_json(ORGANIC_FG_PATH, fg)

    _record_adjustment({
        "id": "adj_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(qty),
        "kind": "subtract",
        "recipe": recipe_name,
        "quantity": qty,
        "drained": drained,
        "reason": reason,
        "notes": notes,
        "created_at": datetime.now().isoformat(),
    })

    return jsonify({
        "success": True,
        "quantity_removed": qty,
        "drained": drained,
        "remaining_total": available - qty,
    })


# ══════════════════════════════════════════════════════════════════════════════
# COGS CALCULATOR ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# Maps app format codes to COGS matrix size keys
_FORMAT_TO_COGS_SIZE = {
    "SS-476ML":    "475ml",
    "SS-475ML":    "475ml",
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
    "onion":    ["onion"],
    "carrot":   ["carrot"],
    "celery":   ["celery"],
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
    # Note: mushroom cost is flat $/batch (both types combined = $252.56)
    # When a recipe has mushrooms, use this flat cost per batch

    # ── Other ingredients (flat $/batch — mirepoix + salt + spices) ──────────
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


COGS_SCHEMA_VERSION = 3  # increment when seed structure changes

def _load_cogs():
    """Load COGS data. Always validates structure against current seed version."""
    data = _load_json(COGS_PATH, {}) if os.path.exists(COGS_PATH) else {}

    # Force reseed if version mismatch OR any required structural key missing
    required_keys = {"overhead_fixed", "overhead_variable", "slow_kettles_per_day",
                     "supplies", "mushroom_fresh", "mushroom_specialty",
                     "other_ingredients_per_batch"}
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

                # Bone cost from recipe kg × $/kg
                bone_prices = c.get("bones", {})
                bp          = bone_prices.get(bt_key, {})
                bone_price  = float(bp.get("price_per_kg", 0))
                bone_pu     = (bones_kg * bone_price) / units

                # Turkey: add flat whole-turkey cost per batch
                if bt_key == "turkey":
                    whole_cost = float(bp.get("whole_turkey_cost_per_batch", 180))
                    bone_pu   += whole_cost / units

                # Mushroom: from recipe kg if mushroom broth,
                # OR use flat $/batch from seed if no kg provided
                if bt_key == "mushroom":
                    if mushroom_kg > 0:
                        # fresh rate used for all mushroom kg from recipe
                        mf = c.get("mushroom_fresh", {})
                        ms = c.get("mushroom_specialty", {})
                        mush_cost_per_batch = (
                            float(mf.get("kg_per_batch", 16)) * float(mf.get("price_per_kg", 11.66)) +
                            float(ms.get("kg_per_batch", 3))  * float(ms.get("price_per_kg", 22.00))
                        )
                        mush_pu = mush_cost_per_batch / units
                    else:
                        # Flat mushroom cost from seed
                        mf = c.get("mushroom_fresh", {})
                        ms = c.get("mushroom_specialty", {})
                        mush_cost = (float(mf.get("kg_per_batch",16)) * float(mf.get("price_per_kg",11.66)) +
                                     float(ms.get("kg_per_batch",3))  * float(ms.get("price_per_kg",22.00)))
                        mush_pu = mush_cost / units
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
                    mush_cost = (float(mf.get("kg_per_batch",16)) * float(mf.get("price_per_kg",11.66)) +
                                 float(ms.get("kg_per_batch",3))  * float(ms.get("price_per_kg",22.00)))
                    mush_pu = mush_cost / units

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
    Only bones and mushrooms are extracted per-recipe — everything else
    (mirepoix, salt, spices) uses the flat other_ingredients_per_batch cost.
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
            result["bones_kg"] += amount
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
        breakdown.pop("mirepoix", None)

    return round(unit_cogs, 2), breakdown


@app.route("/cogs")
@login_required
def cogs_page():
    return render_template("cogs.html")


@app.route("/api/cogs", methods=["GET"])
@login_required
def get_cogs():
    c = _load_cogs()
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(c)
    return jsonify({
        "inputs": c,
        "matrix": matrix,
        "cost_per_run": cost_per_run,
        "avg_runs_per_month": avg_runs,
        "size_labels": {
            "475ml": "475ml SS",
            "750ml_ss": "750ml SS",
            "876ml": "876ml SS",
            "750ml_fz": "750ml FZ",
            "750ml_bb": "750ml BB",
            "750ml_pouch": "750ml Pouch",
        }
    })


@app.route("/api/cogs", methods=["PATCH"])
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


@app.route("/api/cogs/recipe/<path:recipe_name>", methods=["GET"])
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


@app.route("/api/cogs/reset", methods=["POST"])
@login_required
def reset_cogs():
    """Force-reset cogs.json to the current seed values. Used for migration."""
    seed = dict(_COGS_SEED, schema_version=COGS_SCHEMA_VERSION)
    _save_json(COGS_PATH, seed)
    matrix, cost_per_run, avg_runs = _compute_cogs_matrix(seed)
    return jsonify({"ok": True, "cost_per_run": cost_per_run,
                    "avg_runs": avg_runs, "matrix": matrix})


@app.route("/api/cogs/compute", methods=["POST"])
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


# ══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT & MAINTENANCE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _load_equipment():
    return _load_json(EQUIPMENT_PATH, [])

def _save_equipment(data):
    _save_json(EQUIPMENT_PATH, data)


@app.route("/company-settings")
@login_required
def company_settings_page():
    return render_template("company_settings.html")


@app.route("/important-documents")
@login_required
def important_documents_page():
    return render_template("important_documents.html")


@app.route("/equipment")
@login_required
def equipment_page():
    return render_template("equipment.html")


@app.route("/api/equipment", methods=["GET"])
@login_required
def get_equipment():
    return jsonify(_load_equipment())


@app.route("/api/equipment", methods=["POST"])
@login_required
def add_equipment():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    items = _load_equipment()
    entry = {
        "id":               "eq_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(len(items)),
        "name":             name,
        "type":             (data.get("type") or "").strip(),
        "model":            (data.get("model") or "").strip(),
        "serial_number":    (data.get("serial_number") or "").strip(),
        "asset_tag":        (data.get("asset_tag") or "").strip(),
        "location":         (data.get("location") or "").strip(),
        "status":           data.get("status", "active"),
        "purchase_date":    (data.get("purchase_date") or "").strip(),
        "purchase_price":   data.get("purchase_price"),
        "contact_primary":  (data.get("contact_primary") or "").strip(),
        "contact_secondary":(data.get("contact_secondary") or "").strip(),
        "warranty_expiry":  (data.get("warranty_expiry") or "").strip(),
        "warranty_contact": (data.get("warranty_contact") or "").strip(),
        "warranty_notes":   (data.get("warranty_notes") or "").strip(),
        "service_interval": (data.get("service_interval") or "").strip(),
        "last_service_date":(data.get("last_service_date") or "").strip(),
        "next_service_date":(data.get("next_service_date") or "").strip(),
        "notes":            (data.get("notes") or "").strip(),
        "service_log":      [],
        "created_at":       datetime.now().isoformat(),
    }
    items.append(entry)
    _save_equipment(items)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/equipment/<eq_id>", methods=["PUT"])
@login_required
def update_equipment(eq_id):
    data = request.get_json() or {}
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    fields = [
        "name", "type", "model", "serial_number", "asset_tag", "location",
        "status", "purchase_date", "purchase_price", "contact_primary",
        "contact_secondary", "warranty_expiry", "warranty_contact",
        "warranty_notes", "service_interval", "last_service_date",
        "next_service_date", "notes",
    ]
    for f in fields:
        if f in data:
            entry[f] = data[f]
    entry["updated_at"] = datetime.now().isoformat()
    _save_equipment(items)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/equipment/<eq_id>", methods=["DELETE"])
@login_required
def delete_equipment(eq_id):
    items = _load_equipment()
    items = [e for e in items if e["id"] != eq_id]
    _save_equipment(items)
    return jsonify({"ok": True})


@app.route("/api/equipment/<eq_id>/log", methods=["POST"])
@login_required
def add_service_log(eq_id):
    data = request.get_json() or {}
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    log_entry = {
        "id":           "log_" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "date":         (data.get("date") or datetime.now().strftime("%Y-%m-%d")),
        "type":         (data.get("type") or "Maintenance").strip(),
        "description":  (data.get("description") or "").strip(),
        "performed_by": (data.get("performed_by") or "").strip(),
        "cost":         data.get("cost"),
        "parts":        (data.get("parts") or "").strip(),
        "next_action":  (data.get("next_action") or "").strip(),
        "created_at":   datetime.now().isoformat(),
    }
    if not entry.get("service_log"):
        entry["service_log"] = []
    entry["service_log"].append(log_entry)
    # Auto-update last service date
    if log_entry["date"]:
        entry["last_service_date"] = log_entry["date"]
    entry["updated_at"] = datetime.now().isoformat()
    _save_equipment(items)
    return jsonify({"ok": True, "log_entry": log_entry})


@app.route("/api/equipment/<eq_id>/log/<log_id>", methods=["DELETE"])
@login_required
def delete_service_log(eq_id, log_id):
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    entry["service_log"] = [l for l in (entry.get("service_log") or []) if l["id"] != log_id]
    _save_equipment(items)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY AUDIT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def _load_audits():
    return _load_json(AUDITS_PATH, [])

def _save_audits(data):
    _save_json(AUDITS_PATH, data)

def _active_audit(kind):
    """Return the current in-progress audit of the given kind, or None."""
    return next((a for a in _load_audits()
                 if a.get("kind") == kind and a.get("status") == "in_progress"), None)


@app.route("/audit/<kind>")
@login_required
def audit_page(kind):
    """Render the audit page for 'rm' or 'fg'."""
    if kind not in ("rm", "fg"):
        return "Invalid audit type", 400
    return render_template("audit.html", kind=kind)


@app.route("/api/audit/start", methods=["POST"])
@login_required
def start_audit():
    """Start a new audit or return the existing in-progress one.
    Body: { kind: 'rm'|'fg', categories: [str] }
    """
    data = request.get_json() or {}
    kind = data.get("kind")
    if kind not in ("rm", "fg"):
        return jsonify({"error": "kind must be rm or fg"}), 400

    audits = _load_audits()

    # Abandon any previous in-progress audit of the same kind
    audits = [a for a in audits
              if not (a.get("kind") == kind and a.get("status") == "in_progress")]

    categories = data.get("categories", [])

    # Build the items list
    if kind == "rm":
        items = _build_rm_audit_items(categories)
    else:
        items = _build_fg_audit_items(categories)

    audit = {
        "id":         "audit_" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "kind":       kind,
        "status":     "in_progress",
        "categories": categories,
        "started_at": datetime.now().isoformat(),
        "items":      items,
        "current_idx": 0,
        "results":    {},  # {item_id: {counted, system_qty, ...}}
    }
    audits.append(audit)
    _save_audits(audits)
    return jsonify({"ok": True, "audit": audit})


def _build_rm_audit_items(categories):
    """Build ordered list of RM items for the audit, filtered by section categories."""
    materials = _load_json(ORGANIC_RAW_PATH, [])
    sections_data = _load_rm_sections()
    assignments = sections_data.get("assignments", {})
    sections = {s["id"]: s["name"] for s in sections_data.get("sections", [])}

    # Group by ingredient name, filter by selected categories
    seen = {}  # item_name -> {total_remaining, lots, unit, section}
    for mat in materials:
        remaining = float(mat.get("remaining") or 0)
        if remaining <= 0:
            continue
        item_name = (mat.get("item") or "").strip()
        if not item_name:
            continue
        section_id = assignments.get(item_name, "")
        section_name = sections.get(section_id, "") or "Unassigned"
        if categories and section_name not in categories:
            continue
        if item_name not in seen:
            seen[item_name] = {
                "id":      item_name,
                "name":    item_name,
                "unit":    mat.get("unit", ""),
                "section": section_name,
                "system_qty": 0,
                "lots":    [],
            }
        seen[item_name]["system_qty"] = round(seen[item_name]["system_qty"] + remaining, 4)
        seen[item_name]["lots"].append({
            "id":           mat.get("id"),
            "supplier_lot": mat.get("supplier_lot", ""),
            "date_received": mat.get("date_received", ""),
            "remaining":    remaining,
        })

    # Sort by section then name
    items = sorted(seen.values(), key=lambda x: (x["section"], x["name"]))
    return items


def _build_fg_audit_items(categories):
    """Build ordered list of FG lots for the audit, filtered by brand."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    items = []
    for entry in fg:
        if int(entry.get("quantity_remaining") or 0) <= 0:
            continue
        brand = (entry.get("brand") or "Unknown").strip()
        if categories and brand not in categories:
            continue
        recipe  = (entry.get("recipe") or "").strip()
        fmt     = (entry.get("format") or "").strip()
        lot     = (entry.get("lot") or "").strip()
        items.append({
            "id":         entry["id"],
            "recipe":     recipe,
            "brand":      brand,
            "format":     fmt,
            "lot":        lot,
            "sku":        f"{recipe} · {fmt}",
            "system_qty": int(entry.get("quantity_remaining") or 0),
            "created_at": entry.get("created_at", ""),
        })
    # Sort: brand → format (SS/FZ/BB) → recipe → lot date
    fmt_order = {"SS": 0, "FZ": 1, "BB": 2}
    items.sort(key=lambda x: (
        x["brand"],
        fmt_order.get((x["format"] or "")[:2].upper(), 9),
        x["recipe"],
        x.get("created_at", ""),
    ))
    return items


@app.route("/api/audit/active/<kind>", methods=["GET"])
@login_required
def get_active_audit(kind):
    """Return the current in-progress audit for rm or fg, or null."""
    audit = _active_audit(kind)
    return jsonify({"audit": audit})


@app.route("/api/audit/<audit_id>/save", methods=["POST"])
@login_required
def save_audit_progress(audit_id):
    """Save progress on an in-progress audit without completing it.
    Body: { current_idx: int, results: {item_id: {counted}} }
    """
    data = request.get_json() or {}
    audits = _load_audits()
    audit = next((a for a in audits if a["id"] == audit_id), None)
    if not audit:
        return jsonify({"error": "Audit not found"}), 404
    audit["current_idx"] = data.get("current_idx", audit["current_idx"])
    audit["results"].update(data.get("results", {}))
    audit["last_saved_at"] = datetime.now().isoformat()
    _save_audits(audits)
    return jsonify({"ok": True})


@app.route("/api/audit/<audit_id>/complete", methods=["POST"])
@login_required
def complete_audit(audit_id):
    """Complete an audit — apply all counted adjustments to inventory.
    Body: { results: {item_id: {counted}} }
    """
    data = request.get_json() or {}
    audits = _load_audits()
    audit = next((a for a in audits if a["id"] == audit_id), None)
    if not audit:
        return jsonify({"error": "Audit not found"}), 404

    # Merge final results
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
    """Apply RM audit results — adjust remaining on individual lots."""
    materials = _load_json(ORGANIC_RAW_PATH, [])
    adjustments = []

    for item_name, result in audit["results"].items():
        counted = result.get("counted")
        if counted is None:
            continue  # skipped

        counted = round(float(counted), 4)
        # Find all non-depleted lots for this item
        lots = sorted(
            [m for m in materials if (m.get("item") or "").strip() == item_name
             and float(m.get("remaining") or 0) > 0],
            key=lambda m: m.get("date_received", "")
        )
        if not lots:
            continue

        system_total = round(sum(float(m.get("remaining") or 0) for m in lots), 4)
        diff = round(counted - system_total, 4)
        if diff == 0:
            continue

        if diff < 0:
            # Decrease: take from oldest lots first (FIFO)
            to_remove = abs(diff)
            for lot in lots:
                if to_remove <= 0:
                    break
                avail = float(lot.get("remaining") or 0)
                take = min(avail, to_remove)
                lot["remaining"] = round(avail - take, 4)
                to_remove = round(to_remove - take, 4)
        else:
            # Increase: add to most recent lot
            lots[-1]["remaining"] = round(float(lots[-1].get("remaining") or 0) + diff, 4)

        adjustments.append({
            "item": item_name,
            "system_qty": system_total,
            "counted": counted,
            "diff": diff,
        })

    _save_json(ORGANIC_RAW_PATH, materials)

    # Record in adjustments log
    for adj in adjustments:
        _record_adjustment({
            "id":         "audit_rm_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(abs(int(adj["diff"]*100))),
            "kind":       "audit_rm",
            "item":       adj["item"],
            "system_qty": adj["system_qty"],
            "counted":    adj["counted"],
            "diff":       adj["diff"],
            "audit_id":   audit["id"],
            "created_at": datetime.now().isoformat(),
        })

    return adjustments


def _apply_fg_audit(audit):
    """Apply FG audit results — direct per-lot overwrite."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    adjustments = []

    for fg_id, result in audit["results"].items():
        counted = result.get("counted")
        if counted is None:
            continue
        counted = int(counted)
        entry = next((f for f in fg if f.get("id") == fg_id), None)
        if not entry:
            continue
        system_qty = int(entry.get("quantity_remaining") or 0)
        if counted == system_qty:
            continue
        entry["quantity_remaining"] = counted
        entry["last_adjusted_at"]   = datetime.now().isoformat()
        adjustments.append({
            "fg_id":      fg_id,
            "recipe":     entry.get("recipe", ""),
            "lot":        entry.get("lot", ""),
            "system_qty": system_qty,
            "counted":    counted,
            "diff":       counted - system_qty,
        })

    _save_json(ORGANIC_FG_PATH, fg)

    for adj in adjustments:
        _record_adjustment({
            "id":         "audit_fg_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(abs(adj["diff"])),
            "kind":       "audit_fg",
            "recipe":     adj["recipe"],
            "lot":        adj["lot"],
            "system_qty": adj["system_qty"],
            "counted":    adj["counted"],
            "diff":       adj["diff"],
            "audit_id":   audit["id"],
            "created_at": datetime.now().isoformat(),
        })

    return adjustments


@app.route("/api/audit/history", methods=["GET"])
@login_required
def audit_history():
    """Return completed audits, most recent first."""
    kind = request.args.get("kind")
    audits = _load_audits()
    completed = [a for a in audits if a.get("status") == "completed"]
    if kind:
        completed = [a for a in completed if a.get("kind") == kind]
    completed.sort(key=lambda a: a.get("completed_at", ""), reverse=True)
    return jsonify(completed)


@app.route("/api/audit/categories/<kind>", methods=["GET"])
@login_required
def audit_categories(kind):
    """Return available categories for category selection screen."""
    if kind == "rm":
        sections_data = _load_rm_sections()
        sections = {s["id"]: s["name"] for s in sections_data.get("sections", [])}
        assignments = sections_data.get("assignments", {})
        materials = _load_json(ORGANIC_RAW_PATH, [])

        # Find which section names actually have stock
        active_sections = set()
        has_unassigned = False
        for mat in materials:
            if float(mat.get("remaining") or 0) <= 0:
                continue
            item_name = (mat.get("item") or "").strip()
            section_id = assignments.get(item_name, "")
            section_name = sections.get(section_id, "")
            if section_name:
                active_sections.add(section_name)
            else:
                has_unassigned = True

        # Return sections in defined order, only those with stock
        cats = [s["name"] for s in sections_data.get("sections", [])
                if s["name"] in active_sections]
        if has_unassigned:
            cats.append("Unassigned")
        if not cats:
            cats = ["All"]
        return jsonify(cats)
    else:
        fg = _load_json(ORGANIC_FG_PATH, [])
        brands = sorted({(f.get("brand") or "Unknown").strip()
                         for f in fg if int(f.get("quantity_remaining") or 0) > 0})
        return jsonify(brands or ["All"])


@app.route("/api/organic/adjustments", methods=["GET"])
@login_required
def get_adjustments():
    """Return the full audit log of manual adjustments (additions + subtractions)."""
    return jsonify(_load_json(ADJUSTMENTS_PATH, []))


# ── Supplier catalog ────────────────────────────────────────────────────
# suppliers.json: list of {id, name, ingredients: [{name, unit}]}
# rm_receipt_photos/: <entry_id>.<ext> — one photo per add-inventory entry

def _load_suppliers():
    return _load_json(SUPPLIERS_PATH, [])

def _save_suppliers(data):
    _save_json(SUPPLIERS_PATH, data)


@app.route("/api/suppliers", methods=["GET"])
@login_required
def get_suppliers():
    return jsonify(_load_suppliers())


@app.route("/api/suppliers", methods=["POST"])
@login_required
def create_supplier():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    suppliers = _load_suppliers()
    if any(s["name"].lower() == name.lower() for s in suppliers):
        return jsonify({"error": "Supplier already exists"}), 409
    supplier = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "name": name,
        "ingredients": [],
    }
    suppliers.append(supplier)
    _save_suppliers(suppliers)
    return jsonify(supplier), 201


@app.route("/api/suppliers/<sid>", methods=["PUT"])
@login_required
def update_supplier(sid):
    data = request.get_json(force=True) or {}
    suppliers = _load_suppliers()
    idx = next((i for i, s in enumerate(suppliers) if s["id"] == sid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if any(s["name"].lower() == name.lower() and s["id"] != sid for s in suppliers):
            return jsonify({"error": "Name taken"}), 409
        suppliers[idx]["name"] = name
    if "ingredients" in data:
        suppliers[idx]["ingredients"] = data["ingredients"]
    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            suppliers[idx][field] = (data[field] or "").strip()
    _save_suppliers(suppliers)
    return jsonify(suppliers[idx])


@app.route("/api/suppliers/<sid>", methods=["DELETE"])
@login_required
def delete_supplier(sid):
    suppliers = _load_suppliers()
    suppliers = [s for s in suppliers if s["id"] != sid]
    _save_suppliers(suppliers)
    return jsonify({"ok": True})


@app.route("/api/suppliers/<sid>/ingredients", methods=["PUT"])
@login_required
def update_supplier_ingredients(sid):
    data = request.get_json(force=True) or {}
    ingredients = data.get("ingredients", [])
    suppliers = _load_suppliers()
    idx = next((i for i, s in enumerate(suppliers) if s["id"] == sid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    suppliers[idx]["ingredients"] = ingredients
    _save_suppliers(suppliers)
    return jsonify(suppliers[idx])


# ── RM receipt photo upload ──────────────────────────────────────────────
_RM_PHOTO_ALLOWED = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "pdf"}
_RM_PHOTO_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


@app.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["POST"])
@login_required
def upload_rm_receipt_photo(entry_id):
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "No file"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _RM_PHOTO_ALLOWED:
        return jsonify({"error": f"File type not supported"}), 400
    data = file.read()
    if len(data) > _RM_PHOTO_MAX_BYTES:
        return jsonify({"error": "File too large (max 20 MB)"}), 413
    for fn in os.listdir(RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            os.remove(os.path.join(RM_RECEIPT_PHOTOS_DIR, fn))
    filename = f"{entry_id}.{ext}"
    with open(os.path.join(RM_RECEIPT_PHOTOS_DIR, filename), "wb") as fh:
        fh.write(data)
    return jsonify({"ok": True, "filename": filename})


@app.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["GET"])
@login_required
def get_rm_receipt_photo(entry_id):
    for fn in os.listdir(RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            return send_from_directory(RM_RECEIPT_PHOTOS_DIR, fn)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["DELETE"])
@login_required
def delete_rm_receipt_photo(entry_id):
    for fn in os.listdir(RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            os.remove(os.path.join(RM_RECEIPT_PHOTOS_DIR, fn))
            return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


@app.route("/api/organic/raw-materials/receipt-photo-exists/<entry_id>", methods=["GET"])
@login_required
def rm_receipt_photo_exists(entry_id):
    for fn in os.listdir(RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            return jsonify({"exists": True, "filename": fn})
    return jsonify({"exists": False})


@app.route("/api/organic/finished-goods/grouped", methods=["GET"])
@login_required
def get_finished_goods_grouped():
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()
    grouped = _group_fg_with_catalog(fg, recipes)
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        grouped = [g for g in grouped
                   if (g.get("certification") or "").lower() == cert_filter.lower()]
    # Merge PAR levels and prices from sku_meta.json
    meta = _load_json(SKU_META_PATH, {})
    for g in grouped:
        m = meta.get(g["sku_key"], {})
        g["par"] = m.get("par")          # None = no PAR; int = PAR level
        g["price"] = m.get("price")      # None = unset; float = price per unit
    return jsonify(grouped)


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

    # Build stock map
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, [])
    meta = _load_json(SKU_META_PATH, {})
    company = _load_company_info()
    buffer_units = int(company.get("ripe_inventory_buffer") or 0)

    stock_map = {}
    for entry in fg:
        key = _sku_key(entry.get("brand",""), entry.get("recipe",""), entry.get("format",""))
        stock_map[key] = stock_map.get(key, 0) + int(entry.get("quantity_remaining") or 0)

    # Subtract committed (not yet deducted) sales
    lower_map = {k.lower(): k for k in stock_map}
    for sale in sales:
        if sale.get("deducted") is False:
            sk = sale.get("sku_key", "")
            canonical = stock_map.get(sk) and sk or lower_map.get(sk.lower())
            if canonical:
                stock_map[canonical] = max(0, stock_map[canonical] - int(sale.get("quantity") or 0))

    # Build catalogue from buyer's assigned SKUs
    catalogue = []
    for sku in (buyer.get("skus") or []):
        sk = sku.get("sku_key", "")
        gross = stock_map.get(sk, 0)
        available = max(0, gross - buffer_units)
        m = meta.get(sk, {})
        catalogue.append({
            "sku_key":     sk,
            "name":        sku.get("recipe", ""),
            "brand":       sku.get("brand", ""),
            "format":      sku.get("format", ""),
            "display":     sku.get("display", ""),
            "position":    sku.get("position", 999),
            # Buyer-specific pricing (set in Soma buyer record)
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
        # Order rules — sourced from Soma company settings
        "rules": {
            "ss_min_cases_delivery": int(company.get("ss_min_cases_delivery") or 40),
            "fzbb_small_lead_days":  int(company.get("fzbb_small_lead_days")  or 3),
            "fzbb_large_lead_days":  int(company.get("fzbb_large_lead_days")  or 7),
            "fzbb_large_threshold":  int(company.get("fzbb_large_threshold")  or 8),
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

    # Build Soma side: all active recipe sku_keys
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

    # Build FG side: which sku_keys actually have inventory
    fg = _load_json(ORGANIC_FG_PATH, [])
    fg_stock = {}
    for entry in fg:
        key = _sku_key(entry.get("brand",""), entry.get("recipe",""), entry.get("format",""))
        fg_stock[key] = fg_stock.get(key, 0) + int(entry.get("quantity_remaining") or 0)

    # Build Ripe side: fetch their product list
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

    # Audit each Ripe product
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
            # Warn if recipe name has dirty format suffix
            if soma_keys[sk]["has_format_in_name"]:
                result["status"] = "matched_dirty_name"
                result["issue"]  = f"Recipe name '{soma_keys[sk]['recipe_name']}' contains format suffix — should be cleaned"
        else:
            result["status"] = "broken"
            result["issue"]  = f"soma_sku_key '{sk}' not found in Soma recipes"
            # Suggest closest match
            suggestions = []
            sk_lower = sk.lower()
            for k, v in soma_keys.items():
                if v["recipe_name"].lower() in sk_lower or sk_lower in v["recipe_name"].lower():
                    suggestions.append(k)
            if suggestions:
                result["suggestions"] = suggestions
        ripe_audit.append(result)

    # Soma recipes with no Ripe product mapping
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

    # Summary
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
    Key-gated via X-Internal-Key. Returns {sku_key: units_available}.
    Excludes committed (scheduled-but-not-yet-deducted) Ripe sales.
    """
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    provided = request.headers.get("X-Internal-Key", "")
    import hmac as _hmac
    if not internal_key or not _hmac.compare_digest(provided.encode(), internal_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401

    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, [])
    meta = _load_json(SKU_META_PATH, {})
    company = _load_company_info()
    buffer_units = int(company.get("ripe_inventory_buffer") or 0)

    # Gross stock per SKU
    stock = {}
    for entry in fg:
        key = _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", ""))
        stock[key] = stock.get(key, 0) + int(entry.get("quantity_remaining") or 0)

    # Subtract scheduled (deducted=False) Ripe sales — inventory spoken for.
    # Build a lowercase lookup so legacy sale records with lowercased sku_keys
    # still correctly reduce the visible stock.
    stock_lower = {k.lower(): k for k in stock}
    for sale in sales:
        if sale.get("deducted") is False:
            sale_key = sale.get("sku_key", "")
            # Try exact match first, then case-insensitive fallback
            if sale_key in stock:
                matched_key = sale_key
            elif sale_key.lower() in stock_lower:
                matched_key = stock_lower[sale_key.lower()]
            else:
                continue
            stock[matched_key] = max(0, stock[matched_key] - int(sale.get("quantity") or 0))

    # Build response — apply buffer so Ripe sees conservative numbers
    result = {}
    for key, gross in stock.items():
        m = meta.get(key, {})
        available = max(0, gross - buffer_units)
        result[key] = {
            "available": available,
            "par": m.get("par"),
        }
    return jsonify(result)


@app.route("/api/sku-meta/<path:sku_key>", methods=["PATCH"])
@login_required
def update_sku_meta(sku_key):
    """Update PAR level for a SKU.
    Body: { par: int|null }
    null = remove the field (No PAR).
    Price is no longer stored here — it lives in the buyer catalogue.
    """
    data = request.get_json() or {}
    meta = _load_json(SKU_META_PATH, {})
    if sku_key not in meta:
        meta[sku_key] = {}
    if "par" in data:
        if data["par"] is None:
            meta[sku_key].pop("par", None)
        else:
            try:
                meta[sku_key]["par"] = int(data["par"])
            except (ValueError, TypeError):
                return jsonify({"error": "par must be an integer or null"}), 400
    # Silently ignore any price field — price lives in buyer catalogue now
    # Clean up empty entries
    if not meta[sku_key]:
        del meta[sku_key]
    _save_json(SKU_META_PATH, meta)
    return jsonify({"ok": True, "meta": meta.get(sku_key, {})})


@app.route("/api/sku-meta", methods=["GET"])
@login_required
def get_all_sku_meta():
    """Return all SKU meta — used by create_schedule page to check PAR warnings."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()
    grouped = _group_fg_with_catalog(fg, recipes)
    meta = _load_json(SKU_META_PATH, {})
    warnings = []
    for g in grouped:
        m = meta.get(g["sku_key"], {})
        par = m.get("par")
        if par is not None:
            remaining = g.get("total_remaining", 0)
            if remaining < par:
                warnings.append({
                    "sku_key": g["sku_key"],
                    "display": g.get("display", ""),
                    "par": par,
                    "remaining": remaining,
                    "shortfall": par - remaining,
                })
    return jsonify({"meta": meta, "par_warnings": warnings})


@app.route("/api/organic/finished-goods/sku/<path:sku_key>", methods=["GET"])
@login_required
def get_finished_goods_sku_detail(sku_key):
    """Return LOT-level FIFO detail for a single SKU.
    Each LOT row aggregates same-LOT entries from multiple kettles."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    lots = _aggregate_lots_for_sku(fg, sku_key)
    if not lots:
        # Validate that the SKU exists at all
        groups = _group_fg_by_sku(fg)
        if not any(g["sku_key"] == sku_key for g in groups):
            return jsonify({"error": "SKU not found"}), 404
    # Pull the SKU's display info from any matching FG entry
    display_info = None
    for entry in fg:
        if _sku_key(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", "")) == sku_key:
            display_info = {
                "sku_key": sku_key,
                "brand": entry.get("brand", ""),
                "recipe": entry.get("recipe", ""),
                "format": entry.get("format", ""),
                "display": _sku_display(entry.get("brand", ""), entry.get("recipe", ""), entry.get("format", "")),
            }
            break
    return jsonify({
        "sku": display_info or {"sku_key": sku_key},
        "lots": lots,
    })


# ── Company Info ──────────────────────────────────────────────────────────────
def _load_company_info():
    info = _load_json(COMPANY_INFO_PATH, {})
    merged = dict(_DEFAULT_COMPANY_INFO)
    merged.update(info)
    return merged


@app.route("/api/company-info", methods=["GET"])
@login_required
def get_company_info():
    return jsonify(_load_company_info())


@app.route("/api/company-info", methods=["PATCH"])
@login_required
def update_company_info():
    data = request.get_json() or {}
    info = _load_company_info()
    allowed = set(_DEFAULT_COMPANY_INFO.keys())
    for k, v in data.items():
        if k in allowed:
            if k == "ripe_inventory_buffer":
                try:
                    info[k] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
            else:
                info[k] = (v or "").strip()
    _save_json(COMPANY_INFO_PATH, info)
    return jsonify({"ok": True, "info": info})


# ── Organic: Sales ───────────────────────────────────────────────────
@app.route("/api/organic/sales", methods=["GET"])
@login_required
def get_organic_sales():
    """Return sales records. Optional query param ?certification=X filters
    to that tier. Sale records carry the certification of the SKU sold,
    derived from the recipe at creation time."""
    sales = _load_json(ORGANIC_SALES_PATH, [])
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        sales = [s for s in sales
                 if (s.get("certification") or "").lower() == cert_filter.lower()]
    return jsonify(sales)


@app.route("/api/organic/sales", methods=["POST"])
@login_required
def add_organic_sale():
    """Record a sale. Two body shapes accepted:

    NEW (preferred): {sku_key, quantity, buyer, sale_date, case_lot}
        FIFO-deducts across LOTs of that SKU (oldest production date first).
        Sale record stores a 'lots' array with the breakdown.

    LEGACY: {fg_id, quantity, buyer, sale_date, case_lot}
        Deducts from a specific FG entry (per-kettle batch). Kept for
        traceability flows that target a specific batch.
    """
    data = request.json or {}
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])

    try:
        quantity = int(data.get("quantity", 0))
    except (ValueError, TypeError):
        quantity = 0
    if quantity <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400

    sku_key = (data.get("sku_key") or "").strip()
    fg_id = (data.get("fg_id") or "").strip()

    # Auto-derive sku_key from brand/recipe/format if not supplied directly
    if not sku_key and not fg_id:
        brand_f = (data.get("brand") or "").strip()
        recipe_f = (data.get("recipe") or "").strip()
        format_f = (data.get("format") or "").strip()
        if recipe_f:
            sku_key = _sku_key(brand_f, recipe_f, format_f)
        else:
            return jsonify({"error": "Either sku_key, fg_id, or recipe required"}), 400

    sale_lots = []   # records what was deducted
    brand = recipe = fmt = ""

    if sku_key:
        # NEW path: FIFO across the SKU's LOTs (oldest production date first)
        # Build a list of FG entries belonging to this SKU, ordered FIFO
        candidates = [f for f in fg
                      if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
                      and (f.get("quantity_remaining") or 0) > 0]
        if not candidates:
            return jsonify({"error": "No inventory available for this SKU"}), 400

        def _entry_prod_date(e):
            wid = e.get("week_id")
            d_idx = e.get("day_idx")
            if wid is not None and d_idx is not None:
                try:
                    return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    pass
            return (e.get("created_at") or "")[:10]

        candidates.sort(key=lambda e: (_entry_prod_date(e), e.get("lot", ""), e.get("id", "")))

        total_available = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
        if quantity > total_available:
            return jsonify({"error": f"Not enough inventory: requested {quantity}, available {total_available}"}), 400

        first = candidates[0]
        brand = first.get("brand", "")
        recipe = first.get("recipe", "")
        fmt = first.get("format", "")

        remaining_to_deduct = quantity
        # Group same-LOT deductions for the sale's lots[] summary
        lot_summary = {}
        for entry in candidates:
            if remaining_to_deduct <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            if avail <= 0:
                continue
            take = min(avail, remaining_to_deduct)
            entry["quantity_remaining"] = avail - take
            remaining_to_deduct -= take
            lot = entry.get("lot", "")
            if lot not in lot_summary:
                lot_summary[lot] = {
                    "lot": lot, "quantity": 0,
                    "fg_ids": [],            # legacy/display convenience
                    "breakdown": [],         # exact per-fg_id deduction (for accurate restore)
                }
            lot_summary[lot]["quantity"] += take
            lot_summary[lot]["fg_ids"].append(entry.get("id"))
            lot_summary[lot]["breakdown"].append({
                "fg_id": entry.get("id"),
                "quantity": take,
            })
        sale_lots = list(lot_summary.values())

    else:
        # LEGACY path: single fg_id
        fg_entry = next((f for f in fg if f.get("id") == fg_id), None)
        if not fg_entry:
            return jsonify({"error": "Finished good not found"}), 404
        avail = int(fg_entry.get("quantity_remaining") or 0)
        if quantity > avail:
            return jsonify({"error": f"Not enough inventory: requested {quantity}, available {avail}"}), 400
        fg_entry["quantity_remaining"] = avail - quantity
        brand = fg_entry.get("brand", "")
        recipe = fg_entry.get("recipe", "")
        fmt = fg_entry.get("format", "")
        sale_lots = [{
            "lot": fg_entry.get("lot", ""),
            "quantity": quantity,
            "fg_ids": [fg_entry.get("id")],
            "breakdown": [{"fg_id": fg_entry.get("id"), "quantity": quantity}],
        }]

    # Determine certification from the FG entry(ies) we deducted from
    # (consistent within a SKU, so we can pull from the first one).
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
        "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales)),
        "sku_key": sku_key or _sku_key(brand, recipe, fmt),
        "brand": brand,
        "recipe": recipe,
        "format": fmt,
        "certification": sale_cert,
        "quantity": quantity,
        "lots": sale_lots,
        # Convenience fields for legacy display
        "fg_lot": (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
        "fg_id": (sale_lots[0]["fg_ids"][0] if len(sale_lots) == 1 and len(sale_lots[0]["fg_ids"]) == 1 else ""),
        "buyer": data.get("buyer", ""),
        "sale_date": data.get("sale_date", ""),
        "case_lot": data.get("case_lot", ""),
        "po_number": data.get("po_number", ""),
        "created_at": datetime.now().isoformat(),
    }
    sales.append(sale)
    _save_json(ORGANIC_SALES_PATH, sales)
    _save_json(ORGANIC_FG_PATH, fg)
    buyer = data.get("buyer", "").strip()
    if buyer:
        _add_contact("buyer", buyer)
    return jsonify({"success": True, "id": sale["id"], "sale": sale})


@app.route("/api/organic/sales/order", methods=["POST"])
@login_required
def add_sale_order():
    """Record a complete sale order — multiple SKUs in one transaction.

    Body: {
        buyer:            str,
        buyer_id:         str (optional),
        sale_date:        str YYYY-MM-DD,
        po_number:        str (optional),
        location_name:    str (optional),
        location_address: str (optional),
        lines: [
            { sku_key: str, brand: str, recipe: str, format: str, quantity: int },
            ...
        ]
    }

    All lines share one order_id. Each line gets its own sale record (for
    per-SKU LOT tracking and inventory deduction) but they are linked by
    order_id so the Records view, packing slip, and invoice treat them as
    one transaction.
    """
    data = request.get_json() or {}
    lines = data.get("lines") or []
    if not lines:
        return jsonify({"error": "lines array required"}), 400

    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg    = _load_json(ORGANIC_FG_PATH, [])

    buyer        = (data.get("buyer") or "").strip()
    sale_date    = data.get("sale_date") or datetime.now().date().isoformat()
    po_number    = (data.get("po_number") or "").strip()
    location_name    = (data.get("location_name") or "").strip()
    location_address = (data.get("location_address") or "").strip()

    # One order_id shared across all lines in this transaction
    order_id = "ORD-" + datetime.now().strftime("%Y%m%d%H%M%S")

    saved_ids   = []
    saved_sales = []
    errors      = []

    for line in lines:
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            continue

        sku_key    = (line.get("sku_key") or "").strip()
        brand      = (line.get("brand")   or "").strip()
        recipe     = (line.get("recipe")  or "").strip()
        fmt        = (line.get("format")  or "").strip()
        unit_price = line.get("unit_price")
        if unit_price is not None:
            try:
                unit_price = round(float(unit_price), 2)
            except (TypeError, ValueError):
                unit_price = None

        if not sku_key:
            if recipe:
                sku_key = _sku_key(brand, recipe, fmt)
            else:
                errors.append(f"Line missing sku_key and recipe: {line}")
                continue

        # FIFO deduction across this SKU's LOTs
        candidates = [
            f for f in fg
            if _sku_key(f.get("brand",""), f.get("recipe",""), f.get("format","")) == sku_key
            and int(f.get("quantity_remaining") or 0) > 0
        ]
        if not candidates:
            errors.append(f"No inventory for {sku_key}")
            continue

        def _prod_date(e):
            wid, d_idx = e.get("week_id"), e.get("day_idx")
            if wid and d_idx is not None:
                try:
                    return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return (e.get("created_at") or "")[:10]

        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot",""), e.get("id","")))
        total_available = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
        if quantity > total_available:
            errors.append(f"Insufficient stock for {sku_key}: need {quantity}, have {total_available}")
            continue

        # Deduct
        remaining = quantity
        lot_summary = {}
        for entry in candidates:
            if remaining <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            take  = min(avail, remaining)
            entry["quantity_remaining"] = avail - take
            remaining -= take
            lot = entry.get("lot", "")
            if lot not in lot_summary:
                lot_summary[lot] = {"lot": lot, "quantity": 0, "fg_ids": [], "breakdown": []}
            lot_summary[lot]["quantity"]  += take
            lot_summary[lot]["fg_ids"].append(entry.get("id"))
            lot_summary[lot]["breakdown"].append({"fg_id": entry.get("id"), "quantity": take})

        sale_lots = list(lot_summary.values())

        # Certification from first lot
        sale_cert = ""
        for lot_entry in sale_lots:
            for b in (lot_entry.get("breakdown") or []):
                target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                if target and target.get("certification"):
                    sale_cert = target["certification"]
                    break
            if sale_cert:
                break

        # Pull canonical brand/recipe/format from FG entry
        first = candidates[0]
        brand   = first.get("brand",   brand)
        recipe  = first.get("recipe",  recipe)
        fmt     = first.get("format",  fmt)

        line_total = round(unit_price * quantity, 2) if unit_price is not None else None
        sale = {
            "id":          "SL-" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "order_id":    order_id,
            "sku_key":     sku_key,
            "brand":       brand,
            "recipe":      recipe,
            "format":      fmt,
            "certification": sale_cert,
            "quantity":    quantity,
            "cases":       quantity // 12,
            "lots":        sale_lots,
            "fg_lot":      (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
            "fg_id":       (sale_lots[0]["fg_ids"][0] if len(sale_lots) == 1 and sale_lots[0]["fg_ids"] else ""),
            "buyer":       buyer,
            "buyer_id":    data.get("buyer_id", ""),
            "sale_date":   sale_date,
            "po_number":   po_number,
            "location_name":    location_name,
            "location_address": location_address,
            "unit_price":  unit_price,
            "line_total":  line_total,
            "created_at":  datetime.now().isoformat(),
        }
        saved_ids.append(sale["id"])
        saved_sales.append(sale)
        sales.append(sale)

    if errors and not saved_ids:
        return jsonify({"error": "All lines failed", "details": errors}), 400

    _save_json(ORGANIC_SALES_PATH, sales)
    _save_json(ORGANIC_FG_PATH, fg)

    if buyer:
        _add_contact("buyer", buyer)

    return jsonify({
        "success":  True,
        "order_id": order_id,
        "ids":      saved_ids,
        "saved":    len(saved_ids),
        "errors":   errors,
    })


@app.route("/api/organic/sales/<sale_id>", methods=["PATCH"])
@login_required
def edit_organic_sale(sale_id):
    """Edit a completed sale record. Supports updating:
      - sale_date
      - buyer
      - location_name / location_address
      - quantity (adjusts FG by the delta — restores or deducts)
      - po_number / notes

    Quantity changes: if new qty > old qty, tries to FIFO-deduct the difference.
    If new qty < old qty, restores the difference to the most recent LOT drawn.
    """
    data = request.get_json() or {}
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    idx = next((i for i, s in enumerate(sales) if s.get("id") == sale_id), None)
    if idx is None:
        return jsonify({"error": "Sale not found"}), 404
    sale = dict(sales[idx])

    for field in ("sale_date", "buyer", "po_number", "notes", "location_name", "location_address"):
        if field in data:
            sale[field] = (data[field] or "").strip()

    if "quantity" in data:
        new_qty = int(data["quantity"])
        if new_qty < 0:
            return jsonify({"error": "Quantity must be non-negative"}), 400
        old_qty = int(sale.get("quantity") or 0)
        delta = new_qty - old_qty

        if delta > 0:
            # Need to deduct more — FIFO from same SKU
            sku_key = sale.get("sku_key", "")
            candidates = [
                f for f in fg
                if _sku_key(f.get("brand",""), f.get("recipe",""), f.get("format","")) == sku_key
                and int(f.get("quantity_remaining") or 0) > 0
            ]
            candidates.sort(key=lambda e: (e.get("created_at",""), e.get("lot","")))
            remaining = delta
            for entry in candidates:
                if remaining <= 0:
                    break
                avail = int(entry.get("quantity_remaining") or 0)
                take = min(avail, remaining)
                entry["quantity_remaining"] = avail - take
                remaining -= take
            if remaining > 0:
                return jsonify({"error": f"Insufficient FG stock for delta of +{delta} units"}), 422

        elif delta < 0:
            # Restore units — put back into the most recent LOT drawn
            restore = abs(delta)
            lots = sale.get("lots") or []
            for lot_info in reversed(lots):
                if restore <= 0:
                    break
                for fg_entry in fg:
                    if fg_entry.get("lot") == lot_info.get("lot") and restore > 0:
                        fg_entry["quantity_remaining"] = int(fg_entry.get("quantity_remaining") or 0) + restore
                        restore = 0
                        break
            # If we couldn't trace back to a specific LOT, restore to most recent entry
            if restore > 0:
                sku_key = sale.get("sku_key", "")
                matching = [f for f in fg if _sku_key(f.get("brand",""), f.get("recipe",""), f.get("format","")) == sku_key]
                if matching:
                    matching.sort(key=lambda e: e.get("created_at",""), reverse=True)
                    matching[0]["quantity_remaining"] = int(matching[0].get("quantity_remaining") or 0) + restore

        sale["quantity"] = new_qty
        _save_json(ORGANIC_FG_PATH, fg)

    sale["edited_at"] = datetime.now().isoformat()
    sales[idx] = sale
    _save_json(ORGANIC_SALES_PATH, sales)
    return jsonify({"ok": True, "sale": sale})


@app.route("/api/organic/sales/<sale_id>", methods=["DELETE"])
@login_required
def delete_organic_sale(sale_id):
    """Restore quantity back to the FG entries the sale drew from.
    Handles both new (lots[] array) and legacy (single fg_id) sale shapes."""
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"success": True})  # Already gone

    # Restore inventory
    if sale.get("lots"):
        for lot_entry in sale["lots"]:
            qty_to_restore = int(lot_entry.get("quantity") or 0)
            if qty_to_restore <= 0:
                continue
            # Preferred: per-fg_id breakdown (post-2026-05 sales) — restores
            # exactly to where each unit was deducted from.
            breakdown = lot_entry.get("breakdown")
            if breakdown:
                for b in breakdown:
                    target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                    if target:
                        target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(b.get("quantity") or 0)
                continue
            # Fallback: legacy lot record without breakdown — restore everything
            # to the first matching fg_id (best-effort; preserves SKU total).
            fg_ids = lot_entry.get("fg_ids") or []
            for fid in fg_ids:
                target = next((f for f in fg if f.get("id") == fid), None)
                if target:
                    target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + qty_to_restore
                    break
    elif sale.get("fg_id"):
        # Pre-multi-LOT legacy shape
        target = next((f for f in fg if f.get("id") == sale["fg_id"]), None)
        if target:
            target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(sale.get("quantity") or 0)

    sales = [s for s in sales if s.get("id") != sale_id]
    _save_json(ORGANIC_SALES_PATH, sales)
    _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})



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
        # Old shape — wrap in single-LOT array
        if s.get("fg_id") or s.get("fg_lot"):
            s["lots"] = [{
                "lot": s.get("fg_lot", ""),
                "quantity": int(s.get("quantity") or 0),
                "fg_ids": [s["fg_id"]] if s.get("fg_id") else [],
            }]
            # Add sku_key for completeness
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
    return _load_json(BUYERS_PATH, [])


def _save_buyers(data):
    _save_json(BUYERS_PATH, data)


@app.route("/api/buyers", methods=["GET"])
@login_required
def get_buyers():
    return jsonify(_load_buyers())


@app.route("/api/buyers/sku-catalog", methods=["GET"])
@login_required
def get_buyer_sku_catalog():
    catalog = _all_sku_catalog()
    groups = {}
    for sku in catalog:
        b = sku["brand"] or "No Brand"
        if b not in groups:
            groups[b] = []
        groups[b].append(sku)
    return jsonify([{"brand": b, "skus": groups[b]} for b in sorted(groups.keys())])


@app.route("/api/buyers", methods=["POST"])
@login_required
def create_buyer():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    buyers = _load_buyers()
    if any(b["name"].lower() == name.lower() for b in buyers):
        return jsonify({"error": "Buyer already exists"}), 409
    sku_catalog = _all_sku_catalog()
    default_skus = [s for s in sku_catalog if s["brand"].lower() == name.lower()]
    buyer = {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
             "name": name, "skus": default_skus}
    buyers.append(buyer)
    _save_buyers(buyers)
    return jsonify(buyer), 201


@app.route("/api/buyers/<bid>", methods=["PUT"])
@login_required
def update_buyer(bid):
    data = request.get_json(force=True) or {}
    buyers = _load_buyers()
    idx = next((i for i, b in enumerate(buyers) if b["id"] == bid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if any(b["name"].lower() == name.lower() and b["id"] != bid for b in buyers):
            return jsonify({"error": "Name taken"}), 409
        buyers[idx]["name"] = name
    if "skus" in data:
        # Preserve pricing fields if they already exist and incoming data
        # doesn't explicitly include them (allows partial SKU updates)
        existing_by_key = {s.get("sku_key",""): s for s in (buyers[idx].get("skus") or [])}
        new_skus = []
        for sku in data["skus"]:
            key = sku.get("sku_key", "")
            existing = existing_by_key.get(key, {})
            merged = dict(existing)
            merged.update(sku)
            # Validate and round pricing fields
            for pf in ("price", "cogs", "margin_pct"):
                if pf in merged and merged[pf] is not None:
                    try:
                        merged[pf] = round(float(merged[pf]), 2)
                    except (TypeError, ValueError):
                        merged[pf] = None
            new_skus.append(merged)
        buyers[idx]["skus"] = new_skus
    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            buyers[idx][field] = (data[field] or "").strip()
    if "locations" in data:
        # locations: [{id, name, address}] — client manages IDs
        locs = data["locations"]
        if isinstance(locs, list):
            buyers[idx]["locations"] = [
                {"id": l.get("id") or str(i),
                 "name": (l.get("name") or "").strip(),
                 "address": (l.get("address") or "").strip()}
                for i, l in enumerate(locs)
                if (l.get("name") or "").strip()
            ]
    _save_buyers(buyers)
    return jsonify(buyers[idx])


@app.route("/api/buyers/<bid>/skus/<path:sku_key>/pricing", methods=["PATCH"])
@login_required
def update_buyer_sku_pricing(bid, sku_key):
    """Update pricing for a single SKU on a buyer.
    Body: { price, cogs, margin_pct, buyer_sku, active }
    Computes missing values using the price=cogs*(1+margin/100) relationship.
    """
    data = request.get_json() or {}
    buyers = _load_buyers()
    idx = next((i for i, b in enumerate(buyers) if b["id"] == bid), None)
    if idx is None:
        return jsonify({"error": "Buyer not found"}), 404

    skus = buyers[idx].get("skus") or []
    sku_idx = next((i for i, s in enumerate(skus) if s.get("sku_key") == sku_key), None)
    if sku_idx is None:
        return jsonify({"error": "SKU not assigned to this buyer"}), 404

    sku = dict(skus[sku_idx])

    # Derive pricing triangle: price = cogs * (1 + margin/100)
    price      = float(data["price"])      if "price"      in data and data["price"]      is not None else sku.get("price")
    cogs       = float(data["cogs"])       if "cogs"       in data and data["cogs"]       is not None else sku.get("cogs")
    margin_pct = float(data["margin_pct"]) if "margin_pct" in data and data["margin_pct"] is not None else sku.get("margin_pct")

    if price is not None and cogs is not None:
        margin_pct = round(((price / cogs) - 1) * 100, 2) if cogs > 0 else 0.0
    elif price is not None and margin_pct is not None:
        cogs = round(price / (1 + margin_pct / 100), 2) if (1 + margin_pct / 100) > 0 else price
    elif cogs is not None and margin_pct is not None:
        price = round(cogs * (1 + margin_pct / 100), 2)

    if price      is not None: sku["price"]      = round(price, 2)
    if cogs       is not None: sku["cogs"]        = round(cogs, 2)
    if margin_pct is not None: sku["margin_pct"]  = round(margin_pct, 2)

    if "buyer_sku" in data:
        sku["buyer_sku"] = (data["buyer_sku"] or "").strip()
    if "active" in data:
        sku["active"] = bool(data["active"])

    skus[sku_idx] = sku
    buyers[idx]["skus"] = skus
    _save_buyers(buyers)
    return jsonify({"ok": True, "sku": sku})


@app.route("/api/buyers/<bid>", methods=["DELETE"])
@login_required
def delete_buyer(bid):
    _save_buyers([b for b in _load_buyers() if b["id"] != bid])
    return jsonify({"ok": True})


# ── Sale documents ────────────────────────────────────────────────────────
@app.route("/api/organic/sales/<sale_id>/packing-slip", methods=["GET"])
@login_required
def get_packing_slip(sale_id):
    sales = _load_json(ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"error": "Sale not found"}), 404

    # Collect all lines for this order (order_id groups multi-SKU transactions)
    order_id = sale.get("order_id")
    if order_id:
        order_lines = [s for s in sales if s.get("order_id") == order_id]
        order_lines.sort(key=lambda s: s.get("created_at", ""))
    else:
        order_lines = [sale]  # legacy single-line sale

    company = _load_company_info()
    buyers = _load_buyers()
    buyer_name = sale.get("buyer") or "—"
    buyer_rec = next((b for b in buyers if b.get("name") == buyer_name), {})
    buyer_address = sale.get("location_address") or buyer_rec.get("address") or ""
    buyer_contact = sale.get("location_name") or ""
    buyer_phone = buyer_rec.get("phone") or ""
    buyer_email = buyer_rec.get("email") or ""

    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable, Image)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
    import io as _io

    DARK_GREEN  = colors.HexColor("#1b5e20")
    MID_GREEN   = colors.HexColor("#2e7d32")
    LIGHT_GREEN = colors.HexColor("#e8f5e9")
    BORDER      = colors.HexColor("#c8d8c8")
    GREY_TEXT   = colors.HexColor("#555555")
    LIGHT_ROW   = colors.HexColor("#f5f9f5")

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=0.65*inch, leftMargin=0.65*inch,
                            topMargin=0.55*inch, bottomMargin=0.65*inch)
    styles = getSampleStyleSheet()

    def _ps(name, **kw):
        base = styles.get(name, styles["Normal"])
        return ParagraphStyle("_"+name+"_"+str(abs(hash(str(kw)))), parent=base, **kw)

    story = []

    # Header: logo left, company info right
    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")

    company_lines = [company.get("name") or "Soma Bone Broth"]
    for fld in ("address", "city", "phone", "email", "website"):
        if company.get(fld): company_lines.append(company[fld])

    company_para = Paragraph(
        "<br/>".join(company_lines),
        _ps("Normal", fontSize=8, textColor=GREY_TEXT, alignment=TA_RIGHT, leading=12)
    )

    if os.path.exists(logo_path):
        logo_img = Image(logo_path, width=1.4*inch, height=1.4*inch, kind="proportional")
        header_tbl = Table([[logo_img, company_para]], colWidths=[2.5*inch, 5.0*inch])
        header_tbl.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN",  (1,0), (1,0),  "RIGHT"),
        ]))
    else:
        header_tbl = Table(
            [[Paragraph(company.get("name") or "Soma Bone Broth",
                        _ps("Normal", fontSize=18, textColor=DARK_GREEN, fontName="Helvetica-Bold")),
              company_para]],
            colWidths=[3.0*inch, 4.5*inch]
        )
    story.append(header_tbl)
    story.append(Spacer(1, 0.1*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=MID_GREEN, spaceAfter=6))
    story.append(Paragraph("PACKING SLIP",
        _ps("Normal", fontSize=20, fontName="Helvetica-Bold", textColor=DARK_GREEN, spaceAfter=2)))

    # Ship To / Order Info block
    sale_date = sale.get("sale_date") or "—"
    po        = sale.get("po_number") or sale.get("case_lot") or "—"
    ref       = sale_id[-10:]

    lbl   = _ps("Normal", fontSize=8, textColor=GREY_TEXT, fontName="Helvetica-Bold", leading=11, spaceBefore=2)
    val   = _ps("Normal", fontSize=10, leading=13)
    val_s = _ps("Normal", fontSize=9, textColor=GREY_TEXT, leading=12)

    ship_lines = [f"<b>{buyer_name}</b>"]
    if buyer_contact: ship_lines.append(buyer_contact)
    if buyer_address: ship_lines.append(buyer_address)
    if buyer_phone:   ship_lines.append(buyer_phone)
    if buyer_email:   ship_lines.append(buyer_email)
    ship_para = Paragraph("<br/>".join(ship_lines), val)

    order_info = Table([
        [Paragraph("SHIP TO", lbl), ship_para,
         Paragraph("DATE",      lbl), Paragraph(sale_date, val)],
        ["", "", Paragraph("ORDER REF", lbl), Paragraph(ref, val_s)],
        ["", "", Paragraph("PO #",      lbl), Paragraph(po,  val_s)],
    ], colWidths=[0.9*inch, 3.6*inch, 1.0*inch, 2.0*inch])
    order_info.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("SPAN",          (0,0), (0,2)),
        ("SPAN",          (1,0), (1,2)),
        ("BACKGROUND",    (0,0), (1,2),  LIGHT_GREEN),
        ("BOX",           (0,0), (1,2),  0.5, BORDER),
        ("BOX",           (2,0), (3,2),  0.5, BORDER),
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(Spacer(1, 0.15*inch))
    story.append(order_info)
    story.append(Spacer(1, 0.2*inch))

    # Items table — one row per SKU line across all order lines
    hdr_s  = _ps("Normal", fontSize=9, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER)
    cell_l = _ps("Normal", fontSize=10, alignment=TA_LEFT)
    cell_c = _ps("Normal", fontSize=10, alignment=TA_CENTER)
    tot_s  = _ps("Normal", fontSize=10, fontName="Helvetica-Bold", alignment=TA_CENTER)

    rows = [[Paragraph("PRODUCT", hdr_s), Paragraph("FORMAT", hdr_s),
             Paragraph("LOT #", hdr_s),   Paragraph("QTY (units)", hdr_s)]]
    total_units = 0

    for line in order_lines:
        lp  = ((line.get("brand","")+" " if line.get("brand") else "") + (line.get("recipe") or "")).strip()
        lf  = line.get("format") or ""
        ll  = line.get("lots") or []
        if ll:
            for lot in ll:
                qty = int(lot.get("quantity") or 0)
                total_units += qty
                rows.append([Paragraph(lp, cell_l), Paragraph(lf, cell_c),
                             Paragraph(lot.get("lot") or "—", cell_c), Paragraph(str(qty), cell_c)])
        else:
            qty = int(line.get("quantity") or 0)
            total_units += qty
            rows.append([Paragraph(lp, cell_l), Paragraph(lf, cell_c),
                         Paragraph(line.get("fg_lot") or "—", cell_c), Paragraph(str(qty), cell_c)])
    total_cases = total_units // 12
    rows.append(["", "", Paragraph("TOTAL", tot_s),
                 Paragraph(f"{total_units} units  ({total_cases} cases)", tot_s)])

    rc = len(rows)
    items_tbl = Table(rows, colWidths=[3.2*inch, 1.1*inch, 1.4*inch, 1.8*inch])
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),    MID_GREEN),
        ("TEXTCOLOR",     (0,0),  (-1,0),    colors.white),
        ("ROWBACKGROUNDS",(0,1),  (-1,rc-2), [colors.white, LIGHT_ROW]),
        ("BACKGROUND",    (0,-1), (-1,-1),   LIGHT_GREEN),
        ("GRID",          (0,0),  (-1,rc-2), 0.5, BORDER),
        ("LINEABOVE",     (0,-1), (-1,-1),   1.0, MID_GREEN),
        ("TOPPADDING",    (0,0),  (-1,-1),   8),
        ("BOTTOMPADDING", (0,0),  (-1,-1),   8),
        ("LEFTPADDING",   (0,0),  (-1,-1),   8),
        ("RIGHTPADDING",  (0,0),  (-1,-1),   8),
        ("VALIGN",        (0,0),  (-1,-1),   "MIDDLE"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 0.3*inch))

    # Footer
    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))
    footer = ["Thank you for your business."]
    if company.get("registration"): footer.append(f"Reg: {company['registration']}")
    story.append(Paragraph("  |  ".join(footer),
                            _ps("Normal", fontSize=8, textColor=GREY_TEXT, alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    safe_buyer = "".join(c for c in buyer_name if c.isalnum() or c in "-_ ")[:20]
    return send_file(buf, mimetype="application/pdf", as_attachment=False,
                     download_name=f"packing-slip-{safe_buyer}-{sale_date}.pdf")


@app.route("/api/organic/sales/<sale_id>/qbo-csv", methods=["GET"])
@login_required
def get_qbo_csv(sale_id):
    sales = _load_json(ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"error": "Sale not found"}), 404
    import csv
    import io as _io
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["InvoiceNo","Customer","InvoiceDate","DueDate",
                     "Item(Product/Service)","ItemQuantity","ItemRate","ItemAmount","Memo"])
    lots = sale.get("lots") or []
    product = ((sale.get("brand","")+" " if sale.get("brand") else "") +
               (sale.get("recipe") or "") +
               (" "+sale.get("format") if sale.get("format") else "")).strip()
    lot_str = ", ".join(l.get("lot","") for l in lots if l.get("lot"))
    total_qty = sum(int(l.get("quantity") or 0) for l in lots)
    writer.writerow([sale_id[-8:].upper(), sale.get("buyer",""),
                     sale.get("sale_date",""), sale.get("sale_date",""),
                     product, total_qty, "", "", f"LOT#: {lot_str}" if lot_str else ""])
    buf.seek(0)
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=invoice-{sale_id[-8:].upper()}.csv"})


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


@app.route("/api/organic/trace", methods=["GET"])
@login_required
def organic_trace():
    search_type = request.args.get("type", "")  # "raw_lot" or "fg_lot"
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})

    runs = _load_json(ORGANIC_RUNS_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sales = _load_json(ORGANIC_SALES_PATH, [])

    if search_type == "raw_lot":
        # Find all runs that used this raw material LOT
        matched_runs = []
        for run in runs:
            for ing in run.get("ingredients_used", []):
                if ing.get("supplier_lot", "").lower() == query.lower():
                    matched_runs.append(run)
                    break
        # Find finished goods from those runs
        run_ids = {r["id"] for r in matched_runs}
        matched_fg = [f for f in fg if f.get("run_id") in run_ids]
        # Find sales of those finished goods (handle both new lots[] and legacy fg_id)
        fg_ids = {f["id"] for f in matched_fg}
        matched_sales = [s for s in sales if _sale_touches_fg(s, fg_ids)]
        return jsonify({
            "search_type": "raw_lot",
            "query": query,
            "runs": matched_runs,
            "finished_goods": matched_fg,
            "sales": matched_sales,
        })

    elif search_type == "fg_lot":
        # Find finished goods with this LOT
        matched_fg = [f for f in fg if f.get("lot", "").lower() == query.lower()]
        # Find runs that produced them
        run_ids = {f.get("run_id") for f in matched_fg}
        matched_runs = [r for r in runs if r.get("id") in run_ids]
        # Find sales (handle both new lots[] and legacy fg_id)
        fg_ids = {f["id"] for f in matched_fg}
        matched_sales = [s for s in sales if _sale_touches_fg(s, fg_ids)]
        return jsonify({
            "search_type": "fg_lot",
            "query": query,
            "runs": matched_runs,
            "finished_goods": matched_fg,
            "sales": matched_sales,
        })

    return jsonify({"results": []})


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

    # Build the set of (day_idx, vessel) → desired organic recipe for this week
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
            # Universal inventory: every scheduled recipe gets a production run
            # tracked, regardless of certification (Organic, Pasture Raised,
            # Conventional, or untagged). The certification flows from the
            # recipe to the FG entry at completion time.
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
        # Completed runs are immutable from the schedule side
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
                # Recompute LOT from the new schedule context
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
        # Check if a completed run already exists for this slot (don't duplicate)
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
        # Compute finish day = start day + 1 (with Sunday→next Monday rollover)
        try:
            start_dt = datetime.strptime(sw, "%Y-%m-%d") + timedelta(days=int(sd))
        except (ValueError, TypeError):
            continue
        finish_dt = start_dt + timedelta(days=1)
        # Find the Monday of finish_dt's week
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
        def _prod_date(e):
            wid = e.get("week_id")
            d_idx = e.get("day_idx")
            if wid and d_idx is not None:
                try:
                    return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return (e.get("created_at") or "")[:10]

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
            meta[key] = {"par": 100, "price": 10.00}
            changed = True
        # Never modify existing entries — user may have intentionally
        # removed par (No PAR) or price fields
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


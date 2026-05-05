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

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "soma-bone-broth-2026-change-me")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "soma2026")
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
        session["authenticated"] = True
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
    name, this is treated as a rename. We REFUSE to silently overwrite a
    different existing recipe — caller must choose a non-colliding name."""
    data = request.json or {}
    recipes = load_recipes()
    new_name = (data.get("name") or name).strip()
    recipe_data = data.get("data", {})

    if name not in recipes:
        return jsonify({"error": f"Recipe '{name}' not found"}), 404

    # Rename collision check: if the new name is different AND is already
    # taken by a different recipe, refuse. This prevents the edit form from
    # accidentally overwriting an unrelated recipe (which was previously
    # destroying data when a user renamed a duplicate to match an original).
    if new_name != name and new_name in recipes:
        return jsonify({
            "error": f"A different recipe named '{new_name}' already exists. "
                     f"Choose a different name or delete the other one first."
        }), 409

    if new_name != name:
        del recipes[name]
    recipes[new_name] = recipe_data
    save_recipes(recipes)
    return jsonify({"success": True, "name": new_name})

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
        display = brand + "-" + name
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
    for field in ["signoff_kitchen", "signoff_manager", "kitchen_lead", "production_manager"]:
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
# Manual inventory adjustments log (additions and subtractions outside of
# production runs and sales). Each entry records what changed, why, and
# which LOT(s) were drained or created. Used for audit traceability.
ADJUSTMENTS_PATH = os.path.join(INVENTORY_DIR, "adjustments.json")
# Camera-scan request log: per-day rolling counter for daily-limit enforcement
# plus an audit trail of every scan (success or failure).
SCAN_LOG_PATH = os.path.join(INVENTORY_DIR, "scan_log.json")
SCAN_DAILY_LIMIT = int(os.environ.get("SCAN_DAILY_LIMIT", "50"))


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


# ── Raw material category buckets for the inventory list view ──
# Each ingredient is bucketed into one of these labels for grouping.
# Order matters: this is the display order AND the rule-evaluation order
# (first match wins). Items not matching any specific bucket fall through
# to "4. Everything Else".
RAW_CATEGORIES = [
    "1. Bones & Proteins",
    "2. Adjuncts",
    "3. Mushrooms & Mushroom Powders",
    "4. Everything Else",
]


def _categorize_ingredient(name):
    """Return the display category bucket for a raw material name.
    Lowercase substring matching — order of rules matters (first match wins).

    Buckets, in evaluation order:
      1. Bones & Proteins — bones, feet, neck, lamb meat, turkey
      2. Adjuncts — anything with 'adjunct' in the name (wins over mushroom)
      3. Mushrooms & Mushroom Powders — anything with 'mushroom'
      4. Everything Else — vegetables, herbs, spices, salts, liquids, etc.
    """
    if not name:
        return "4. Everything Else"
    n = name.lower()

    # 1. Bones & proteins
    if ("bone" in n or "feet" in n or "neck" in n or "turkey" in n
            or ("lamb" in n and "meat" in n)):
        return "1. Bones & Proteins"
    # 2. Adjuncts — checked BEFORE mushrooms so 'Mushroom Adjunct' lands here
    if "adjunct" in n:
        return "2. Adjuncts"
    # 3. Mushrooms (whole, dried, powders)
    if "mushroom" in n:
        return "3. Mushrooms & Mushroom Powders"
    # 4. Catch-all
    return "4. Everything Else"


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

        rows.append({
            "name": ing["name"],
            "unit": ing["unit"],
            "category": _categorize_ingredient(ing["name"]),
            "total_received": total_received,
            "total_remaining": total_remaining,
            "lot_count": agg.get("lot_count", 0),
            "active_lot_count": agg.get("active_lot_count", 0),
            "has_baseline": agg.get("has_baseline", False),
            "catalog_only": (agg.get("lot_count", 0) == 0),
        })

    # Sort: by category index first, then alphabetically by name within each
    cat_index = {c: i for i, c in enumerate(RAW_CATEGORIES)}
    rows.sort(key=lambda r: (cat_index.get(r["category"], 99), r["name"].lower(), r["unit"]))
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
            if s.get("fg_id") == fg_id:
                try:
                    already_sold += int(s.get("quantity", 0))
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

    out = list(base.values())
    out.sort(key=lambda r: ((r.get("brand") or "").lower(),
                            (r.get("recipe") or "").lower(),
                            r.get("format") or ""))
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


@app.route("/api/organic/adjustments", methods=["GET"])
@login_required
def get_adjustments():
    """Return the full audit log of manual adjustments (additions + subtractions)."""
    return jsonify(_load_json(ADJUSTMENTS_PATH, []))


# ── Camera-scan endpoints ────────────────────────────────────────────────
# Two flows for camera-driven data entry:
#   POST /api/scan/invoice      — receive inventory (raw materials)
#   POST /api/scan/packing-slip — record sale (finished goods)
# Both accept a multipart upload, call Claude Vision, and return parsed
# line items WITH server-side fuzzy matches against the canonical catalogs.
# The frontend renders an editable review grid; user confirms or corrects;
# submission happens through the existing bulk endpoints, NOT directly here.

import vision_scan  # local module

# Allowed image extensions for scans (mirrors the invoice module list)
_SCAN_ALLOWED_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif"}
_SCAN_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _scan_log_today_count():
    """Return how many scans have been performed today (UTC)."""
    log = _load_json(SCAN_LOG_PATH, [])
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for entry in log if (entry.get("date") or "") == today)


def _record_scan(kind, success, error=None, line_count=0):
    """Append a scan log entry. Used for both daily-limit and audit."""
    log = _load_json(SCAN_LOG_PATH, [])
    log.append({
        "kind": kind,
        "success": bool(success),
        "error": error,
        "line_count": line_count,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat(),
    })
    # Keep log bounded — last 1000 entries should cover months of normal use
    if len(log) > 1000:
        log = log[-1000:]
    _save_json(SCAN_LOG_PATH, log)


def _build_ingredient_catalog_for_match():
    """Return a flat list of {name, unit} canonical ingredients. Same source
    of truth as the picker dropdown: active recipes + custom items, water
    excluded."""
    catalog = []
    seen = set()
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
                if key in seen:
                    continue
                seen.add(key)
                catalog.append({"name": ing_name, "unit": unit})
    custom = _load_json(ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        nm = (c.get("name") or "").strip()
        un = (c.get("unit") or "").strip()
        if not nm or not un or is_untracked_ingredient(nm):
            continue
        key = (nm.lower(), un)
        if key in seen:
            continue
        seen.add(key)
        catalog.append({"name": nm, "unit": un})
    return catalog


def _build_sku_catalog_for_match():
    """Return a flat list of SKUs (active recipes only) for fuzzy matching."""
    recipes = load_recipes()
    catalog = []
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        brand = (rdata.get("brand") or "").strip()
        fmt = (rdata.get("format") or "").strip()
        sku_key = _sku_key(brand, rname, fmt)
        catalog.append({
            "sku_key": sku_key,
            "brand": brand,
            "recipe": rname,
            "format": fmt,
            "display": _sku_display(brand, rname, fmt),
        })
    return catalog


def _validate_scan_upload(file):
    """Common upload validation. Returns (bytes, filename) or raises with a
    Flask-friendly response tuple."""
    if not file or not file.filename:
        return None, ("No file uploaded.", 400)
    name = file.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _SCAN_ALLOWED_EXTS:
        return None, (f"File type '{ext}' not supported. Use JPG/PNG/WEBP/HEIC.", 400)
    file_bytes = file.read()
    if len(file_bytes) == 0:
        return None, ("Uploaded file is empty.", 400)
    if len(file_bytes) > _SCAN_MAX_BYTES:
        mb = round(len(file_bytes) / (1024 * 1024), 1)
        return None, (f"File too large: {mb} MB. Max is 10 MB.", 413)
    return (file_bytes, name), None


@app.route("/api/scan/usage", methods=["GET"])
@login_required
def get_scan_usage():
    """Return today's scan count and the configured daily limit."""
    return jsonify({
        "today_count": _scan_log_today_count(),
        "daily_limit": SCAN_DAILY_LIMIT,
        "remaining": max(0, SCAN_DAILY_LIMIT - _scan_log_today_count()),
    })


@app.route("/api/scan/invoice", methods=["POST"])
@login_required
def scan_invoice():
    """Camera-scan an invoice photo. Returns parsed receive-inventory lines
    with server-side fuzzy matches. The user reviews/edits in a frontend
    grid and submits through /api/organic/raw-materials/bulk with baseline=false."""
    # Daily limit check
    if _scan_log_today_count() >= SCAN_DAILY_LIMIT:
        return jsonify({
            "error": f"Daily scan limit reached ({SCAN_DAILY_LIMIT}/day). "
                     "Limit resets at midnight. Use manual entry below.",
            "daily_limit": SCAN_DAILY_LIMIT,
        }), 429

    file = request.files.get("file")
    upload, err = _validate_scan_upload(file)
    if err:
        msg, code = err
        return jsonify({"error": msg}), code
    file_bytes, filename = upload

    try:
        parsed = vision_scan.extract_invoice_lines(file_bytes, filename)
    except RuntimeError as e:
        _record_scan("invoice", success=False, error=str(e))
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 — last-resort safeguard
        _record_scan("invoice", success=False, error=f"Unexpected: {e}")
        return jsonify({"error": f"Scan failed unexpectedly: {e}"}), 500

    # Match each line against the canonical ingredient catalog
    catalog = _build_ingredient_catalog_for_match()
    matched_lines = []
    for line in parsed.get("line_items", []):
        result = vision_scan.match_ingredient(
            line["raw_name"], line["unit"], catalog
        )
        matched_lines.append({
            **line,
            "unit_normalized": vision_scan.normalize_unit(line["unit"]),
            "match": result["match"],
            "confidence": result["confidence"],
            "score": result["score"],
        })

    _record_scan("invoice", success=True, line_count=len(matched_lines))

    return jsonify({
        "success": True,
        "supplier": parsed.get("supplier", ""),
        "invoice_date": parsed.get("invoice_date", ""),
        "invoice_number": parsed.get("invoice_number", ""),
        "notes": parsed.get("notes", ""),
        "line_items": matched_lines,
        "catalog": catalog,  # frontend uses this to populate dropdowns
        "scan_remaining": max(0, SCAN_DAILY_LIMIT - _scan_log_today_count()),
    })


@app.route("/api/scan/packing-slip", methods=["POST"])
@login_required
def scan_packing_slip():
    """Camera-scan a packing slip / PO photo. Returns parsed sale lines
    with server-side SKU matches. The user reviews/edits in a frontend
    grid and submits through /api/organic/sales (one call per line)."""
    if _scan_log_today_count() >= SCAN_DAILY_LIMIT:
        return jsonify({
            "error": f"Daily scan limit reached ({SCAN_DAILY_LIMIT}/day). "
                     "Limit resets at midnight. Use manual entry below.",
            "daily_limit": SCAN_DAILY_LIMIT,
        }), 429

    file = request.files.get("file")
    upload, err = _validate_scan_upload(file)
    if err:
        msg, code = err
        return jsonify({"error": msg}), code
    file_bytes, filename = upload

    try:
        parsed = vision_scan.extract_packing_slip_lines(file_bytes, filename)
    except RuntimeError as e:
        _record_scan("packing_slip", success=False, error=str(e))
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        _record_scan("packing_slip", success=False, error=f"Unexpected: {e}")
        return jsonify({"error": f"Scan failed unexpectedly: {e}"}), 500

    catalog = _build_sku_catalog_for_match()
    matched_lines = []
    for line in parsed.get("line_items", []):
        result = vision_scan.match_sku(
            line["raw_name"], line["format_hint"], catalog
        )
        matched_lines.append({
            **line,
            "match": result["match"],
            "confidence": result["confidence"],
            "score": result["score"],
        })

    _record_scan("packing_slip", success=True, line_count=len(matched_lines))

    return jsonify({
        "success": True,
        "buyer": parsed.get("buyer", ""),
        "ship_date": parsed.get("ship_date", ""),
        "po_number": parsed.get("po_number", ""),
        "notes": parsed.get("notes", ""),
        "line_items": matched_lines,
        "catalog": catalog,
        "scan_remaining": max(0, SCAN_DAILY_LIMIT - _scan_log_today_count()),
    })


@app.route("/api/scan/log", methods=["GET"])
@login_required
def get_scan_log():
    """Return the scan audit log (most recent first, capped at 200)."""
    log = _load_json(SCAN_LOG_PATH, [])
    log = sorted(log, key=lambda e: e.get("created_at", ""), reverse=True)[:200]
    return jsonify({
        "entries": log,
        "today_count": _scan_log_today_count(),
        "daily_limit": SCAN_DAILY_LIMIT,
    })


@app.route("/api/organic/finished-goods/grouped", methods=["GET"])
@login_required
def get_finished_goods_grouped():
    """Returns one row per SKU (brand+recipe+format), joined against the recipe
    catalog so EVERY active recipe shows up — even ones with zero stock or no
    production history. Each existing-stock row aggregates total produced and
    remaining across all kettles + LOTs. Catalog-only rows have catalog_only=true
    and total_remaining=0.

    Use this for the inventory list view; click a row and call /sku/<key> for
    LOT-level detail.

    Optional query parameter: ?certification=Organic|Pasture Raised|Conventional
    filters to one tier. Default is no filter (all certifications)."""
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()
    grouped = _group_fg_with_catalog(fg, recipes)
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        grouped = [g for g in grouped
                   if (g.get("certification") or "").lower() == cert_filter.lower()]
    return jsonify(grouped)


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

    if not sku_key and not fg_id:
        return jsonify({"error": "Either sku_key or fg_id required"}), 400

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
        "created_at": datetime.now().isoformat(),
    }
    sales.append(sale)
    _save_json(ORGANIC_SALES_PATH, sales)
    _save_json(ORGANIC_FG_PATH, fg)
    buyer = data.get("buyer", "").strip()
    if buyer:
        _add_contact("buyer", buyer)
    return jsonify({"success": True, "sale": sale})


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


_migrate_legacy_sales()
_autotag_existing_organic_data()


# ── Organic: Search / Trace ──────────────────────────────────────────
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
        # Find sales of those finished goods
        fg_ids = {f["id"] for f in matched_fg}
        matched_sales = [s for s in sales if s.get("fg_id") in fg_ids]
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
        # Find sales
        fg_ids = {f["id"] for f in matched_fg}
        matched_sales = [s for s in sales if s.get("fg_id") in fg_ids]
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

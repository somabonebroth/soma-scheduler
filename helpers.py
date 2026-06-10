"""helpers.py — foundation layer extracted from app.py.

Dependency-free (stdlib only) primitives shared across the app: filesystem-safe
JSON IO with per-path locks, path/config constants, and pure format/SKU/date
helpers. app.py imports these back. See CLAUDE.md "Pending architectural work".

This module must NOT import app.py (would create a circular import).
"""
import os
import json
import re
import threading
from datetime import datetime, timedelta


DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))


INVENTORY_DIR = os.path.join(DATA_DIR, "inventory")


ORGANIC_RUNS_PATH = os.path.join(INVENTORY_DIR, "production_runs.json")


ORGANIC_CONTACTS_PATH = os.path.join(INVENTORY_DIR, "contacts.json")


COMPANY_INFO_PATH = os.path.join(INVENTORY_DIR, "company_info.json")


ADJUSTMENTS_PATH = os.path.join(INVENTORY_DIR, "adjustments.json")


RM_SECTIONS_PATH = os.path.join(INVENTORY_DIR, "rm_sections.json")


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
    "ripe_credits": [],            # list of {id, name, amount} account credits Ripe can apply to e-transfer orders; auto-deplete on Soma approval
    "ss_small_order_threshold":  20,    # SS delivery minimum: below this delivery is rejected; at/above it delivery is free
    "fzbb_small_lead_days":  3,    # min days notice for FZ/BB ≤ threshold
    "fzbb_large_lead_days":  7,    # min days notice for FZ/BB ≥ threshold
    "fzbb_large_threshold":  8,    # cases at which large lead time applies
}


DEFAULT_RM_SECTIONS = [
    {"id": "bones", "name": "Bones"},
    {"id": "mirepoix", "name": "Mirepoix"},
    {"id": "herbs", "name": "Herbs"},
    {"id": "adjuncts", "name": "Adjuncts & Pre-Packs"},
    {"id": "mushrooms", "name": "Mushrooms"},
    {"id": "spices_other", "name": "Spices & Other"},
]


_FILE_LOCKS: dict = {}


_FILE_LOCKS_LOCK = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    """Return (creating if needed) the threading lock for a given file path."""
    with _FILE_LOCKS_LOCK:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]


def _load_json(path, default=None):
    """Read a JSON file under a per-path threading lock.
    Returns `default` (or [] if not given) when the file is missing.
    """
    with _get_file_lock(path):
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return default if default is not None else []


def _save_json(path, data):
    """Write JSON atomically under a per-path threading lock.
    Writes to .tmp first then renames — so a crash mid-write
    leaves the original intact rather than a truncated file.
    """
    tmp = path + ".tmp"
    with _get_file_lock(path):
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


FORMAT_PREFIX_CANONICAL = {
    "SS": "SS",
    "FZ": "FZ",
    "BB": "BB",
}


FORMAT_RE = re.compile(r"\b([A-Za-z]{1,4})[\s-]*(\d+)\s*ML\b", re.IGNORECASE)


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


def _sku_key(brand, recipe, fmt):
    """Stable identifier for a SKU group: 'BRAND|RECIPE|FORMAT'.
    Format is normalized so 'SS-750ML' and 'ss-750ml' collapse into one SKU.
    Separator chosen so it can't appear in any of the components."""
    return "|".join([(brand or ""), (recipe or ""), _normalize_format(fmt or "")])


def _sku_display(brand, recipe, fmt):
    """Human-readable SKU label using the canonical helper."""
    return build_display_name({"brand": brand, "format": fmt}, recipe_name=recipe)


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


def _ingredient_section_key(name, unit):
    """Storage key for ingredient assignment lookups."""
    return f"{(name or '').strip()}|{(unit or '').strip()}"


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
        seed = {
            "sections": [
                {"id": s["id"], "name": s["name"], "order": i}
                for i, s in enumerate(DEFAULT_RM_SECTIONS)
            ],
            "assignments": {},
        }
        _save_json(RM_SECTIONS_PATH, seed)
        return seed

    sections = data.get("sections")
    if not isinstance(sections, list):
        sections = []
    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    return {"sections": sections, "assignments": assignments}


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


def _add_contact(contact_type, name):
    """Append a contact name to the organic contacts store, de-duplicated by type."""
    contacts = _load_json(ORGANIC_CONTACTS_PATH, {})
    if contact_type not in contacts:
        contacts[contact_type] = []
    if name not in contacts[contact_type]:
        contacts[contact_type].append(name)
        _save_json(ORGANIC_CONTACTS_PATH, contacts)


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

        if lot and len(lot) == 6 and lot.isdigit():
            r["best_before"] = f"{lot[0:2]}/{lot[2:4]}/20{lot[4:6]}"

    out = []
    for lot, r in rows.items():
        r["vessels"] = sorted(r["vessels"])
        out.append(r)
    # FIFO: oldest production date first; depleted lots sorted within their date
    out.sort(key=lambda r: (r["production_date"] or "9999-99-99", r["lot"]))
    return out


def _record_adjustment(record):
    """Append an adjustment record to the audit log."""
    log = _load_json(ADJUSTMENTS_PATH, [])
    log.append(record)
    _save_json(ADJUSTMENTS_PATH, log)


def _load_company_info():
    """Load company info / order-rules JSON, falling back to the default template."""
    info = _load_json(COMPANY_INFO_PATH, {})
    merged = dict(_DEFAULT_COMPANY_INFO)
    merged.update(info)
    return merged


def _sanitize_ripe_credits(raw):
    """Coerce a raw ripe_credits payload into a clean list for storage.
    Each entry: {id, name, amount}. Drops fully-blank rows; clamps amount >= 0."""
    out = []
    if not isinstance(raw, list):
        return out
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        try:
            amt = max(0.0, round(float(c.get("amount") or 0), 2))
        except (TypeError, ValueError):
            amt = 0.0
        if not name and amt <= 0:
            continue  # fully blank row — drop
        cid = str(c.get("id") or "").strip() or f"c{i}"
        out.append({"id": cid, "name": name or "Credit", "amount": amt})
    return out


def _active_ripe_credits(company):
    """Usable credits Ripe should see: only amount > 0. Migrates a legacy
    scalar `ripe_credit` (v1) into a single named credit when no list exists."""
    raw = company.get("ripe_credits")
    if isinstance(raw, list):
        return [c for c in _sanitize_ripe_credits(raw) if c["amount"] > 0]
    try:
        legacy = round(float(company.get("ripe_credit") or 0), 2)
    except (TypeError, ValueError):
        legacy = 0.0
    return [{"id": "legacy", "name": "Credit", "amount": legacy}] if legacy > 0 else []


def _prod_date(e):
    """FIFO sort key: production date for a finished-goods entry.
    Derives YYYY-MM-DD from week_id + day_idx; falls back to created_at."""
    wid, d_idx = e.get("week_id"), e.get("day_idx")
    if wid and d_idx is not None:
        try:
            return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
        except Exception:
            pass
    return (e.get("created_at") or "")[:10]


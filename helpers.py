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
    "ripe_credits": [],            # list of {id, name, amount, kind} account credits Ripe can apply to e-transfer orders; auto-deplete on Soma approval. kind "once" (hand-issued) or "monthly" (the running balance of a ripe_monthly_promos entry; carries template_id, month, issued, month_issued)
    "ripe_monthly_promos": [],     # list of {id, name, amount} standing promos: `amount` is ADDED to the promo's running credit on the 1st of every month; nothing expires
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
                "production_date": None,    # batch START date, YYYY-MM-DD
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

        # Production date = the day the batch STARTED — the date the LOT# (and so
        # Best Before) is derived from. week_id/day_idx on an FG row are the
        # FINISH (count) day, one day later, so prefer the start coords; rows
        # without them fall back to the finish day, then created_at.
        prod_date = None
        wid = entry.get("start_week_id")
        d_idx = entry.get("start_day_idx")
        if wid is None or d_idx is None:
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
    """Company info / order rules with defaults filled in. This is the ONE read
    path, so the monthly-promo renewal runs here: the first read in a new month
    adds each promo's amount to its running credit and writes the file back — every consumer (settings, Ripe's catalogue, approve, the
    ledger) then agrees on what Ripe can use."""
    info = _load_json(COMPANY_INFO_PATH, {})
    if _renew_monthly_credits(info):
        _save_json(COMPANY_INFO_PATH, info)
    merged = dict(_DEFAULT_COMPANY_INFO)
    merged.update(info)
    return merged


def _sanitize_ripe_credits(raw):
    """Coerce a raw ripe_credits payload into a clean list for storage.
    Each entry: {id, name, amount, kind}. Drops fully-blank rows; clamps
    amount >= 0. A "monthly" entry (the running balance of a monthly promo)
    also keeps template_id, month (last month topped up), issued (cumulative,
    for the ledger) and month_issued (what this month's top-up was, so a
    mid-month edit can move the balance by the difference)."""
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
        entry = {"id": cid, "name": name or "Credit", "amount": amt,
                 "kind": "monthly" if c.get("kind") == "monthly" else "once"}
        if entry["kind"] == "monthly":
            entry["template_id"] = str(c.get("template_id") or "")
            entry["month"] = str(c.get("month") or "")
            for key in ("issued", "month_issued"):
                try:
                    entry[key] = max(0.0, round(float(c.get(key)), 2))
                except (TypeError, ValueError):
                    entry[key] = amt
        out.append(entry)
    return out


def _sanitize_monthly_promos(raw):
    """Coerce a raw ripe_monthly_promos payload: {id, name, amount}. Drops
    blank rows (a $0 monthly promo is kept but issues nothing)."""
    out = []
    if not isinstance(raw, list):
        return out
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        try:
            amt = max(0.0, round(float(p.get("amount") or 0), 2))
        except (TypeError, ValueError):
            amt = 0.0
        if not name and amt <= 0:
            continue
        pid = str(p.get("id") or "").strip() or f"p{i}"
        out.append({"id": pid, "name": name or "Monthly promo", "amount": amt})
    return out


def _promo_month(today=None):
    """Current month as YYYY-MM in Toronto time (the business's calendar)."""
    if today is None:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Toronto"))
    return today.strftime("%Y-%m")


def _renew_monthly_credits(info, today=None):
    """Bring `info["ripe_credits"]` up to date with `info["ripe_monthly_promos"]`.
    Mutates `info`; returns True when anything changed.

    - Each promo has ONE running credit (id = the promo id, kind "monthly").
      On the first read of a new month the promo's amount is ADDED to that
      balance — nothing expires, unused credit simply adds up. A month is
      topped up once (`month` records the last one).
    - Editing the promo mid-month moves the balance by the difference against
      what this month's top-up was (`month_issued`), floored at 0; the credit
      is renamed to match the promo.
    - Removing a promo stops the top-ups; the balance already granted is kept
      as an ordinary one-time credit (visible and deletable in Settings).
    """
    promos = _sanitize_monthly_promos(info.get("ripe_monthly_promos"))
    raw = info.get("ripe_credits")
    monthly_present = isinstance(raw, list) and any(
        isinstance(c, dict) and c.get("kind") == "monthly" for c in raw)
    if not promos and not monthly_present:
        return False
    month = _promo_month(today)
    if isinstance(raw, list):
        credits = _sanitize_ripe_credits(raw)
    else:
        credits = _sanitize_ripe_credits(_active_ripe_credits(info))  # migrate the legacy scalar first
    changed = not isinstance(raw, list)
    live = {p["id"]: p for p in promos}

    # One balance per promo. Merge any stray per-month instances (the first
    # cut issued `<promo>-YYYY-MM` credits) and orphans become one-time.
    balances, kept = {}, []
    for c in credits:
        if c["kind"] != "monthly":
            kept.append(c)
            continue
        tid = c.get("template_id")
        if tid not in live:
            kept.append({"id": c["id"], "name": c["name"], "amount": c["amount"], "kind": "once"})
            changed = True
            continue
        b = balances.get(tid)
        if b is None:
            balances[tid] = c
            if c["id"] != tid:
                c["id"] = tid
                changed = True
        else:
            b["amount"] = round(b["amount"] + c["amount"], 2)
            b["issued"] = round(b["issued"] + c["issued"], 2)
            if c.get("month", "") > b.get("month", ""):
                b["month"], b["month_issued"] = c["month"], c["month_issued"]
            changed = True
    credits = kept

    for p in promos:
        b = balances.get(p["id"])
        if b is None:
            if p["amount"] <= 0:
                continue
            b = {"id": p["id"], "name": p["name"], "amount": p["amount"],
                 "issued": p["amount"], "month_issued": p["amount"],
                 "kind": "monthly", "template_id": p["id"], "month": month}
            changed = True
        elif b["month"] != month:
            b["amount"] = round(b["amount"] + p["amount"], 2)
            b["issued"] = round(b["issued"] + p["amount"], 2)
            b["month"], b["month_issued"] = month, p["amount"]
            changed = True
        elif abs(b["month_issued"] - p["amount"]) > 0.005:
            delta = round(p["amount"] - b["month_issued"], 2)
            b["amount"] = max(0.0, round(b["amount"] + delta, 2))
            b["issued"] = max(0.0, round(b["issued"] + delta, 2))
            b["month_issued"] = p["amount"]
            changed = True
        if b["name"] != p["name"]:
            b["name"] = p["name"]
            changed = True
        credits.append(b)

    if changed:
        info["ripe_credits"] = credits
        info.pop("ripe_credit", None)
    return changed


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


LOT_SHELF_LIFE_DAYS = 365


def _lot_for_batch_date(batch_date):
    """THE rule for a production LOT#: the date the batch STARTED + 365 days,
    as ddmmyy. The LOT# is therefore also the Best Before date, and it is the
    number hot-stamped on the jar and printed on the case label.

    Every surface that shows a production LOT# (FG record, production run,
    tablet, schedule pages, schedule/recipe-card/checklist PDFs, case label)
    must come through here — never re-derive it locally. Jars are counted the
    day AFTER the batch starts, so a caller holding a finish/count date must
    step back one day first. `batch_date` is a date or datetime.
    """
    return (batch_date + timedelta(days=LOT_SHELF_LIFE_DAYS)).strftime("%d%m%y")


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



def _in_date_window(value, date_from, date_to):
    """Is an ISO date string inside an optional [date_from, date_to] window?

    Both bounds are optional and INCLUSIVE; a missing bound means unbounded, so
    no params at all means "everything" — the pre-filter behaviour, unchanged.
    A record with no/blank date is KEPT: dropping undated rows from a record
    view would silently hide data, which is worse than showing it.

    Deliberately a plain string compare — ISO dates sort lexicographically, so
    this is exactly the predicate a database would express as
    `WHERE col BETWEEN ? AND ?`. Keeping the semantics identical is what makes
    the eventual swap to SQL a change of implementation, not of contract.
    """
    d = (value or "")[:10]
    if not d:
        return True
    if date_from and d < date_from:
        return False
    if date_to and d > date_to:
        return False
    return True

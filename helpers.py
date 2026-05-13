"""
helpers.py — Shared utilities for Soma app and blueprints.

Imported by app.py, cogs.py, and future blueprints.
Avoids circular imports by having no imports from app.py.
"""
import os
import json
import logging
import threading
from functools import wraps

from flask import session, jsonify, render_template

logger = logging.getLogger(__name__)

# ── Environment / paths ───────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")

# ── File locking ─────────────────────────────────────────────────────────────
_FILE_LOCKS: dict = {}
_FILE_LOCKS_LOCK = threading.Lock()

def _get_file_lock(path: str) -> threading.Lock:
    """Return (creating if needed) the lock for a given file path."""
    with _FILE_LOCKS_LOCK:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = threading.Lock()
        return _FILE_LOCKS[path]

# ── JSON persistence ─────────────────────────────────────────────────────────
def _load_json(path, default=None):
    """Read a JSON file under a per-path threading lock.
    Returns `default` (or [] if not given) when the file is missing.
    Holding the lock during reads prevents a concurrent write from
    producing a partial/corrupt read.
    """
    lock = _get_file_lock(path)
    with lock:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
        return default if default is not None else []

def _save_json(path, data):
    """Write JSON atomically under a per-path threading lock.
    Writes to a .tmp file first, then renames — so a crash mid-write
    leaves the original intact rather than a truncated file.
    """
    lock = _get_file_lock(path)
    tmp = path + ".tmp"
    with lock:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

# ── Auth decorator ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ── Template rendering ───────────────────────────────────────────────────────

# ── Format normalisation ──────────────────────────────────────────────────────
import re as _re

RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")

# Regex to parse format strings like "SS-750ML", "ss750ml", "FZ 780"
FORMAT_RE = _re.compile(
    r"\b(SS|FZ|BB|POUCH|IQ)[\s\-_]*(473|474|475|476|500|735|750|780|876|880)\s*(?:ML)?\b",
    _re.IGNORECASE,
)

def _normalize_format(fmt: str) -> str:
    """Normalize a format string to canonical form e.g. 'ss-750ml' -> 'SS-750ML'."""
    if not fmt:
        return ""
    m = FORMAT_RE.search(fmt)
    if not m:
        return fmt.upper().strip()
    prefix = m.group(1).upper()
    size   = m.group(2)
    # Map size variants to canonical
    size_map = {"473": "473", "474": "473", "475": "473", "476": "473",
                 "735": "750", "750": "750", "780": "780", "876": "876",
                 "880": "876", "500": "500"}
    canonical_size = size_map.get(size, size)
    return f"{prefix}-{canonical_size}ML"


def load_recipes() -> dict:
    """Load recipes.json. Returns dict of recipe_name -> recipe_data.
    No migration logic — raw load for use by cogs blueprint.
    For the full migrating version, see app.py load_recipes().
    """
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            try:
                data = json.load(f)
                if isinstance(data, list):
                    return {r.get("name", ""): r for r in data if r.get("name")}
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, KeyError):
                pass
    return {}

_TEMPLATE_CONTRACTS: dict = {
    "buyer_edit.html":    {"buyer", "sku_catalog", "sku_map"},
    "analytics.html":     {"buyer_names"},
    "buyer_analytics.html": {"buyer_name"},
    "contacts.html": set(),
    "organic.html": set(),
    "cogs.html": set(),
    "dashboard.html": set(),
    "recipes.html": set(),
    "audit.html":         {"kind"},
    "equipment.html": set(),
    "company_settings.html": set(),
    "certifications.html": set(),
    "traceability.html": set(),
    "production_tracker.html": set(),
    "weekly_view.html":   {"week_id"},
    "daily_production.html": {"week_id", "day_idx"},
    "create_schedule.html": set(),
    "master_ccp.html": set(),
    "important_documents.html": set(),
    "login.html": set(),
    "checklist.html":     {"week_id", "day_idx"},
}


_render_logger = logging.getLogger("soma.render")

def _render(template_name: str, **context):
    """Wrapper around render_template that validates required variables.
    Logs a warning if the template contract is violated so the bug is
    caught at request time rather than mid-render.
    """
    required = _TEMPLATE_CONTRACTS.get(template_name, set())
    missing  = required - set(context.keys())
    if missing:
        _render_logger.warning(
            "render(%s) missing required variables: %s  -- will likely crash",
            template_name, sorted(missing)
        )
    return render_template(template_name, **context)

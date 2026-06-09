"""finished_goods.py — Finished-goods inventory blueprint extracted from app.py.

Sixth step of the app.py split (CLAUDE.md "Pending architectural work"), and the
first slice of the large "inventory" domain. Scope: the 10 finished-goods routes
(/api/organic/finished-goods*): list/update/delete, lot-adjust, baseline (+bulk),
manual add/subtract, grouped, and per-SKU detail. The rest of inventory (raw
materials, sku-meta, and the audit-critical reconcile/trace/mass-balance tools)
stays in app.py for later, separate steps.

Pattern (matches buyers/recipes/sales): routes move, shared helpers STAY. The FG
grouping helpers (_group_fg_by_sku, _group_fg_with_catalog) and load_recipes stay
in app.py — sku-meta and other routes there call them — and the constants
ORGANIC_FG_PATH / SKU_META_PATH live in app.py; all reached via `import app` +
app.-qualification. Foundation names (_load_json, _save_json, _sku_key,
_sku_display, _aggregate_lots_for_sku, _record_adjustment) import directly from
helpers, same as suppliers.py.

This blueprint touches NO raw-material consumption-chain code (no deduction,
reconcile, or trace helpers) — those audit-critical paths are untouched.

Defines its own login_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

from helpers import (
    _load_json,
    _save_json,
    _sku_key,
    _sku_display,
    _aggregate_lots_for_sku,
    _record_adjustment,
)

import app

finished_goods_bp = Blueprint("finished_goods", __name__)


def login_required(f):
    """Local copy of app.py's decorator (verbatim) — keeps the blueprint
    free of an import-time dependency on app.py. Behaviour is identical."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


@finished_goods_bp.route("/api/organic/finished-goods", methods=["GET"])
@login_required
def get_finished_goods():
    """Returns raw per-kettle FG entries. Used by traceability/legacy callers."""
    return jsonify(_load_json(app.ORGANIC_FG_PATH, []))


@finished_goods_bp.route("/api/organic/finished-goods/<fg_id>", methods=["PUT"])
@login_required
def update_finished_good(fg_id):
    """Manually adjust the remaining quantity on a finished goods entry.
    Body: {remaining}. Use for inventory corrections (loss, breakage, recount).
    The original quantity_produced and LOT# are preserved unchanged."""
    data = request.json or {}
    fg = _load_json(app.ORGANIC_FG_PATH, [])
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
    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "entry": entry})


@finished_goods_bp.route("/api/organic/finished-goods/<fg_id>", methods=["DELETE"])
@login_required
def delete_finished_good(fg_id):
    """Delete a finished goods entry. Sales already made against this entry
    keep their records (they stand as historical sales) but no inventory
    is restored anywhere — the FG entry is simply removed."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    if not any(f.get("id") == fg_id for f in fg):
        return jsonify({"error": "Finished goods entry not found"}), 404
    fg = [f for f in fg if f.get("id") != fg_id]
    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})


@finished_goods_bp.route("/api/organic/finished-goods/lot-adjust", methods=["POST"])
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

    fg = _load_json(app.ORGANIC_FG_PATH, [])
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
        # don't try to redistribute proportionally because the user's intent
        # is "the LOT total should be N", and the bucket is logically one).
        first = matching[0]
        first["quantity_remaining"] = int(first.get("quantity_remaining") or 0) + delta
        first["last_adjusted_at"] = datetime.now().isoformat()

    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "previous_total": current_total,
                    "new_total": new_remaining, "warnings": warnings})


@finished_goods_bp.route("/api/organic/finished-goods/baseline", methods=["POST"])
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

    recipes = app.load_recipes()
    recipe = recipes.get(recipe_name)
    if not recipe:
        return jsonify({"error": f"Recipe '{recipe_name}' not found"}), 404

    lot = (data.get("lot") or "").strip() or ("BL-" + datetime.now().strftime("%d%m%y"))

    fg = _load_json(app.ORGANIC_FG_PATH, [])
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
    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "entry": entry})


@finished_goods_bp.route("/api/organic/finished-goods/baseline-bulk", methods=["POST"])
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
    recipes = app.load_recipes()
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
    fg = _load_json(app.ORGANIC_FG_PATH, [])
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

    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True, "created": len(created), "lot": lot, "entries": created})


@finished_goods_bp.route("/api/organic/finished-goods/manual-add", methods=["POST"])
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

    recipes = app.load_recipes()
    recipe = recipes.get(recipe_name)
    if not recipe:
        return jsonify({"error": f"Recipe '{recipe_name}' not found"}), 404

    reason = (data.get("reason") or "Other").strip()
    notes = (data.get("notes") or "").strip()
    lot = (data.get("lot") or "").strip() or ("MAN-" + datetime.now().strftime("%d%m%y"))

    fg = _load_json(app.ORGANIC_FG_PATH, [])
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
    _save_json(app.ORGANIC_FG_PATH, fg)

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


@finished_goods_bp.route("/api/organic/finished-goods/manual-subtract", methods=["POST"])
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

    # Match on the full SKU key (brand|recipe|format) when the caller provides
    # the components, so a recipe name shared across formats/brands doesn't drain
    # the wrong SKU. Falls back to recipe-only matching for legacy callers that
    # send just {recipe}.
    # Prefer recomputing the key from components (normalised the same way the FG
    # entries are) over trusting the client's pre-built sku_key string.
    if data.get("brand") or data.get("format"):
        sku_key = _sku_key(data.get("brand", ""), recipe_name, data.get("format", ""))
    else:
        sku_key = (data.get("sku_key") or "").strip()

    fg = _load_json(app.ORGANIC_FG_PATH, [])
    # Find all entries for this SKU, sorted oldest-first for FIFO
    if sku_key:
        matching = [f for f in fg
                    if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
                    and int(f.get("quantity_remaining") or 0) > 0]
    else:
        matching = [f for f in fg
                    if (f.get("recipe") or "") == recipe_name
                    and int(f.get("quantity_remaining") or 0) > 0]
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

    _save_json(app.ORGANIC_FG_PATH, fg)

    _record_adjustment({
        "id": "adj_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(qty),
        "kind": "subtract",
        "recipe": recipe_name,
        "sku_key": sku_key,
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


@finished_goods_bp.route("/api/organic/finished-goods/grouped", methods=["GET"])
@login_required
def get_finished_goods_grouped():
    """GET /api/organic/finished-goods/grouped - FG grouped by SKU, joined to the catalog."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    recipes = app.load_recipes()
    grouped = app._group_fg_with_catalog(fg, recipes)
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        grouped = [g for g in grouped
                   if (g.get("certification") or "").lower() == cert_filter.lower()]
    meta = _load_json(app.SKU_META_PATH, {})
    for g in grouped:
        m = meta.get(g["sku_key"], {})
        g["par"] = m.get("par")          # None = no PAR; int = PAR level
        g["price"] = m.get("price")      # None = unset; float = price per unit
    return jsonify(grouped)


@finished_goods_bp.route("/api/organic/finished-goods/sku/<path:sku_key>", methods=["GET"])
@login_required
def get_finished_goods_sku_detail(sku_key):
    """Return LOT-level FIFO detail for a single SKU.
    Each LOT row aggregates same-LOT entries from multiple kettles."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    lots = _aggregate_lots_for_sku(fg, sku_key)
    if not lots:
        groups = app._group_fg_by_sku(fg)
        if not any(g["sku_key"] == sku_key for g in groups):
            return jsonify({"error": "SKU not found"}), 404
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


# ── Inventory: sku-meta (PAR levels) + inventory page shells ─────────────────

@finished_goods_bp.route("/organic-certification")
@login_required
def organic_certification_page():
    """Hub linking the organic-certification tools (reconcile, audits, docs)."""
    return render_template("organic_certification.html")


@finished_goods_bp.route("/organic")
@login_required
def organic_page():
    """Render the Manage Inventory page (Raw Materials / Runs / Finished Goods / Records tabs)."""
    return render_template("organic.html")


@finished_goods_bp.route("/api/sku-meta/<path:sku_key>", methods=["PATCH"])
@login_required
def update_sku_meta(sku_key):
    """Update PAR level for a SKU.
    Body: { par: int|null }
    null = remove the field (No PAR).
    Price is no longer stored here — it lives in the buyer catalogue.
    """
    data = request.get_json() or {}
    meta = _load_json(app.SKU_META_PATH, {})
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
    if not meta[sku_key]:
        del meta[sku_key]
    _save_json(app.SKU_META_PATH, meta)
    return jsonify({"ok": True, "meta": meta.get(sku_key, {})})


@finished_goods_bp.route("/api/sku-meta", methods=["GET"])
@login_required
def get_all_sku_meta():
    """Return all SKU meta — used by create_schedule page to check PAR warnings."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    recipes = app.load_recipes()
    grouped = app._group_fg_with_catalog(fg, recipes)
    meta = _load_json(app.SKU_META_PATH, {})
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

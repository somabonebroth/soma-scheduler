"""raw_materials.py — Raw-materials inventory blueprint extracted from app.py.

Seventh step of the app.py split (CLAUDE.md "Pending architectural work") and the
second slice of the "inventory" domain. Scope: the 21 raw-materials routes —
ingredients, sections/assignments, RM CRUD (incl. bulk add), the by-ingredient
lot lookup, invoices (upload/list/serve/delete), and receipt photos
(upload/list/get/delete).

Pattern (matches buyers/recipes/sales/finished_goods): PURE routes-move — every
helper and constant stays in app.py and is reached via `import app` +
app.-qualification. This includes the three RM-private invoice helpers
(_parse_lots_field, _remove_invoice_file, _save_invoice_file_bytes) and
_invoice_mime: the invoice-helper cluster is internally self-referential, so it
stays together in app.py rather than being relocated (safer, and consistent with
every prior slice). Foundation names (_load_json, _save_json, _add_contact,
_load_rm_sections, _section_for_ingredient, _runs_using_raw_material,
RM_SECTIONS_PATH) import directly from helpers.

IMPORTANT (audit chain): these routes touch NO raw-material *consumption* code —
no _deduct_run_ingredients / _complete_organic_run / _rebuild_raw_material_
consumption / _eligible_lots_for_date. The only audit-protective hook here is
delete_raw_material's 409 guard, which uses _runs_using_raw_material (already in
helpers.py). The consumption-chain and reconcile/trace tools are untouched.

Defines its own login_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
import os
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint, request, jsonify, session, redirect, url_for,
    send_file, send_from_directory,
)

from helpers import (
    RM_SECTIONS_PATH,
    _load_json,
    _save_json,
    _add_contact,
    _load_rm_sections,
    _section_for_ingredient,
    _runs_using_raw_material,
)

import app

raw_materials_bp = Blueprint("raw_materials", __name__)


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


@raw_materials_bp.route("/api/organic/ingredients", methods=["GET"])
@login_required
def organic_ingredients():
    """Return unique (name, unit) pairs pulled from organic-certified recipes.

    Each entry: {name, unit, recipe_amount}
    - recipe_amount is informational (first-seen amount for this name+unit)
    - Skips needs_review items.
    - Custom user-added ingredients merged in.
    """
    recipes = app.load_recipes()
    derived = {}  # key: (name_lower, unit) -> entry

    for rname, rdata in recipes.items():
        # Universal inventory: pull ingredients from every recipe regardless of
        # certification status. Each recipe contributes its tracked ingredients.
        if rdata.get("archived"):
            continue
        for section in app.INGREDIENT_SECTIONS:
            for item in rdata.get(section, []):
                if not app.is_structured_ingredient(item):
                    continue
                if item.get("needs_review"):
                    continue
                ing_name = (item.get("name") or "").strip()
                unit = (item.get("unit") or "").strip()
                # Untracked ingredients (e.g. water) are unlimited — they
                # appear on recipe cards but are never tracked as raw materials.
                if app.is_untracked_ingredient(ing_name):
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
        all_items = [dict(i, recipe_amount=0) for i in app.ORGANIC_INGREDIENTS_FALLBACK]

    # Merge custom user-added items (never duplicate by (name, unit))
    custom = _load_json(app.ORGANIC_CUSTOM_ITEMS_PATH, [])
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


@raw_materials_bp.route("/api/organic/raw-materials/sections", methods=["GET"])
@login_required
def get_rm_sections():
    """Return the section list + per-ingredient assignments."""
    return jsonify(_load_rm_sections())


@raw_materials_bp.route("/api/organic/raw-materials/sections", methods=["PUT"])
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
        if sid == app.UNASSIGNED_SECTION_ID:
            return jsonify({"error": f"section id '{app.UNASSIGNED_SECTION_ID}' is reserved"}), 400
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


@raw_materials_bp.route("/api/organic/raw-materials/assignments", methods=["PUT"])
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


@raw_materials_bp.route("/api/organic/raw-materials/grouped", methods=["GET"])
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
    recipes = app.load_recipes()
    derived = {}  # key: (name_lower, unit) -> {name, unit}
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        for section in app.INGREDIENT_SECTIONS:
            for item in rdata.get(section, []):
                if not app.is_structured_ingredient(item):
                    continue
                if item.get("needs_review"):
                    continue
                ing_name = (item.get("name") or "").strip()
                unit = (item.get("unit") or "").strip()
                if app.is_untracked_ingredient(ing_name):
                    continue
                if not ing_name or not unit:
                    continue
                key = (ing_name.lower(), unit)
                if key not in derived:
                    derived[key] = {"name": ing_name, "unit": unit}

    custom = _load_json(app.ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        name = c.get("name", "")
        unit = c.get("unit", "")
        if not name or not unit or app.is_untracked_ingredient(name):
            continue
        key = (name.lower(), unit)
        if key not in derived:
            derived[key] = {"name": name, "unit": unit}

    # Now aggregate the actual raw materials inventory by (name_lower, unit)
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
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

    sections_data = _load_rm_sections()
    sections_list = sections_data.get("sections", [])
    section_order = {s["id"]: i for i, s in enumerate(sections_list)}
    section_names = {s["id"]: s["name"] for s in sections_list}

    rows = []
    for key, ing in derived.items():
        agg = aggregates.get(key, {})
        total_received = round(agg.get("total_received", 0.0), 3)
        total_remaining = round(agg.get("total_remaining", 0.0), 3)
        # Trim trailing zeros — show 50 not 50.000, but keep 2.5 as 2.5
        if total_received == int(total_received):
            total_received = int(total_received)
        if total_remaining == int(total_remaining):
            total_remaining = int(total_remaining)

        sec_id = _section_for_ingredient(ing["name"], ing["unit"], sections_data)
        if sec_id:
            sec_name = section_names.get(sec_id, app.UNASSIGNED_SECTION_NAME)
            sort_idx = section_order.get(sec_id, 999)
        else:
            sec_id = app.UNASSIGNED_SECTION_ID
            sec_name = app.UNASSIGNED_SECTION_NAME
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
    for r in rows:
        r.pop("_sort_idx", None)
    return jsonify(rows)


@raw_materials_bp.route("/api/organic/raw-materials/by-ingredient/<path:item>/<unit>", methods=["GET"])
@login_required
def get_raw_material_lots(item, unit):
    """Return all LOT entries for a specific ingredient name + unit.
    Sorted oldest-first (FIFO order). Used when expanding a row in the
    inventory list to see LOT-level detail."""
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
    item_lc = item.lower()
    matching = [m for m in materials
                if (m.get("item") or "").strip().lower() == item_lc
                and (m.get("unit") or "").strip() == unit]
    matching.sort(key=lambda m: (m.get("date_received") or "", m.get("created_at") or ""))
    return jsonify(matching)


@raw_materials_bp.route("/api/organic/ingredients", methods=["POST"])
@login_required
def add_organic_ingredient():
    """Add a custom ingredient. Body: {name, unit}."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    unit = (data.get("unit") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    if unit not in app.VALID_UNITS:
        return jsonify({"error": f"Unit must be one of: {', '.join(app.VALID_UNITS)}"}), 400
    custom = _load_json(app.ORGANIC_CUSTOM_ITEMS_PATH, [])
    existing = {(c.get("name", "").lower(), c.get("unit", "")) for c in custom}
    key = (name.lower(), unit)
    if key in existing:
        return jsonify({"error": "Already exists"}), 400
    custom.append({"name": name, "unit": unit})
    _save_json(app.ORGANIC_CUSTOM_ITEMS_PATH, custom)
    return jsonify({"success": True})


@raw_materials_bp.route("/api/organic/raw-materials", methods=["GET"])
@login_required
def get_raw_materials():
    """GET /api/organic/raw-materials - return all raw-material lots."""
    return jsonify(_load_json(app.ORGANIC_RAW_PATH, []))


@raw_materials_bp.route("/api/organic/raw-materials", methods=["POST"])
@login_required
def add_raw_material():
    """Add a raw material lot. JSON body:
      {item, unit, quantity, supplier, date_received, supplier_lot, baseline (optional)}

    If baseline is true, the supplier_lot is auto-prefixed with 'BL-' and date,
    and migration_baseline=true is set on the entry. These represent physical
    counts at the moment of inventory migration, not real production receipts.
    """
    data = request.json or {}
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
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
    # Never store a blank lot — a receipt with no lot# is untraceable. Stamp a
    # system lot ('MAN-DDMMYY', or 'BL-' for a baseline count) and flag it so the
    # real supplier lot# can be chased and filled in later.
    no_supplier_lot = False
    if not supplier_lot:
        supplier_lot = ("BL-" if is_baseline else "MAN-") + datetime.now().strftime("%d%m%y")
        no_supplier_lot = not is_baseline

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
    if no_supplier_lot:
        entry["no_supplier_lot"] = True
    if is_baseline:
        entry["migration_baseline"] = True
    materials.append(entry)
    _save_json(app.ORGANIC_RAW_PATH, materials)
    supplier = data.get("supplier", "").strip()
    if supplier:
        _add_contact("supplier", supplier)
    return jsonify({"success": True, "entry": entry})


@raw_materials_bp.route("/api/organic/raw-materials/bulk", methods=["POST"])
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

    # This is the same set the picker populates from — recipes + custom items.
    canonical = {}
    recipes = app.load_recipes()
    for rname, rdata in recipes.items():
        if rdata.get("archived"):
            continue
        for section in app.INGREDIENT_SECTIONS:
            for it in rdata.get(section, []):
                if not app.is_structured_ingredient(it):
                    continue
                if it.get("needs_review"):
                    continue
                ing_name = (it.get("name") or "").strip()
                unit = (it.get("unit") or "").strip()
                if app.is_untracked_ingredient(ing_name):
                    continue
                if not ing_name or not unit:
                    continue
                key = (ing_name.lower(), unit)
                if key not in canonical:
                    canonical[key] = ing_name
    custom = _load_json(app.ORGANIC_CUSTOM_ITEMS_PATH, [])
    for c in custom:
        nm = (c.get("name") or "").strip()
        un = (c.get("unit") or "").strip()
        if not nm or not un or app.is_untracked_ingredient(nm):
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

    materials = _load_json(app.ORGANIC_RAW_PATH, [])
    today_str = datetime.now().strftime("%Y-%m-%d")
    base_ts = datetime.now()
    shared_baseline_lot = "BL-" + base_ts.strftime("%d%m%y") if is_baseline else None
    suppliers_seen = set()
    created = []

    for i, item in enumerate(to_create):
        if is_baseline:
            supplier = item["supplier"] or "(physical count)"
            supplier_lot = shared_baseline_lot
            date_received = item["date_received"] or today_str
        else:
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
        elif not item["supplier_lot"]:
            # Auto-generated MAN- lot (no real supplier lot# provided) — flag for follow-up.
            entry["no_supplier_lot"] = True
        if item["notes"]:
            entry["notes"] = item["notes"]
        materials.append(entry)
        created.append(entry)
        if supplier and supplier != "(physical count)":
            suppliers_seen.add(supplier)

    _save_json(app.ORGANIC_RAW_PATH, materials)
    # Record any new suppliers in the contacts file (skips duplicates internally)
    for s in suppliers_seen:
        _add_contact("supplier", s)

    return jsonify({
        "success": True,
        "created": len(created),
        "lot": shared_baseline_lot,  # only set when baseline=true
        "ids": [e["id"] for e in created],  # receipt-photo anchor (first id)
        "entries": created,
    })


@raw_materials_bp.route("/api/organic/raw-materials/<entry_id>", methods=["PUT"])
@login_required
def update_raw_material(entry_id):
    """Manually adjust the remaining quantity of a raw material entry.
    Body: {remaining}. Use for stock counts / corrections.
    Other fields (item, unit, original quantity) are NOT editable here —
    they would invalidate prior production-run deductions if changed.
    """
    data = request.json or {}
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
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
    _save_json(app.ORGANIC_RAW_PATH, materials)
    return jsonify({"success": True, "entry": entry})


@raw_materials_bp.route("/api/organic/raw-materials/<entry_id>/usage", methods=["GET"])
@login_required
def get_raw_material_usage(entry_id):
    """Return list of completed runs that deducted from this entry. Frontend
    uses this to warn the user before deletion."""
    return jsonify({"used_in": _runs_using_raw_material(entry_id)})


@raw_materials_bp.route("/api/organic/raw-materials/<entry_id>", methods=["DELETE"])
@login_required
def delete_raw_material(entry_id):
    """Delete a raw-material lot — but never one that a completed production run
    consumed. A completed batch points back to its lots by id; hard-deleting a
    consumed lot dangles that link and breaks one-step-back traceability. If the
    lot was used, the caller must reverse the affected run(s) first."""
    used_in = _runs_using_raw_material(entry_id)
    if used_in:
        return jsonify({
            "error": (f"Cannot delete: this lot was used in {len(used_in)} completed "
                      f"production run(s). Reverse that production first, then delete."),
            "used_in": used_in,
        }), 409
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
    if not any(m.get("id") == entry_id for m in materials):
        return jsonify({"error": "Entry not found"}), 404
    materials = [m for m in materials if m.get("id") != entry_id]
    _save_json(app.ORGANIC_RAW_PATH, materials)
    return jsonify({"success": True})


@raw_materials_bp.route("/api/organic/invoices", methods=["POST"])
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
    lots = app._parse_lots_field(request.form)

    if not supplier:
        return jsonify({"error": "Supplier required"}), 400
    if not invoice_date:
        return jsonify({"error": "Invoice date required"}), 400

    inv_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    try:
        filename, meta = app._save_invoice_file_bytes(inv_id, f)
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
    invoices = _load_json(app.INVOICES_INDEX_PATH, [])
    invoices.append(record)
    _save_json(app.INVOICES_INDEX_PATH, invoices)
    if supplier:
        _add_contact("supplier", supplier)
    return jsonify({"success": True, "invoice": record})


@raw_materials_bp.route("/api/organic/invoices", methods=["GET"])
@login_required
def list_invoices():
    """Return all invoices, newest invoice_date first (ties broken by uploaded_at)."""
    lot_filter = (request.args.get("lot") or "").strip()
    invoices = _load_json(app.INVOICES_INDEX_PATH, [])
    if lot_filter:
        lf = lot_filter.lower()
        invoices = [inv for inv in invoices
                    if any(lf in (l or "").lower() for l in (inv.get("lots") or []))]
    invoices.sort(key=lambda r: (r.get("invoice_date", ""), r.get("uploaded_at", "")),
                  reverse=True)
    return jsonify(invoices)


@raw_materials_bp.route("/api/organic/invoices/<inv_id>/file", methods=["GET"])
@login_required
def serve_invoice(inv_id):
    """Serve the invoice file inline (not forced download)."""
    invoices = _load_json(app.INVOICES_INDEX_PATH, [])
    rec = next((i for i in invoices if i.get("id") == inv_id), None)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    fname = rec.get("filename", "")
    safe = os.path.basename(fname)
    if safe != fname:
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(app.INVOICES_DIR, safe)
    if not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404
    return send_file(path, mimetype=app._invoice_mime(safe),
                     as_attachment=False, download_name=safe)


@raw_materials_bp.route("/api/organic/invoices/<inv_id>", methods=["DELETE"])
@login_required
def delete_invoice(inv_id):
    """DELETE /api/organic/invoices/<inv_id> - remove an invoice record and its stored file."""
    invoices = _load_json(app.INVOICES_INDEX_PATH, [])
    rec = next((i for i in invoices if i.get("id") == inv_id), None)
    if not rec:
        return jsonify({"error": "Not found"}), 404
    app._remove_invoice_file(rec.get("filename", ""))
    invoices = [i for i in invoices if i.get("id") != inv_id]
    _save_json(app.INVOICES_INDEX_PATH, invoices)
    return jsonify({"success": True})


@raw_materials_bp.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["POST"])
@login_required
def upload_rm_receipt_photo(entry_id):
    """POST a receipt photo for a raw-material delivery (anchored to the entry id)."""
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "No file"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in app._RM_PHOTO_ALLOWED:
        return jsonify({"error": f"File type not supported"}), 400
    data = file.read()
    if len(data) > app._RM_PHOTO_MAX_BYTES:
        return jsonify({"error": "File too large (max 20 MB)"}), 413
    for fn in os.listdir(app.RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            os.remove(os.path.join(app.RM_RECEIPT_PHOTOS_DIR, fn))
    filename = f"{entry_id}.{ext}"
    with open(os.path.join(app.RM_RECEIPT_PHOTOS_DIR, filename), "wb") as fh:
        fh.write(data)
    return jsonify({"ok": True, "filename": filename})


@raw_materials_bp.route("/api/organic/raw-materials/receipt-photos", methods=["GET"])
@login_required
def list_rm_receipt_photos():
    """Return the set of raw-material entry ids that have a stored receipt photo.
    The Receiving list uses this to show an invoice link on the right
    transaction (the photo is anchored to the first entry id of a delivery)."""
    ids = []
    try:
        for fn in os.listdir(app.RM_RECEIPT_PHOTOS_DIR):
            stem = fn.rsplit(".", 1)[0]
            if stem:
                ids.append(stem)
    except FileNotFoundError:
        pass
    return jsonify({"ids": ids})


@raw_materials_bp.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["GET"])
@login_required
def get_rm_receipt_photo(entry_id):
    """GET the stored receipt photo for a raw-material entry."""
    for fn in os.listdir(app.RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            return send_from_directory(app.RM_RECEIPT_PHOTOS_DIR, fn)
    return jsonify({"error": "Not found"}), 404


@raw_materials_bp.route("/api/organic/raw-materials/receipt-photo/<entry_id>", methods=["DELETE"])
@login_required
def delete_rm_receipt_photo(entry_id):
    """DELETE the stored receipt photo for a raw-material entry."""
    for fn in os.listdir(app.RM_RECEIPT_PHOTOS_DIR):
        if fn.startswith(entry_id + "."):
            os.remove(os.path.join(app.RM_RECEIPT_PHOTOS_DIR, fn))
            return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404

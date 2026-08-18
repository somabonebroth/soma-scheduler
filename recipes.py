"""recipes.py — Recipe blueprint extracted from app.py.

Fourth step of the app.py split (CLAUDE.md "Pending architectural work") and the
highest-risk to date: it includes update_recipe's 151-line cross-domain rename
cascade (buyers / finished goods / sales / schedules / sku_meta).

Pattern: routes move, shared helpers STAY. The recipe data helpers
(load_recipes, save_recipes, load_recipe_order, save_recipe_order,
migrate_recipe_ingredients, parse_recipe_pdf_text, _load_tracking_modes) and the
buyer helpers (_load_buyers, _save_buyers) remain in app.py because non-recipe
code there also calls them; likewise the path constants (ORGANIC_FG_PATH,
ORGANIC_SALES_PATH, SCHEDULES_DIR, SKU_META_PATH, RECIPES_PATH, PHOTOS_DIR) and
INGREDIENT_SECTIONS. This blueprint reaches all of them at request time via
`import app` + app.-qualification (the standard blueprint<->app circular pattern:
bare `import app` binds the partially-initialised module at load, attributes
resolve only when a request runs).

_schedules_using_recipe is recipe-private (only archive_recipe calls it) and so
moves here in full; its own deps are app.-qualified like the rest.

Defines its own login_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
import os
import re
import io
import json
from datetime import datetime
from functools import wraps
import logging

from flask import (
    Blueprint, request, jsonify, session, redirect, url_for,
    render_template, send_file,
)

from pdf_engine import generate_single_recipe_pdf, generate_all_recipes_pdf

# Foundation layer (dependency-free) — imported directly, same as suppliers.py.
# These live in helpers.py, NOT app.py, so they are imported here rather than
# app.-qualified.
from helpers import (
    DATA_DIR,
    ORGANIC_RUNS_PATH,
    _load_json,
    _save_json,
    _normalize_format,
    _sku_key,
    _sku_display,
    build_display_name,
)

import app

logger = logging.getLogger(__name__)

recipes_bp = Blueprint("recipes", __name__)


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


def manager_required(f):
    """Local copy of app.py's manager_required (verbatim) — recipe WRITE routes
    are manager-only (2026-08-18 two-role split): the production tablet gets a
    read-only recipe view, and the rename cascade in update_recipe rewrites
    FG/sales/runs/schedules, so hiding buttons is not enough. Sessions from
    before roles existed count as manager (see app.current_role)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        if (session.get("role") or "manager") != "manager":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Manager access required"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


@recipes_bp.route("/recipes")
@login_required
def recipes_page():
    """Render the recipes page."""
    return render_template("recipes.html")


@recipes_bp.route("/api/recipes", methods=["GET"])
@login_required
def get_recipes():
    """GET /api/recipes - return all recipes, with 'per L' ingredients
    annotated with their inferred g/ml dosing unit for display."""
    recipes = app.load_recipes()
    raw_materials = app._load_json(app.ORGANIC_RAW_PATH, [])
    for recipe in recipes.values():
        app._attach_per_l_units(recipe, raw_materials)
    return jsonify(recipes)


@recipes_bp.route("/api/recipes", methods=["POST"])
@manager_required
def add_recipe():
    """POST /api/recipes - create a recipe from the JSON body."""
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    recipes = app.load_recipes()
    recipes[name] = data.get("data", {})
    app.save_recipes(recipes)
    return jsonify({"success": True})


@recipes_bp.route("/api/recipes/<path:name>", methods=["GET"])
@login_required
def get_recipe(name):
    """GET /api/recipes/<name> - return a single recipe by name."""
    recipes = app.load_recipes()
    if name in recipes:
        return jsonify({"name": name, "data": recipes[name]})
    return jsonify({"error": "Recipe not found"}), 404


@recipes_bp.route("/api/recipe-pdf/<path:name>", methods=["GET"])
@login_required
def recipe_pdf(name):
    """Single-recipe card PDF, for the per-row Download button."""
    recipes = app.load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    logo_path = os.path.join(app.app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None
    buf = io.BytesIO()
    generate_single_recipe_pdf(buf, name, recipes[name], logo_path)
    buf.seek(0)
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name="Recipe_" + safe + ".pdf")


@recipes_bp.route("/api/recipe-pdf-all", methods=["GET"])
@login_required
def recipe_pdf_all():
    """All active (non-archived) recipes as one PDF, ordered by the saved
    brand+recipe order so the file matches what's on screen."""
    recipes = app.load_recipes()
    order = app.load_recipe_order() or {}
    brand_order  = order.get("brand_order")  or []
    recipe_order = order.get("recipe_order") or {}
    ordered, seen = [], set()
    for brand in brand_order:
        for name in (recipe_order.get(brand) or []):
            r = recipes.get(name)
            if r and not r.get("archived"):
                ordered.append((name, r))
                seen.add(name)
    for name, r in recipes.items():
        if name not in seen and not r.get("archived"):
            ordered.append((name, r))
    if not ordered:
        return jsonify({"error": "No active recipes to export"}), 400
    logo_path = os.path.join(app.app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None
    buf = io.BytesIO()
    generate_all_recipes_pdf(buf, ordered, logo_path)
    buf.seek(0)
    today = datetime.now().strftime("%Y-%m-%d")
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name="All_Recipes_" + today + ".pdf")


@recipes_bp.route("/api/recipes/<path:name>", methods=["PUT"])
@manager_required
def update_recipe(name):
    """Update an existing recipe. If the body's 'name' differs from the URL
    name, this is treated as a rename.

    Cascades any name/brand/format change to all downstream records:
    FG inventory, sales, production runs, schedules, sku_meta, buyers,
    and notifies the Ripe portal to update soma_sku_key.
    """
    data = request.json or {}
    recipes = app.load_recipes()
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

    if new_name != name:
        del recipes[name]
    recipes[new_name] = recipe_data
    app.save_recipes(recipes)

    # ── Cascade if anything identity-related changed ─────────────────────────
    identity_changed = (new_name != name or new_brand != old_brand or new_sku_key != old_sku_key)
    cascade = {}

    if identity_changed:
        # 1. Finished goods
        fg = _load_json(app.ORGANIC_FG_PATH, [])
        n = 0
        for e in fg:
            if (e.get("recipe") or "").strip() == name:
                e["recipe"] = new_name
                if new_brand: e["brand"] = new_brand
                if new_format: e["format"] = new_format
                n += 1
        if n: _save_json(app.ORGANIC_FG_PATH, fg)
        cascade["finished_goods"] = n

        # 2. Sales records
        sales = _load_json(app.ORGANIC_SALES_PATH, [])
        n = 0
        for s in sales:
            if (s.get("recipe") or "").strip() == name:
                s["recipe"] = new_name
                if new_brand: s["brand"] = new_brand
                if new_format: s["format"] = new_format
                s["sku_key"] = _sku_key(s.get("brand", new_brand), new_name, s.get("format", new_format))
                n += 1
        if n: _save_json(app.ORGANIC_SALES_PATH, sales)
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
            meta = _load_json(app.SKU_META_PATH, {})
            if old_sku_key in meta:
                meta[new_sku_key] = meta.pop(old_sku_key)
                _save_json(app.SKU_META_PATH, meta)
                cascade["sku_meta"] = 1

        # 5. Buyers — update sku_key/recipe/display on assigned SKUs
        buyers = app._load_buyers()
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
        if n: app._save_buyers(buyers)
        cascade["buyers_skus"] = n

        # 6. Schedule files — recipe names as slot values
        n = 0
        try:
            if os.path.isdir(app.SCHEDULES_DIR):
                for fname in os.listdir(app.SCHEDULES_DIR):
                    if not fname.endswith(".json"): continue
                    fpath = os.path.join(app.SCHEDULES_DIR, fname)
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


@recipes_bp.route("/api/recipes/<path:name>", methods=["DELETE"])
@manager_required
def delete_recipe(name):
    """DELETE /api/recipes/<name> - remove a recipe."""
    recipes = app.load_recipes()
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
    app.save_recipes(recipes)
    return jsonify({"success": True})


def _schedules_using_recipe(recipe_name):
    """Return list of (week_id, day_idx, vessel) tuples where recipe_name is scheduled."""
    refs = []
    if not os.path.exists(app.SCHEDULES_DIR):
        return refs
    for fn in os.listdir(app.SCHEDULES_DIR):
        if not fn.endswith(".json"):
            continue
        week_id = fn[:-5]
        try:
            with open(os.path.join(app.SCHEDULES_DIR, fn)) as f:
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


@recipes_bp.route("/api/recipes/<path:name>/duplicate", methods=["POST"])
@manager_required
def duplicate_recipe(name):
    """Duplicate a recipe with a new name. Body: {new_name}.
    Copies all data including ingredients/yield/brand/format. If the source
    has a photo, the photo file is copied to a new filename keyed to the
    duplicate's name."""
    data = request.json or {}
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"error": "new_name required"}), 400

    recipes = app.load_recipes()
    if name not in recipes:
        return jsonify({"error": "Source recipe not found"}), 404
    if new_name in recipes:
        return jsonify({"error": "A recipe with that name already exists"}), 400

    # Deep copy via JSON round-trip
    new_data = json.loads(json.dumps(recipes[name]))

    # Copy photo file if present
    src_photo = new_data.get("photo")
    if src_photo:
        src_path = os.path.join(app.PHOTOS_DIR, src_photo)
        if os.path.exists(src_path):
            ext = os.path.splitext(src_photo)[1].lower() or ".jpg"
            safe = re.sub(r'[^a-zA-Z0-9_-]', '_', new_name)
            new_photo = safe + ext
            new_path = os.path.join(app.PHOTOS_DIR, new_photo)
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
    app.save_recipes(recipes)
    return jsonify({"success": True, "name": new_name, "data": new_data})


@recipes_bp.route("/api/recipes/<path:name>/archive", methods=["POST"])
@manager_required
def archive_recipe(name):
    """Mark a recipe as archived. It stays in storage so old schedules and
    tracker entries still resolve, but it's hidden from the new-schedule
    recipe picker. Returns 200 with a list of schedule references so the
    frontend can warn the user about active uses."""
    recipes = app.load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    recipes[name]["archived"] = True
    app.save_recipes(recipes)
    refs = _schedules_using_recipe(name)
    return jsonify({"success": True, "schedule_refs": refs})


@recipes_bp.route("/api/recipes/<path:name>/unarchive", methods=["POST"])
@manager_required
def unarchive_recipe(name):
    """Restore an archived recipe to active."""
    recipes = app.load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    recipes[name]["archived"] = False
    app.save_recipes(recipes)
    return jsonify({"success": True})


@recipes_bp.route("/api/recipes/migrate-all", methods=["POST"])
@manager_required
def migrate_all_recipes():
    """Force-persist structured-ingredient migration to disk for all recipes.
    Performs smart pack conversion using organic/tracking_modes.json, then
    archives that file (renamed to .archived) so future runs don't reapply it.
    Returns a report of which recipes were changed and which items need review."""
    # Read raw file (bypass auto-migration in app.load_recipes so we can count changes)
    raw_recipes = {}
    if os.path.exists(app.RECIPES_PATH):
        with open(app.RECIPES_PATH, "r") as f:
            raw_recipes = json.load(f)

    tracking_modes = app._load_tracking_modes()

    changed_recipes = []
    review_items = []  # list of {recipe, section, index, name, reason}

    for name, data in raw_recipes.items():
        changed = app.migrate_recipe_ingredients(data, tracking_modes)
        if changed:
            changed_recipes.append(name)
        for section in app.INGREDIENT_SECTIONS:
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

    app.save_recipes(raw_recipes)

    # Archive tracking_modes.json so it isn't reapplied on subsequent migrations.
    # File location: see app._load_tracking_modes; supports both new and legacy paths.
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


@recipes_bp.route("/api/recipes/<path:name>/photo", methods=["POST"])
@manager_required
def upload_recipe_photo(name):
    """POST a photo for a recipe; stored under PHOTOS_DIR."""
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400
    file = request.files["photo"]
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".jpg", ".jpeg", ".png"]:
        ext = ".jpg"
    filename = safe + ext
    filepath = os.path.join(app.PHOTOS_DIR, filename)
    file.save(filepath)
    recipes = app.load_recipes()
    if name in recipes:
        recipes[name]["photo"] = filename
        app.save_recipes(recipes)
    return jsonify({"success": True, "photo": filename})


@recipes_bp.route("/api/recipes/grouped", methods=["GET"])
@login_required
def get_recipes_grouped():
    """Return recipes grouped by brand for the schedule picker.
    Excludes archived recipes by default. Pass ?include_archived=1 to include them."""
    include_archived = request.args.get("include_archived", "0") in ("1", "true", "yes")
    recipes = app.load_recipes()
    order = app.load_recipe_order()
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


@recipes_bp.route("/api/recipes/order", methods=["POST"])
@manager_required
def update_recipe_order():
    """POST /api/recipes/order - persist the recipe display order."""
    data = request.json or {}
    app.save_recipe_order(data)
    return jsonify({"success": True})


@recipes_bp.route("/api/recipes/upload", methods=["POST"])
@manager_required
def upload_recipe():
    """POST /api/recipes/upload - create a recipe from an uploaded PDF or JSON text."""
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
        parsed = app.parse_recipe_pdf_text(text)
        if not parsed:
            return jsonify({"error": "Could not parse recipe from PDF"}), 400
        recipes = app.load_recipes()
        recipes[parsed["name"]] = parsed["data"]
        app.save_recipes(recipes)
        return jsonify({"success": True, "name": parsed["name"], "data": parsed["data"]})

    # Handle JSON text upload (legacy)
    data = request.json or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "No text provided"}), 400
    parsed = app.parse_recipe_pdf_text(text)
    if not parsed:
        return jsonify({"error": "Could not parse recipe"}), 400
    recipes = app.load_recipes()
    recipes[parsed["name"]] = parsed["data"]
    app.save_recipes(recipes)
    return jsonify({"success": True, "name": parsed["name"], "data": parsed["data"]})


@recipes_bp.route("/api/recipes/upload-json", methods=["POST"])
@manager_required
def upload_recipe_json():
    """POST /api/recipes/upload-json - create a recipe from a JSON body (manual add)."""
    data = request.json or {}
    if not data:
        return jsonify({"error": "No data provided"}), 400
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    recipe_data = data.get("data", {})
    recipes = app.load_recipes()
    recipes[name] = recipe_data
    app.save_recipes(recipes)
    return jsonify({"success": True, "name": name})

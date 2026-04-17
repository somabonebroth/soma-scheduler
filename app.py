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
def load_recipes():
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            return json.load(f)
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


# ── Recipe parser ──────────────────────────────────────────────────────
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
            fmt = line.split(":", 1)[1].strip().upper()
            if "SS-876ML" in fmt or "SS876ML" in fmt:
                recipe["format"] = "SS-876ML"
            elif "FZ-750ML" in fmt or "FZ750ML" in fmt:
                recipe["format"] = "FZ-750ML"
            elif "SS-750ML" in fmt or "SS750ML" in fmt:
                recipe["format"] = "SS-750ML"
            elif "IQ-750ML" in fmt or "IQ750ML" in fmt:
                recipe["format"] = "iQ-750ML"
            else:
                recipe["format"] = fmt
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
        upper = name.upper()
        if "SS-876ML" in upper or "SS876ML" in upper:
            recipe["format"] = "SS-876ML"
        elif "FZ-750ML" in upper or "FZ750ML" in upper:
            recipe["format"] = "FZ-750ML"
        elif "SS-750ML" in upper or "SS750ML" in upper:
            recipe["format"] = "SS-750ML"

    if recipe["yield"] is None:
        recipe["yield"] = 190 if "FZ" in recipe["format"] else 150

    # Append format to name for unique storage key
    if recipe["format"] and not name.endswith(recipe["format"]):
        name = name + " " + recipe["format"]

    # Parse recipe body
    current_section = None
    in_special = False
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
            recipe["finishing"].append(line)
            continue
        if "add to jar" in ll or "add to container" in ll:
            current_section = "add_to_jar"
            continue
        if any(ll.startswith(p) for p in ["no salt", "g per liter", "ml per liter"]) or "per liter" in ll or "per litre" in ll:
            if current_section != "finishing":
                current_section = "finishing"
            recipe["finishing"].append(line)
            continue
        if in_special:
            recipe["special_instructions"].append(line)
            continue
        if current_section and current_section in recipe:
            recipe[current_section].append(line)

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
    data = request.json
    recipes = load_recipes()
    new_name = data.get("name", name)
    recipe_data = data.get("data", {})
    if name in recipes and new_name != name:
        del recipes[name]
    recipes[new_name] = recipe_data
    save_recipes(recipes)
    return jsonify({"success": True})

@app.route("/api/recipes/<path:name>", methods=["DELETE"])
@login_required
def delete_recipe(name):
    recipes = load_recipes()
    if name in recipes:
        del recipes[name]
        save_recipes(recipes)
        return jsonify({"success": True})
    return jsonify({"error": "Recipe not found"}), 404

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
    recipes = load_recipes()
    order = load_recipe_order()
    groups = {}
    for name, data in recipes.items():
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
    Items containing 'per liter', 'per litre', 'per/L', 'g/L', 'ml/L' are NOT halved.
    """
    import copy
    halved = copy.deepcopy(recipe_data)

    # Halve yield
    if halved.get("yield"):
        try:
            halved["yield"] = round(int(halved["yield"]) / 2)
        except (ValueError, TypeError):
            pass

    # Halve ingredient quantities in all sections
    per_l_patterns = ["per liter", "per litre", "per/l", "g/l", "ml/l", "/l "]
    for section in ["kettle_overnight", "after_skim", "finishing", "add_to_jar"]:
        if section in halved and isinstance(halved[section], list):
            new_items = []
            for item in halved[section]:
                # Check if this is a per/L item — skip halving
                if any(p in item.lower() for p in per_l_patterns):
                    new_items.append(item)
                    continue
                # Try to find and halve the leading number
                m = re.match(r'^(\d+\.?\d*)\s*(.*)', item)
                if m:
                    val = float(m.group(1)) / 2
                    # Show as int if whole number
                    if val == int(val):
                        val = int(val)
                    new_items.append(f"{val} {m.group(2)}")
                else:
                    new_items.append(item)
            halved[section] = new_items

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

    return jsonify({
        "date": date.strftime("%A, %d/%m/%Y"),
        "day_name": DAYS[day_idx],
        "prev_date": prev_date.strftime("%d/%m/%Y"),
        "prev_lot": prev_date.strftime("%d%m%y"),
        "lot": date.strftime("%d%m%y"),
        "today_lot": date.strftime("%d%m%y"),
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
    return jsonify({"success": True})


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

    # Build display name: strip format from recipe_name if already present, then show as base-format
    base_name = recipe_name
    if recipe_format:
        # Remove format suffix regardless of separator (space, dash, or space-dash)
        for suffix in [" " + recipe_format, "-" + recipe_format, " -" + recipe_format]:
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break
        recipe_format_display = base_name + "-" + recipe_format
    else:
        recipe_format_display = recipe_name

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
    try:
        _check_organic_completion(week_id, day_idx, data)
    except Exception:
        pass

    return jsonify({"success": True, "filename": filename})


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
@app.route("/api/traceability", methods=["GET"])
@login_required
def get_traceability():
    weeks = list_schedules()
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


# ── Production Tracker ────────────────────────────────────────────────
def _week_totals(week_id):
    """Calculate totals for a single week."""
    totals = {"produced": 0, "bb": 0, "kettles_end": 0}
    for d_idx in range(7):
        cl = load_checklist(week_id, d_idx)
        if cl:
            if cl.get("produced"):
                for vessel_id, amount in cl["produced"].items():
                    try:
                        totals["produced"] += int(amount)
                    except (ValueError, TypeError):
                        pass
            if cl.get("bb_produced"):
                for vessel_id, amount in cl["bb_produced"].items():
                    try:
                        totals["bb"] += int(amount)
                    except (ValueError, TypeError):
                        pass
            try:
                totals["kettles_end"] += int(cl.get("kettles_end", 0))
            except (ValueError, TypeError):
                pass
    totals["total"] = totals["produced"] + totals["bb"] + totals["kettles_end"]
    return totals


@app.route("/api/production-tracker/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def get_production_tracker(week_id):
    daily_totals = []
    for d_idx in range(7):
        cl = load_checklist(week_id, d_idx)
        total_produced = 0
        total_bb = 0
        total_kettles_end = 0
        if cl:
            if cl.get("produced"):
                for vessel_id, amount in cl["produced"].items():
                    try:
                        total_produced += int(amount)
                    except (ValueError, TypeError):
                        pass
            if cl.get("bb_produced"):
                for vessel_id, amount in cl["bb_produced"].items():
                    try:
                        total_bb += int(amount)
                    except (ValueError, TypeError):
                        pass
            try:
                total_kettles_end = int(cl.get("kettles_end", 0))
            except (ValueError, TypeError):
                total_kettles_end = 0
        daily_totals.append({
            "day_idx": d_idx,
            "day_name": DAYS[d_idx],
            "produced": total_produced,
            "bb": total_bb,
            "kettles_end": total_kettles_end,
            "total": total_produced + total_bb + total_kettles_end,
            "has_data": cl is not None and _has_meaningful_data(cl) if cl else False,
        })
    return jsonify(daily_totals)


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
            totals["week_id"] = wid
            totals["label"] = current.strftime("%b %d") + " - " + end_date.strftime("%b %d")
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

        month_total = {"produced": 0, "bb": 0, "kettles_end": 0}
        current = start_monday
        seen_weeks = set()
        while current <= last_day:
            wid = current.strftime("%Y-%m-%d")
            if wid not in seen_weeks:
                seen_weeks.add(wid)
                wt = _week_totals(wid)
                month_total["produced"] += wt["produced"]
                month_total["bb"] += wt["bb"]
                month_total["kettles_end"] += wt["kettles_end"]
            current += timedelta(days=7)

        month_total["total"] = month_total["produced"] + month_total["bb"] + month_total["kettles_end"]
        month_total["month"] = m
        month_total["label"] = month_names[m - 1]
        months.append(month_total)
    return jsonify(months)


# ── Init ──────────────────────────────────────────────────────────────

# Organic data paths
ORGANIC_DIR = os.path.join(DATA_DIR, "organic")
ORGANIC_RAW_PATH = os.path.join(ORGANIC_DIR, "raw_materials.json")
ORGANIC_RUNS_PATH = os.path.join(ORGANIC_DIR, "production_runs.json")
ORGANIC_FG_PATH = os.path.join(ORGANIC_DIR, "finished_goods.json")
ORGANIC_SALES_PATH = os.path.join(ORGANIC_DIR, "sales.json")
ORGANIC_CONTACTS_PATH = os.path.join(ORGANIC_DIR, "contacts.json")
os.makedirs(ORGANIC_DIR, exist_ok=True)


def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Organic Page ──────────────────────────────────────────────────────
@app.route("/organic")
@login_required
def organic_page():
    return render_template("organic.html")


# ── Organic: Get ingredient list from organic recipes ─────────────────
@app.route("/api/organic/ingredients", methods=["GET"])
@login_required
def organic_ingredients():
    recipes = load_recipes()
    # Map ingredient name -> unit
    ingredient_map = {}
    skip_patterns = ["filtered water", "top up water", "top kettle", "water to", "water -"]
    for name, data in recipes.items():
        cert = (data.get("certification") or "").lower()
        if cert != "organic":
            continue
        for section in ["kettle_overnight", "after_skim", "finishing", "add_to_jar"]:
            items = data.get(section, [])
            for item in items:
                item = item.strip()
                if not item:
                    continue
                # Skip water items
                if any(p in item.lower() for p in skip_patterns):
                    continue
                # Parse "50 kg Chicken Bones" or "500 ml Honey" etc.
                m = re.match(r'^(\d+\.?\d*)\s*(kg|g|ml|l|L)\s+(.+)', item, re.IGNORECASE)
                if m:
                    unit = m.group(2).lower()
                    if unit == "l":
                        unit = "L"
                    ing_name = m.group(3).strip()
                    ingredient_map[ing_name] = unit
                else:
                    # Check for "per liter" type items
                    if "per liter" in item.lower() or "per litre" in item.lower() or "/l" in item.lower():
                        m2 = re.match(r'^(\d+\.?\d*)\s*(.*)', item)
                        if m2:
                            ingredient_map[m2.group(2).strip()] = "ml"
                        continue
                    # Items like "10 Onion" (count-based)
                    m3 = re.match(r'^(\d+\.?\d*)\s+(.+)', item)
                    if m3:
                        ing_name = m3.group(2).strip()
                        # Special case: Turmeric Juice = container (750ml)
                        if "turmeric juice" in ing_name.lower():
                            ingredient_map[ing_name] = "container (750ml)"
                        else:
                            ingredient_map[ing_name] = "units"
                    else:
                        ingredient_map[item] = "units"

    # Return as list of {name, unit} sorted alphabetically
    result = [{"name": k, "unit": v} for k, v in sorted(ingredient_map.items())]
    return jsonify(result)


# ── Organic: Raw Material Inventory (FIFO) ────────────────────────────
@app.route("/api/organic/raw-materials", methods=["GET"])
@login_required
def get_raw_materials():
    return jsonify(_load_json(ORGANIC_RAW_PATH, []))


@app.route("/api/organic/raw-materials", methods=["POST"])
@login_required
def add_raw_material():
    data = request.json
    materials = _load_json(ORGANIC_RAW_PATH, [])
    entry = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(materials)),
        "item": data.get("item", ""),
        "supplier": data.get("supplier", ""),
        "date_received": data.get("date_received", ""),
        "supplier_lot": data.get("supplier_lot", ""),
        "quantity": float(data.get("quantity", 0)),
        "unit": data.get("unit", ""),
        "remaining": float(data.get("quantity", 0)),
        "created_at": datetime.now().isoformat(),
    }
    materials.append(entry)
    _save_json(ORGANIC_RAW_PATH, materials)
    # Auto-save supplier to contacts
    supplier = data.get("supplier", "").strip()
    if supplier:
        _add_contact("supplier", supplier)
    return jsonify({"success": True, "entry": entry})


@app.route("/api/organic/raw-materials/<entry_id>", methods=["DELETE"])
@login_required
def delete_raw_material(entry_id):
    materials = _load_json(ORGANIC_RAW_PATH, [])
    materials = [m for m in materials if m.get("id") != entry_id]
    _save_json(ORGANIC_RAW_PATH, materials)
    return jsonify({"success": True})


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


def _create_organic_run(week_id, day_idx, vessel, recipe_name, recipe_data, lot):
    """Create a production run record when an organic recipe is scheduled."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    # Check if already exists
    for r in runs:
        if r.get("week_id") == week_id and r.get("day_idx") == day_idx and r.get("vessel") == vessel:
            return  # Already tracked
    run = {
        "id": f"{week_id}_{day_idx}_{vessel}",
        "week_id": week_id,
        "day_idx": day_idx,
        "day_name": DAYS[day_idx],
        "vessel": vessel,
        "recipe": recipe_name,
        "lot": lot,
        "brand": recipe_data.get("brand", ""),
        "status": "scheduled",
        "ingredients_used": [],
        "amount_produced": 0,
        "created_at": datetime.now().isoformat(),
    }
    runs.append(run)
    _save_json(ORGANIC_RUNS_PATH, runs)


def _complete_organic_run(week_id, day_idx, produced_data):
    """When daily production is completed, deduct raw materials and create finished goods."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    materials = _load_json(ORGANIC_RAW_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    recipes = load_recipes()

    for run in runs:
        if run.get("week_id") != week_id or run.get("day_idx") != day_idx:
            continue
        if run.get("status") == "completed":
            continue

        vessel = run["vessel"]
        recipe_name = run["recipe"]
        recipe_data = recipes.get(recipe_name, {})
        if not recipe_data:
            continue

        # Get amount produced for this vessel
        vid = vessel.replace("(", "").replace(")", "")
        amount = 0
        if produced_data.get("produced"):
            try:
                amount = int(produced_data["produced"].get(vid, 0))
            except (ValueError, TypeError):
                pass

        # Deduct raw materials (FIFO) based on recipe ingredients
        ingredients_used = []
        is_115L = vessel == "115L"
        for section in ["kettle_overnight", "after_skim", "finishing", "add_to_jar"]:
            items = recipe_data.get(section, [])
            for item in items:
                # Parse quantity from ingredient line
                m = re.match(r'^(\d+\.?\d*)\s*(.*)', item.strip())
                if m:
                    qty_needed = float(m.group(1))
                    if is_115L:
                        qty_needed = qty_needed / 2
                    item_name = m.group(2).strip()
                else:
                    item_name = item.strip()
                    qty_needed = 0

                if qty_needed <= 0:
                    continue

                # FIFO deduction
                qty_remaining = qty_needed
                for mat in materials:
                    if mat["remaining"] <= 0:
                        continue
                    # Match by checking if the raw material item contains the ingredient name or vice versa
                    if not _ingredient_matches(mat["item"], item_name):
                        continue
                    deduct = min(qty_remaining, mat["remaining"])
                    mat["remaining"] = round(mat["remaining"] - deduct, 2)
                    qty_remaining = round(qty_remaining - deduct, 2)
                    ingredients_used.append({
                        "item": mat["item"],
                        "supplier_lot": mat["supplier_lot"],
                        "quantity_used": deduct,
                        "raw_material_id": mat["id"],
                    })
                    if qty_remaining <= 0:
                        break

                # If still remaining, record negative (flagged)
                if qty_remaining > 0:
                    ingredients_used.append({
                        "item": item_name,
                        "supplier_lot": "INSUFFICIENT_STOCK",
                        "quantity_used": qty_remaining,
                        "negative": True,
                    })

        run["status"] = "completed"
        run["ingredients_used"] = ingredients_used
        run["amount_produced"] = amount
        run["completed_at"] = datetime.now().isoformat()

        # Create finished goods entry
        if amount > 0:
            fg_entry = {
                "id": f"fg_{week_id}_{day_idx}_{vessel}",
                "run_id": run["id"],
                "recipe": recipe_name,
                "brand": recipe_data.get("brand", ""),
                "format": recipe_data.get("format", ""),
                "lot": run["lot"],
                "quantity_produced": amount,
                "quantity_remaining": amount,
                "vessel": vessel,
                "week_id": week_id,
                "day_idx": day_idx,
                "created_at": datetime.now().isoformat(),
            }
            fg.append(fg_entry)

    _save_json(ORGANIC_RUNS_PATH, runs)
    _save_json(ORGANIC_RAW_PATH, materials)
    _save_json(ORGANIC_FG_PATH, fg)


def _ingredient_matches(raw_item, recipe_ingredient):
    """Check if a raw material inventory item matches a recipe ingredient line."""
    raw_lower = raw_item.lower().strip()
    ing_lower = recipe_ingredient.lower().strip()
    # Direct containment check
    if raw_lower in ing_lower or ing_lower in raw_lower:
        return True
    # Check significant words overlap
    raw_words = set(raw_lower.split())
    ing_words = set(ing_lower.split())
    # Remove common filler words
    filler = {"of", "the", "a", "an", "and", "or", "to", "in", "per", "with"}
    raw_words -= filler
    ing_words -= filler
    if raw_words and ing_words:
        overlap = raw_words & ing_words
        if len(overlap) >= min(len(raw_words), len(ing_words)):
            return True
    return False


# ── Organic: Finished Goods ──────────────────────────────────────────
@app.route("/api/organic/finished-goods", methods=["GET"])
@login_required
def get_finished_goods():
    return jsonify(_load_json(ORGANIC_FG_PATH, []))


# ── Organic: Sales ───────────────────────────────────────────────────
@app.route("/api/organic/sales", methods=["GET"])
@login_required
def get_organic_sales():
    return jsonify(_load_json(ORGANIC_SALES_PATH, []))


@app.route("/api/organic/sales", methods=["POST"])
@login_required
def add_organic_sale():
    data = request.json
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])

    fg_id = data.get("fg_id", "")
    quantity = int(data.get("quantity", 0))

    # Find finished good and reduce
    fg_entry = None
    for f in fg:
        if f["id"] == fg_id:
            fg_entry = f
            break

    if not fg_entry:
        return jsonify({"error": "Finished good not found"}), 404

    sale = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales)),
        "fg_id": fg_id,
        "fg_lot": fg_entry.get("lot", ""),
        "recipe": fg_entry.get("recipe", ""),
        "brand": fg_entry.get("brand", ""),
        "format": fg_entry.get("format", ""),
        "quantity": quantity,
        "buyer": data.get("buyer", ""),
        "sale_date": data.get("sale_date", ""),
        "case_lot": data.get("case_lot", ""),
        "created_at": datetime.now().isoformat(),
    }
    sales.append(sale)

    fg_entry["quantity_remaining"] = fg_entry.get("quantity_remaining", 0) - quantity
    _save_json(ORGANIC_SALES_PATH, sales)
    _save_json(ORGANIC_FG_PATH, fg)
    # Auto-save buyer to contacts
    buyer = data.get("buyer", "").strip()
    if buyer:
        _add_contact("buyer", buyer)
    return jsonify({"success": True, "sale": sale})


@app.route("/api/organic/sales/<sale_id>", methods=["DELETE"])
@login_required
def delete_organic_sale(sale_id):
    sales = _load_json(ORGANIC_SALES_PATH, [])
    fg = _load_json(ORGANIC_FG_PATH, [])
    sale = None
    for s in sales:
        if s.get("id") == sale_id:
            sale = s
            break
    if sale:
        # Restore quantity to finished goods
        for f in fg:
            if f["id"] == sale.get("fg_id"):
                f["quantity_remaining"] = f.get("quantity_remaining", 0) + sale["quantity"]
                break
        sales = [s for s in sales if s.get("id") != sale_id]
        _save_json(ORGANIC_SALES_PATH, sales)
        _save_json(ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})


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
    """Scan a schedule for organic recipes and create production run records."""
    recipes = load_recipes()
    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    for day_key, vessels in schedule.items():
        day_idx = int(day_key)
        date = week_start + timedelta(days=day_idx)
        lot = date.strftime("%d%m%y")
        for vessel, recipe_name in vessels.items():
            if not recipe_name:
                continue
            recipe_data = recipes.get(recipe_name, {})
            cert = (recipe_data.get("certification") or "").lower()
            if cert == "organic":
                _create_organic_run(week_id, day_idx, vessel, recipe_name, recipe_data, lot)


# ── Hook: Complete organic runs when daily production is filed ─────────
def _check_organic_completion(week_id, day_idx, checklist_data):
    """When daily production is completed, process organic runs for that day."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    has_organic = any(
        r.get("week_id") == week_id and r.get("day_idx") == day_idx and r.get("status") != "completed"
        for r in runs
    )
    if has_organic:
        _complete_organic_run(week_id, day_idx, checklist_data)


if not os.path.exists(RECIPES_PATH):
    from default_recipes import DEFAULT_RECIPES
    save_recipes(DEFAULT_RECIPES)

if not os.path.exists(CCP_MASTER_PATH):
    save_ccp_master(DEFAULT_CCP_SECTIONS)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

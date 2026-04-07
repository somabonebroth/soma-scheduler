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
VESSELS = ["K1", "K2", "K3", "K4(115L)"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
CHECKLISTS_DIR = os.path.join(DATA_DIR, "checklists")
CCP_MASTER_PATH = os.path.join(DATA_DIR, "ccp_master.json")

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
    {"num": "5", "title": "DEPRESSURIZING & JAR REMOVAL", "items": [
        "Turn off heat; leave canner on burner until fully depressurized - no force-cooling",
        "Open counterweight/petcock once fully depressurized; wait additional 10 min",
        "Open lid away from body (steam burn risk)",
        "Remove jars gripping glass body or lid rim; minimize lid contact",
        "Do not retighten lids or tilt/move jars during cooling",
    ]},
    {"num": "6", "title": "COOLING & SEAL VERIFICATION (NEXT DAY)", "items": [
        "Cool undisturbed at room temp 12-24 hours",
        "Test each seal: press lid center - no flex or spring",
        "Dispose immediately of any unsealed jars - do NOT refrigerate",
    ]},
    {"num": "7", "title": "FINISHING & LABELLING", "items": [
        "Wash and dry jars if necessary",
        "Set label machine: date and LOT# match production schedule",
        "Label each case of 12: product name, expiry date, LOT number",
    ]},
    {"num": "8", "title": "INVENTORY & STORAGE", "items": [
        "Add finished inventory to Finished Goods Inventory with LOT for tracking",
        "Store in designated area, labelled - away from heat and direct sunlight",
        "Best before: within 1 year - confirm label matches",
    ]},
]


# -- Auth --
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# -- Data helpers --
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
    files = sorted(os.listdir(SCHEDULES_DIR), reverse=True)
    return [f.replace(".json", "") for f in files if f.endswith(".json")]

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

def get_current_week_id():
    today = datetime.today()
    day = today.weekday()
    monday = today - timedelta(days=day)
    return monday.strftime("%Y-%m-%d")

def get_day_assignments(week_id, day_idx):
    schedule = load_schedule(week_id)
    if schedule and schedule.get("schedule"):
        day_key = str(day_idx)
        if day_key in schedule["schedule"]:
            return schedule["schedule"][day_key]
    return {}

def get_prev_day_info(week_id, day_idx):
    if day_idx == 0:
        prev_week = datetime.strptime(week_id, "%Y-%m-%d") - timedelta(days=7)
        prev_week_id = prev_week.strftime("%Y-%m-%d")
        return get_day_assignments(prev_week_id, 6)
    else:
        return get_day_assignments(week_id, day_idx - 1)

def parse_recipe_pdf_text(text):
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
    raw_name = lines[0]
    recipe = {
        "yield": None, "format": "", "brand": "",
        "special_instructions": [], "kettle_overnight": [],
        "after_skim": [], "finishing": [], "add_to_jar": [],
    }
    upper = raw_name.upper()
    if "SS-876ML" in upper or "SS876ML" in upper:
        recipe["format"] = "SS-876ML"
    elif "FZ-750ML" in upper or "FZ750ML" in upper:
        recipe["format"] = "FZ-750ML"
    elif "SS-750ML" in upper or "SS750ML" in upper:
        recipe["format"] = "SS-750ML"
    # Try to extract brand from name (e.g. "RIPE-LIQUID GOLD-SS-876ML")
    # Split on first hyphen to get brand
    parts = raw_name.split("-", 1)
    if len(parts) > 1:
        recipe["brand"] = parts[0].strip()
        # Remove format suffix from recipe name
        recipe_part = parts[1].strip()
        for suffix in ["-SS-876ML", "-FZ-750ML", "-SS-750ML", "-SS876ML", "-FZ750ML", "-SS750ML"]:
            if recipe_part.upper().endswith(suffix.upper()):
                recipe_part = recipe_part[:len(recipe_part)-len(suffix)].strip()
                break
        name = recipe_part
    else:
        name = raw_name
    for line in lines:
        m = re.search(r"Target Yield:\s*(\d+)", line, re.IGNORECASE)
        if m:
            recipe["yield"] = int(m.group(1))
            break
    if recipe["yield"] is None:
        recipe["yield"] = 190 if "FZ" in recipe["format"] else 150
    current_section = None
    in_special = False
    for line in lines[1:]:
        ll = line.lower().strip()
        if "target yield" in ll:
            continue
        if ll == "special instructions:" or ll.startswith("special instructions"):
            in_special = True
            continue
        if "add to kettle overnight" in ll:
            in_special = False
            current_section = "kettle_overnight"
            continue
        if "add directly to kettle after skim" in ll or "add to kettle after skim" in ll:
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


# -- Auth routes --
@app.route("/login")
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    if data.get("password") == APP_PASSWORD:
        session["authenticated"] = True
        return jsonify({"success": True})
    return jsonify({"error": "Incorrect password"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})


# -- Page routes --
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
def daily_production_page(week_id, day_idx):
    return render_template("daily_production.html", week_id=week_id, day_idx=day_idx)

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


# -- Recipe API --
@app.route("/api/recipes", methods=["GET"])
@login_required
def get_recipes():
    return jsonify(load_recipes())

@app.route("/api/recipes/upload", methods=["POST"])
@login_required
def upload_recipe():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Must be a PDF file"}), 400
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
    finally:
        os.unlink(tmp_path)
    if not text.strip():
        return jsonify({"error": "Could not extract text from PDF"}), 400
    result = parse_recipe_pdf_text(text)
    if not result:
        return jsonify({"error": "Could not parse recipe format"}), 400
    recipes = load_recipes()
    recipes[result["name"]] = result["data"]
    save_recipes(recipes)
    return jsonify({"success": True, "name": result["name"], "recipe": result["data"]})

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
    recipes = load_recipes()
    if name not in recipes:
        return jsonify({"error": "Recipe not found"}), 404
    data = request.json
    new_name = data.get("name", name)
    recipe_data = data.get("data", {})
    if new_name != name:
        del recipes[name]
    recipes[new_name] = recipe_data
    save_recipes(recipes)
    return jsonify({"success": True, "name": new_name})

@app.route("/api/recipes/<path:name>", methods=["DELETE"])
@login_required
def delete_recipe(name):
    recipes = load_recipes()
    if name in recipes:
        del recipes[name]
        save_recipes(recipes)
        return jsonify({"success": True})
    return jsonify({"error": "Recipe not found"}), 404

@app.route("/api/recipes/grouped", methods=["GET"])
@login_required
def get_recipes_grouped():
    recipes = load_recipes()
    groups = {}
    for name, data in recipes.items():
        brand = data.get("brand", "Other")
        if not brand:
            brand = "Other"
        if brand not in groups:
            groups[brand] = []
        groups[brand].append({"name": name, "format": data.get("format", ""), "yield": data.get("yield", "")})
    for brand in groups:
        groups[brand].sort(key=lambda x: (0 if "SS" in x.get("format", "") else 1, x["name"]))
    return jsonify(groups)

@app.route("/api/recipes/upload-json", methods=["POST"])
@login_required
def add_recipe_manual():
    data = request.json
    name = data.get("name", "")
    recipe_data = data.get("data", {})
    if not name:
        return jsonify({"error": "Name required"}), 400
    recipes = load_recipes()
    recipes[name] = recipe_data
    save_recipes(recipes)
    return jsonify({"success": True, "name": name})


# -- Schedule API --
@app.route("/api/schedule", methods=["POST"])
@login_required
def save_schedule_route():
    data = request.json
    week_id = data.get("week_id")
    schedule = data.get("schedule")
    notes = data.get("notes", "")
    if not week_id or schedule is None:
        return jsonify({"error": "Missing data"}), 400
    save_schedule(week_id, {"schedule": schedule, "notes": notes})
    return jsonify({"success": True})

@app.route("/api/schedule/<week_id>", methods=["GET"])
@login_required
def get_schedule(week_id):
    data = load_schedule(week_id)
    if data is None:
        return jsonify({"schedule": None})
    return jsonify(data)

@app.route("/api/schedule/current", methods=["GET"])
@login_required
def get_current_schedule():
    week_id = get_current_week_id()
    data = load_schedule(week_id)
    if data is None:
        return jsonify({"schedule": None, "week_id": week_id})
    return jsonify({"schedule": data.get("schedule"), "notes": data.get("notes", ""), "week_id": week_id})

@app.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    return jsonify(list_schedules())


# -- Generate PDFs --
@app.route("/api/generate", methods=["POST"])
@login_required
def generate_pdfs():
    data = request.json
    week_id = data.get("week_id")
    schedule = data.get("schedule")
    notes = data.get("notes", "")
    if not week_id or not schedule:
        return jsonify({"error": "Missing data"}), 400

    recipes = load_recipes()
    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    save_schedule(week_id, {"schedule": schedule, "notes": notes})

    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    os.makedirs(week_pdf_dir, exist_ok=True)

    days_map = {}
    for d_idx in range(7):
        day_key = str(d_idx)
        if day_key in schedule:
            vessels = []
            for vessel in VESSELS:
                recipe_name = schedule[day_key].get(vessel, "")
                vessels.append({"vessel": vessel, "recipe": recipe_name})
            days_map[d_idx] = vessels
        else:
            days_map[d_idx] = []

    logo_path = os.path.join(app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None

    schedule_path = os.path.join(week_pdf_dir, "Weekly_Schedule.pdf")
    generate_weekly_schedule_pdf(schedule_path, week_start, days_map, recipes, notes, logo_path)

    generated = ["Weekly_Schedule.pdf"]
    for d_idx in range(7):
        date = week_start + timedelta(days=d_idx)
        assignments = days_map.get(d_idx, [])
        has_active = any(a.get("recipe") for a in assignments)
        if has_active:
            filename = DAYS[d_idx] + "_Production.pdf"
            path = os.path.join(week_pdf_dir, filename)
            generate_daily_package_pdf(path, date, assignments, recipes, logo_path)
            generated.append(filename)

    return jsonify({"success": True, "files": generated, "week_id": week_id})

@app.route("/api/pdfs/<week_id>", methods=["GET"])
@login_required
def list_pdfs(week_id):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify([])
    files = sorted(os.listdir(week_pdf_dir))
    return jsonify([f for f in files if f.endswith(".pdf")])

@app.route("/api/pdf/<week_id>/<filename>", methods=["GET"])
@login_required
def download_pdf(week_id, filename):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    return send_from_directory(week_pdf_dir, filename, as_attachment=True)

@app.route("/api/pdfs/<week_id>/download-all", methods=["GET"])
@login_required
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
                     download_name="Soma_Production_" + week_id + ".zip")


# -- Daily Production API --
@app.route("/api/daily-production/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
def get_daily_production(week_id, day_idx):
    recipes = load_recipes()
    start_assignments = get_day_assignments(week_id, day_idx)
    finish_assignments = get_prev_day_info(week_id, day_idx)

    start_data = {}
    for vessel in VESSELS:
        recipe_name = start_assignments.get(vessel, "")
        if recipe_name and recipe_name in recipes:
            start_data[vessel] = {"recipe": recipe_name, "details": recipes[recipe_name]}
        else:
            start_data[vessel] = None

    finish_data = {}
    for vessel in VESSELS:
        recipe_name = finish_assignments.get(vessel, "")
        if recipe_name and recipe_name in recipes:
            finish_data[vessel] = {"recipe": recipe_name, "details": recipes[recipe_name]}
        else:
            finish_data[vessel] = None

    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    prod_date = week_start + timedelta(days=day_idx)
    lot = prod_date.strftime("%d%m%y")

    prev_date = prod_date - timedelta(days=1)
    prev_lot = prev_date.strftime("%d%m%y")

    return jsonify({
        "start": start_data,
        "finish": finish_data,
        "date": prod_date.strftime("%d/%m/%Y"),
        "day_name": DAYS[day_idx],
        "lot": lot,
        "prev_lot": prev_lot,
    })

@app.route("/api/daily-production/<week_id>/<int:day_idx>/save", methods=["POST"])
@login_required
def save_daily_production(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    save_checklist_data(week_id, day_idx, data)
    return jsonify({"success": True})


# -- Label Generation --
@app.route("/api/label", methods=["POST"])
@login_required
def generate_label():
    data = request.json
    product_name = data.get("product_name", "")
    lot = data.get("lot", "")
    production_date = data.get("production_date", "")

    if not product_name:
        return jsonify({"error": "Missing product name"}), 400

    try:
        prod_date = datetime.strptime(production_date, "%d/%m/%Y")
    except Exception:
        prod_date = datetime.today()

    best_before = prod_date + timedelta(days=365)

    logo_path = os.path.join(app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None

    label_buffer = io.BytesIO()
    generate_label_pdf(label_buffer, product_name, lot, best_before.strftime("%d/%m/%Y"))
    label_buffer.seek(0)

    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', product_name)
    return send_file(label_buffer, mimetype="application/pdf", as_attachment=True,
                     download_name="Label_" + safe_name + "_" + lot + ".pdf")


# -- CCP Checklist --
@app.route("/api/checklist/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
def get_checklist(week_id, day_idx):
    data = load_checklist(week_id, day_idx)
    start_info = get_day_assignments(week_id, day_idx)
    finish_info = get_prev_day_info(week_id, day_idx)
    ccp = load_ccp_master()
    return jsonify({
        "checklist": data,
        "start_info": start_info,
        "finish_info": finish_info,
        "ccp_sections": ccp,
    })

@app.route("/api/checklist/<week_id>/<int:day_idx>", methods=["POST"])
@login_required
def save_checklist_route(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    save_checklist_data(week_id, day_idx, data)
    return jsonify({"success": True})

@app.route("/api/checklist/<week_id>/<int:day_idx>/complete", methods=["POST"])
@login_required
def complete_checklist(week_id, day_idx):
    data = request.json
    data["last_updated"] = datetime.now().isoformat()
    data["completed"] = True
    save_checklist_data(week_id, day_idx, data)

    start_info = get_day_assignments(week_id, day_idx)
    finish_info = get_prev_day_info(week_id, day_idx)

    active_vessels = []
    for vessel in VESSELS:
        recipe = start_info.get(vessel, "") or finish_info.get(vessel, "")
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

    return jsonify({"success": True, "filename": filename})

@app.route("/api/checklist-status/<week_id>", methods=["GET"])
@login_required
def checklist_status(week_id):
    statuses = {}
    for d_idx in range(7):
        data = load_checklist(week_id, d_idx)
        if data and data.get("completed"):
            statuses[str(d_idx)] = "completed"
        elif data:
            statuses[str(d_idx)] = "in_progress"
        else:
            statuses[str(d_idx)] = "not_started"
    return jsonify(statuses)


# -- Master CCP --
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


# -- Traceability --
@app.route("/api/traceability", methods=["GET"])
@login_required
def get_traceability():
    weeks = list_schedules()
    records = []
    for week_id in weeks:
        week_record = {"week_id": week_id, "days": []}
        for d_idx in range(7):
            cl = load_checklist(week_id, d_idx)
            if cl and cl.get("completed"):
                week_record["days"].append({
                    "day_idx": d_idx,
                    "day_name": DAYS[d_idx],
                    "completed": True,
                    "last_updated": cl.get("last_updated", ""),
                })
        if week_record["days"]:
            records.append(week_record)
    return jsonify(records)

@app.route("/api/traceability/<week_id>/<int:day_idx>", methods=["DELETE"])
@login_required
def delete_traceability_record(week_id, day_idx):
    path = os.path.join(CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    if os.path.exists(path):
        os.unlink(path)
        # Also remove completed PDF if it exists
        pdf_path = os.path.join(PDF_DIR, week_id, DAYS[day_idx] + "_Completed_Checklist.pdf")
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
        return jsonify({"success": True})
    return jsonify({"error": "Record not found"}), 404


# -- Production Tracker --
@app.route("/api/production-tracker/<week_id>", methods=["GET"])
@login_required
def get_production_tracker(week_id):
    daily_totals = []
    for d_idx in range(7):
        cl = load_checklist(week_id, d_idx)
        total = 0
        if cl and cl.get("produced"):
            for vessel_id, amount in cl["produced"].items():
                try:
                    total += int(amount)
                except (ValueError, TypeError):
                    pass
        daily_totals.append({
            "day_idx": d_idx,
            "day_name": DAYS[d_idx],
            "total": total,
            "has_data": cl is not None,
        })
    return jsonify(daily_totals)

@app.route("/production-tracker")
@login_required
def production_tracker_page():
    return render_template("production_tracker.html")


# -- Init --
if not os.path.exists(RECIPES_PATH):
    from default_recipes import DEFAULT_RECIPES
    save_recipes(DEFAULT_RECIPES)

if not os.path.exists(CCP_MASTER_PATH):
    save_ccp_master(DEFAULT_CCP_SECTIONS)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

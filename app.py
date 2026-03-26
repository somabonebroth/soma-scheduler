"""
Soma Bone Broth — Production Scheduler v3
"""

from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, session, redirect, url_for
from datetime import datetime, timedelta
from pdf_engine import generate_weekly_schedule_pdf, generate_daily_package_pdf, generate_filled_checklist_pdf
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

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")
CHECKLISTS_DIR = os.path.join(DATA_DIR, "checklists")

for d in [DATA_DIR, SCHEDULES_DIR, PDF_DIR, CHECKLISTS_DIR]:
    os.makedirs(d, exist_ok=True)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def load_recipes():
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            return json.load(f)
    return {}

def save_recipes(recipes):
    with open(RECIPES_PATH, "w") as f:
        json.dump(recipes, f, indent=2)

def parse_recipe_pdf_text(text):
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None
    name = lines[0]
    recipe = {
        "yield": None, "format": "",
        "special_instructions": [], "kettle_overnight": [],
        "after_skim": [], "finishing": [], "add_to_jar": [],
    }
    if "SS-876ML" in name.upper() or "SS876ML" in name.upper():
        recipe["format"] = "SS-876ML"
    elif "FZ-750ML" in name.upper() or "FZ750ML" in name.upper():
        recipe["format"] = "FZ-750ML"
    elif "SS-750ML" in name.upper() or "SS750ML" in name.upper():
        recipe["format"] = "SS-750ML"
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


def load_schedule(week_id):
    path = os.path.join(SCHEDULES_DIR, f"{week_id}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def save_schedule(week_id, data):
    path = os.path.join(SCHEDULES_DIR, f"{week_id}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def list_schedules():
    files = sorted(os.listdir(SCHEDULES_DIR), reverse=True)
    return [f.replace(".json", "") for f in files if f.endswith(".json")]

def get_checklist_path(week_id, day_idx):
    return os.path.join(CHECKLISTS_DIR, f"{week_id}_day{day_idx}.json")

def load_checklist(week_id, day_idx):
    path = get_checklist_path(week_id, day_idx)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def save_checklist_data(week_id, day_idx, data):
    path = get_checklist_path(week_id, day_idx)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        # ── Auth ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("index"))
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


# ── Pages ──────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/checklist/<week_id>/<int:day_idx>")
@login_required
def checklist_page(week_id, day_idx):
    return render_template("checklist.html", week_id=week_id, day_idx=day_idx)


# ── Recipes ────────────────────────────────────────────────────────────
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

@app.route("/api/recipes/<path:name>", methods=["DELETE"])
@login_required
def delete_recipe(name):
    recipes = load_recipes()
    if name in recipes:
        del recipes[name]
        save_recipes(recipes)
        return jsonify({"success": True})
    return jsonify({"error": "Recipe not found"}), 404

@app.route("/api/recipes/names", methods=["GET"])
@login_required
def get_recipe_names():
    recipes = load_recipes()
    return jsonify(sorted(recipes.keys()))


# ── Schedules ──────────────────────────────────────────────────────────
@app.route("/api/schedule", methods=["POST"])
@login_required
def save_schedule_route():
    data = request.json
    week_id = data.get("week_id")
    schedule = data.get("schedule")
    notes = data.get("notes", "")
    if not week_id or schedule is None:
        return jsonify({"error": "Missing week_id or schedule"}), 400
    save_schedule(week_id, {"schedule": schedule, "notes": notes})
    return jsonify({"success": True})

@app.route("/api/schedule/<week_id>", methods=["GET"])
@login_required
def get_schedule(week_id):
    data = load_schedule(week_id)
    if data is None:
        return jsonify({"schedule": None})
    return jsonify(data)

@app.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    return jsonify(list_schedules())

# ── PDF Generation ────────────────────────────────────────────────────
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

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    generated = ["Weekly_Schedule.pdf"]

    for d_idx in range(7):
        date = week_start + timedelta(days=d_idx)
        assignments = days_map.get(d_idx, [])
        has_active = any(a.get("recipe") for a in assignments)
        if has_active:
            filename = f"{day_names[d_idx]}_Production.pdf"
            path = os.path.join(week_pdf_dir, filename)
            generate_daily_package_pdf(path, date, assignments, recipes, logo_path)
            generated.append(filename)

    return jsonify({"success": True, "files": generated, "week_id": week_id})

@app.route("/api/pdf/<week_id>/<filename>", methods=["GET"])
@login_required
def download_pdf(week_id, filename):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    return send_from_directory(week_pdf_dir, filename, as_attachment=True)

@app.route("/api/pdfs/<week_id>", methods=["GET"])
@login_required
def list_pdfs(week_id):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify([])
    files = sorted(os.listdir(week_pdf_dir))
    return jsonify([f for f in files if f.endswith(".pdf")])

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
                     download_name=f"Soma_Production_{week_id}.zip")


# ── Digital Checklists ─────────────────────────────────────────────────
@app.route("/api/checklist/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
def get_checklist(week_id, day_idx):
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
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    filename = f"{day_names[day_idx]}_Completed_Checklist.pdf"
    pdf_path = os.path.join(week_pdf_dir, filename)

    generate_filled_checklist_pdf(pdf_path, date, active_vessels, data, logo_path)

    return jsonify({"success": True, "filename": filename})


# ── Init recipes ───────────────────────────────────────────────────────
if not os.path.exists(RECIPES_PATH):
    from default_recipes import DEFAULT_RECIPES
    save_recipes(DEFAULT_RECIPES)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

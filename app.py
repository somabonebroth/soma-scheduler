"""
Soma Bone Broth — Production Scheduler
Flask backend: schedule management, PDF generation, recipe CRUD
"""

from flask import Flask, render_template, request, jsonify, send_file, send_from_directory
from datetime import datetime, timedelta
from pdf_engine import generate_weekly_schedule_pdf, generate_daily_package_pdf
import json
import os
import re

app = Flask(__name__, static_folder="static", template_folder="templates")

# ── Paths ──────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
RECIPES_PATH = os.path.join(DATA_DIR, "recipes.json")
SCHEDULES_DIR = os.path.join(DATA_DIR, "schedules")
PDF_DIR = os.path.join(DATA_DIR, "pdfs")

for d in [DATA_DIR, SCHEDULES_DIR, PDF_DIR]:
    os.makedirs(d, exist_ok=True)


# ── Recipe helpers ─────────────────────────────────────────────────────
def load_recipes():
    if os.path.exists(RECIPES_PATH):
        with open(RECIPES_PATH, "r") as f:
            return json.load(f)
    return {}

def save_recipes(recipes):
    with open(RECIPES_PATH, "w") as f:
        json.dump(recipes, f, indent=2)

def parse_recipe_pdf_text(text):
    """Parse a recipe from extracted PDF text matching Soma's format."""
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None

    # First line is the recipe name
    name = lines[0]

    recipe = {
        "yield": None,
        "format": "",
        "special_instructions": [],
        "kettle_overnight": [],
        "after_skim": [],
        "finishing": [],
        "add_to_jar": [],
    }

    # Detect format from name
    if "SS-876ML" in name.upper() or "SS876ML" in name.upper():
        recipe["format"] = "SS-876ML"
    elif "FZ-750ML" in name.upper() or "FZ750ML" in name.upper():
        recipe["format"] = "FZ-750ML"
    elif "SS-750ML" in name.upper() or "SS750ML" in name.upper():
        recipe["format"] = "SS-750ML"

    # Parse target yield
    for line in lines:
        m = re.search(r"Target Yield:\s*(\d+)", line, re.IGNORECASE)
        if m:
            recipe["yield"] = int(m.group(1))
            break

    # Default yield if not found
    if recipe["yield"] is None:
        if "FZ" in recipe["format"]:
            recipe["yield"] = 190
        else:
            recipe["yield"] = 150

    # Identify sections
    current_section = None
    in_special = False
    skip_lines = {name.lower(), "target yield:", "special instructions:"}

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


# ── Schedule helpers ───────────────────────────────────────────────────
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


# ── Routes: Pages ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── Routes: API ────────────────────────────────────────────────────────
@app.route("/api/recipes", methods=["GET"])
def get_recipes():
    return jsonify(load_recipes())

@app.route("/api/recipes/upload", methods=["POST"])
def upload_recipe():
    """Upload a PDF recipe card and parse it."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Must be a PDF file"}), 400

    # Save temp file and extract text
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

    # Save to recipes
    recipes = load_recipes()
    recipes[result["name"]] = result["data"]
    save_recipes(recipes)

    return jsonify({"success": True, "name": result["name"], "recipe": result["data"]})

@app.route("/api/recipes/names", methods=["GET"])
def get_recipe_names():
    recipes = load_recipes()
    names = sorted(recipes.keys())
    return jsonify(names)


@app.route("/api/schedule", methods=["POST"])
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
def get_schedule(week_id):
    data = load_schedule(week_id)
    if data is None:
        return jsonify({"schedule": None})
    return jsonify(data)

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    return jsonify(list_schedules())


@app.route("/api/generate", methods=["POST"])
def generate_pdfs():
    """Generate weekly schedule PDF + 7 daily production PDFs."""
    data = request.json
    week_id = data.get("week_id")
    schedule = data.get("schedule")
    notes = data.get("notes", "")

    if not week_id or not schedule:
        return jsonify({"error": "Missing data"}), 400

    recipes = load_recipes()
    week_start = datetime.strptime(week_id, "%Y-%m-%d")

    # Save schedule
    save_schedule(week_id, {"schedule": schedule, "notes": notes})

    # Create week PDF directory
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    os.makedirs(week_pdf_dir, exist_ok=True)

    # Build schedule_data dict
    days_map = {}
    for d_idx in range(7):
        day_key = str(d_idx)
        if day_key in schedule:
            vessels = []
            for vessel in ["K1", "K2", "K3"]:
                recipe_name = schedule[day_key].get(vessel, "")
                vessels.append({"vessel": vessel, "recipe": recipe_name})
            days_map[d_idx] = vessels
        else:
            days_map[d_idx] = []

    # Generate weekly schedule
    schedule_path = os.path.join(week_pdf_dir, "Weekly_Schedule.pdf")
    generate_weekly_schedule_pdf(schedule_path, week_start, days_map, recipes, notes)

    # Generate daily packages
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    generated = ["Weekly_Schedule.pdf"]

    for d_idx in range(7):
        date = week_start + timedelta(days=d_idx)
        assignments = days_map.get(d_idx, [])
        has_active = any(a.get("recipe") for a in assignments)
        if has_active:
            filename = f"{day_names[d_idx]}_Production.pdf"
            path = os.path.join(week_pdf_dir, filename)
            generate_daily_package_pdf(path, date, assignments, recipes)
            generated.append(filename)

    return jsonify({"success": True, "files": generated, "week_id": week_id})


@app.route("/api/pdf/<week_id>/<filename>", methods=["GET"])
def download_pdf(week_id, filename):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    return send_from_directory(week_pdf_dir, filename, as_attachment=True)

@app.route("/api/pdfs/<week_id>", methods=["GET"])
def list_pdfs(week_id):
    week_pdf_dir = os.path.join(PDF_DIR, week_id)
    if not os.path.exists(week_pdf_dir):
        return jsonify([])
    files = sorted(os.listdir(week_pdf_dir))
    return jsonify([f for f in files if f.endswith(".pdf")])


if __name__ == "__main__":
    # Initialize default recipes if none exist
    if not os.path.exists(RECIPES_PATH):
        from default_recipes import DEFAULT_RECIPES
        save_recipes(DEFAULT_RECIPES)
        print(f"✓ Initialized {len(DEFAULT_RECIPES)} recipes")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

"""
equipment.py — Equipment & Maintenance Blueprint

Routes:
  GET  /equipment                     equipment_page
  GET  /api/equipment                 get_equipment
  POST /api/equipment                 add_equipment
  PUT  /api/equipment/<eq_id>         update_equipment
  DELETE /api/equipment/<eq_id>       delete_equipment
  POST /api/equipment/<eq_id>/log     add_service_log
  DELETE /api/equipment/<eq_id>/log/<log_id>  delete_service_log
"""
import os
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify

from helpers import _load_json, _save_json, login_required, _render, DATA_DIR

logger = logging.getLogger(__name__)

equipment_bp = Blueprint("equipment", __name__)

EQUIPMENT_PATH = os.path.join(DATA_DIR, "equipment.json")

# ══════════════════════════════════════════════════════════════════════════════

def _load_equipment():
    return _load_json(EQUIPMENT_PATH, [])

def _save_equipment(data):
    _save_json(EQUIPMENT_PATH, data)




@equipment_bp.route("/equipment")
@login_required
def equipment_page():
    return _render("equipment.html")


@equipment_bp.route("/api/equipment", methods=["GET"])
@login_required
def get_equipment():
    return jsonify(_load_equipment())


@equipment_bp.route("/api/equipment", methods=["POST"])
@login_required
def add_equipment():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    items = _load_equipment()
    entry = {
        "id":               "eq_" + datetime.now().strftime("%Y%m%d%H%M%S") + str(len(items)),
        "name":             name,
        "type":             (data.get("type") or "").strip(),
        "model":            (data.get("model") or "").strip(),
        "serial_number":    (data.get("serial_number") or "").strip(),
        "asset_tag":        (data.get("asset_tag") or "").strip(),
        "location":         (data.get("location") or "").strip(),
        "status":           data.get("status", "active"),
        "purchase_date":    (data.get("purchase_date") or "").strip(),
        "purchase_price":   data.get("purchase_price"),
        "contact_primary":  (data.get("contact_primary") or "").strip(),
        "contact_secondary":(data.get("contact_secondary") or "").strip(),
        "warranty_expiry":  (data.get("warranty_expiry") or "").strip(),
        "warranty_contact": (data.get("warranty_contact") or "").strip(),
        "warranty_notes":   (data.get("warranty_notes") or "").strip(),
        "service_interval": (data.get("service_interval") or "").strip(),
        "last_service_date":(data.get("last_service_date") or "").strip(),
        "next_service_date":(data.get("next_service_date") or "").strip(),
        "notes":            (data.get("notes") or "").strip(),
        "service_log":      [],
        "created_at":       datetime.now().isoformat(),
    }
    items.append(entry)
    _save_equipment(items)
    return jsonify({"ok": True, "entry": entry})


@equipment_bp.route("/api/equipment/<eq_id>", methods=["PUT"])
@login_required
def update_equipment(eq_id):
    data = request.get_json() or {}
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    fields = [
        "name", "type", "model", "serial_number", "asset_tag", "location",
        "status", "purchase_date", "purchase_price", "contact_primary",
        "contact_secondary", "warranty_expiry", "warranty_contact",
        "warranty_notes", "service_interval", "last_service_date",
        "next_service_date", "notes",
    ]
    for f in fields:
        if f in data:
            entry[f] = data[f]
    entry["updated_at"] = datetime.now().isoformat()
    _save_equipment(items)
    return jsonify({"ok": True, "entry": entry})


@equipment_bp.route("/api/equipment/<eq_id>", methods=["DELETE"])
@login_required
def delete_equipment(eq_id):
    items = _load_equipment()
    items = [e for e in items if e["id"] != eq_id]
    _save_equipment(items)
    return jsonify({"ok": True})


@equipment_bp.route("/api/equipment/<eq_id>/log", methods=["POST"])
@login_required
def add_service_log(eq_id):
    data = request.get_json() or {}
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    log_entry = {
        "id":           "log_" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "date":         (data.get("date") or datetime.now().strftime("%Y-%m-%d")),
        "type":         (data.get("type") or "Maintenance").strip(),
        "description":  (data.get("description") or "").strip(),
        "performed_by": (data.get("performed_by") or "").strip(),
        "cost":         data.get("cost"),
        "parts":        (data.get("parts") or "").strip(),
        "next_action":  (data.get("next_action") or "").strip(),
        "created_at":   datetime.now().isoformat(),
    }
    if not entry.get("service_log"):
        entry["service_log"] = []
    entry["service_log"].append(log_entry)
    # Auto-update last service date
    if log_entry["date"]:
        entry["last_service_date"] = log_entry["date"]
    entry["updated_at"] = datetime.now().isoformat()
    _save_equipment(items)
    return jsonify({"ok": True, "log_entry": log_entry})


@equipment_bp.route("/api/equipment/<eq_id>/log/<log_id>", methods=["DELETE"])
@login_required
def delete_service_log(eq_id, log_id):
    items = _load_equipment()
    entry = next((e for e in items if e["id"] == eq_id), None)
    if not entry:
        return jsonify({"error": "Not found"}), 404
    entry["service_log"] = [l for l in (entry.get("service_log") or []) if l["id"] != log_id]
    _save_equipment(items)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY AUDIT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

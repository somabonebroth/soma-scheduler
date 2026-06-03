"""suppliers.py — Supplier management blueprint extracted from app.py.

First blueprint of the app.py split (CLAUDE.md "Pending architectural work").
Pure code-move: identical routes, URLs, and auth behaviour.

Follows the ripe_orders.py convention — a self-contained blueprint that defines
its own login_required (so it never imports app.py at module load, avoiding a
circular import) and pulls its IO + path foundation from helpers.py.
"""
import os
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, redirect, url_for

from helpers import _load_json, _save_json, INVENTORY_DIR

suppliers_bp = Blueprint("suppliers", __name__)

SUPPLIERS_PATH = os.path.join(INVENTORY_DIR, "suppliers.json")


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


def _load_suppliers():
    return _load_json(SUPPLIERS_PATH, [])

def _save_suppliers(data):
    _save_json(SUPPLIERS_PATH, data)

@suppliers_bp.route("/api/suppliers", methods=["GET"])
@login_required
def get_suppliers():
    return jsonify(_load_suppliers())

@suppliers_bp.route("/api/suppliers", methods=["POST"])
@login_required
def create_supplier():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    suppliers = _load_suppliers()
    if any(s["name"].lower() == name.lower() for s in suppliers):
        return jsonify({"error": "Supplier already exists"}), 409
    supplier = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "name": name,
        "ingredients": data.get("ingredients") or [],
    }
    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            supplier[field] = (data[field] or "").strip()
    suppliers.append(supplier)
    _save_suppliers(suppliers)
    return jsonify(supplier), 201

@suppliers_bp.route("/api/suppliers/<sid>", methods=["PUT"])
@login_required
def update_supplier(sid):
    data = request.get_json(force=True) or {}
    suppliers = _load_suppliers()
    idx = next((i for i, s in enumerate(suppliers) if s["id"] == sid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if any(s["name"].lower() == name.lower() and s["id"] != sid for s in suppliers):
            return jsonify({"error": "Name taken"}), 409
        suppliers[idx]["name"] = name
    if "ingredients" in data:
        suppliers[idx]["ingredients"] = data["ingredients"]
    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            suppliers[idx][field] = (data[field] or "").strip()
    _save_suppliers(suppliers)
    return jsonify(suppliers[idx])

@suppliers_bp.route("/api/suppliers/<sid>", methods=["DELETE"])
@login_required
def delete_supplier(sid):
    suppliers = _load_suppliers()
    suppliers = [s for s in suppliers if s["id"] != sid]
    _save_suppliers(suppliers)
    return jsonify({"ok": True})

@suppliers_bp.route("/api/suppliers/<sid>/ingredients", methods=["PUT"])
@login_required
def update_supplier_ingredients(sid):
    data = request.get_json(force=True) or {}
    ingredients = data.get("ingredients", [])
    suppliers = _load_suppliers()
    idx = next((i for i, s in enumerate(suppliers) if s["id"] == sid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    suppliers[idx]["ingredients"] = ingredients
    _save_suppliers(suppliers)
    return jsonify(suppliers[idx])

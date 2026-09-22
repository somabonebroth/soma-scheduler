"""suppliers.py — Supplier management blueprint extracted from app.py.

First blueprint of the app.py split (CLAUDE.md "Pending architectural work").
Pure code-move: identical routes, URLs, and auth behaviour.

Follows the ripe_orders.py convention — a self-contained blueprint that defines
its own manager_required (so it never imports app.py at module load, avoiding a
circular import) and pulls its IO + path foundation from helpers.py.
"""
import os
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, redirect, url_for

from helpers import _load_json, _save_json, INVENTORY_DIR

suppliers_bp = Blueprint("suppliers", __name__)

SUPPLIERS_PATH = os.path.join(INVENTORY_DIR, "suppliers.json")


def manager_required(f):
    """Local copy of app.py's manager_required (verbatim) — every route in this
    blueprint is a management task on the HOO's desktop, not the production
    tablet (2026-08-18 two-role split). Sessions from before roles existed count
    as manager (see app.current_role)."""
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


def _load_suppliers():
    """Load the suppliers list from disk."""
    return _load_json(SUPPLIERS_PATH, [])


CERT_EXPIRING_DAYS = 60   # "expiring" window for a supplier certificate


def _cert_status(expiry):
    """Classify a supplier certificate by its expiry date (YYYY-MM-DD or blank).
    Returns (status, days_left): 'none' (no date recorded), 'expired',
    'expiring' (within CERT_EXPIRING_DAYS), or 'current'. One rule, computed on
    the server, so the Buyers & Suppliers page and the Organic Certification
    page can never disagree."""
    expiry = (expiry or "").strip()
    if not expiry:
        return "none", None
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return "none", None
    days = (d - datetime.now().date()).days
    if days < 0:
        return "expired", days
    if days <= CERT_EXPIRING_DAYS:
        return "expiring", days
    return "current", days


def _valid_expiry(value):
    """A cert_expiry is blank or a real YYYY-MM-DD date."""
    value = (value or "").strip()
    if not value:
        return True
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _save_suppliers(data):
    """Persist the suppliers list to disk."""
    _save_json(SUPPLIERS_PATH, data)


@suppliers_bp.route("/api/suppliers", methods=["GET"])
@manager_required
def get_suppliers():
    """GET /api/suppliers - return all suppliers, each annotated (not stored)
    with cert_status / cert_days_left from its cert_expiry — see _cert_status."""
    out = []
    for sup in _load_suppliers():
        sup = dict(sup)
        sup["cert_status"], sup["cert_days_left"] = _cert_status(sup.get("cert_expiry"))
        out.append(sup)
    return jsonify(out)


@suppliers_bp.route("/api/suppliers", methods=["POST"])
@manager_required
def create_supplier():
    """POST /api/suppliers - create a supplier from the JSON body."""
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
    if not _valid_expiry(data.get("cert_expiry")):
        return jsonify({"error": "cert_expiry must be YYYY-MM-DD or blank"}), 400
    for field in ("contact_name","phone","email","address","website","certifications","notes",
                  "cert_expiry","cert_doc_id"):
        if field in data:
            supplier[field] = (data[field] or "").strip()
    suppliers.append(supplier)
    _save_suppliers(suppliers)
    return jsonify(supplier), 201


@suppliers_bp.route("/api/suppliers/<sid>", methods=["PUT"])
@manager_required
def update_supplier(sid):
    """PUT /api/suppliers/<id> - update an existing supplier."""
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
    if not _valid_expiry(data.get("cert_expiry")):
        return jsonify({"error": "cert_expiry must be YYYY-MM-DD or blank"}), 400
    for field in ("contact_name","phone","email","address","website","certifications","notes",
                  "cert_expiry","cert_doc_id"):
        if field in data:
            suppliers[idx][field] = (data[field] or "").strip()
    _save_suppliers(suppliers)
    return jsonify(suppliers[idx])


@suppliers_bp.route("/api/suppliers/<sid>", methods=["DELETE"])
@manager_required
def delete_supplier(sid):
    """DELETE /api/suppliers/<id> - remove a supplier."""
    suppliers = _load_suppliers()
    suppliers = [s for s in suppliers if s["id"] != sid]
    _save_suppliers(suppliers)
    return jsonify({"ok": True})


"""suppliers.py — Supplier management blueprint extracted from app.py.

First blueprint of the app.py split (CLAUDE.md "Pending architectural work").
Pure code-move: identical routes, URLs, and auth behaviour.

Follows the ripe_orders.py convention — a self-contained blueprint that defines
its own manager_required (so it never imports app.py at module load, avoiding a
circular import) and pulls its IO + path foundation from helpers.py.
"""
import os
import re
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, redirect, url_for, send_file
from werkzeug.utils import secure_filename

from helpers import _load_json, _save_json, INVENTORY_DIR, DATA_DIR

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
    """Load the suppliers list from disk, every record in the current shape
    (see _normalize_supplier)."""
    return [_normalize_supplier(s) for s in _load_json(SUPPLIERS_PATH, [])]


CERT_EXPIRING_DAYS = 60   # "expiring" window for a supplier certificate
CERT_RENEW_DAYS = 14      # "request renewal" window — raised on the Daily Summary
CERT_DIR = os.path.join(DATA_DIR, "supplier_certs")
CERT_MAX_BYTES = 15 * 1024 * 1024
SUPPLIER_TYPES = ("supplier", "distributor")
_CERT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_STATUS_RANK = {"expired": 0, "renew": 1, "expiring": 2, "none": 3, "current": 4}


def _cert_status(expiry, today=None):
    """Classify a certificate by its expiry date (YYYY-MM-DD or blank).
    Returns (status, days_left): 'none' (no date recorded), 'expired',
    'renew' (within CERT_RENEW_DAYS — time to ask for the new one),
    'expiring' (within CERT_EXPIRING_DAYS), or 'current'. One rule, computed on
    the server, so Buyers & Suppliers, Organic Certification and the Daily
    Summary can never disagree."""
    expiry = (expiry or "").strip()
    if not expiry:
        return "none", None
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        return "none", None
    days = (d - (today or datetime.now().date())).days
    if days < 0:
        return "expired", days
    if days <= CERT_RENEW_DAYS:
        return "renew", days
    if days <= CERT_EXPIRING_DAYS:
        return "expiring", days
    return "current", days


def _valid_expiry(value):
    """An expiry is blank or a real YYYY-MM-DD date."""
    value = (value or "").strip()
    if not value:
        return True
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _normalize_supplier(sup):
    """Bring a stored supplier to the current shape (in memory; persisted on
    the next save). Since 2026-09-23 a supplier carries `type` (supplier |
    distributor) and a `certificates` list:
        {id, name, source, expiry, file, filename, doc_id}
    `source` blank = the supplier's OWN certificate; set = the name of the
    upstream supplier a DISTRIBUTOR buys from, whose certificate it holds.
    `file` is a PDF uploaded here (CERT_DIR); `doc_id` is the older link to a
    /certifications document, kept readable. The legacy single-certificate
    fields (certifications text, cert_expiry, cert_doc_id) fold into one
    entry so nothing recorded before is lost."""
    sup = dict(sup)
    if sup.get("type") not in SUPPLIER_TYPES:
        sup["type"] = "supplier"
    if "certificates" not in sup:
        certs = []
        text = (sup.get("certifications") or "").strip()
        if text or sup.get("cert_expiry") or sup.get("cert_doc_id"):
            certs.append({
                "id": "legacy",
                "name": text or "Certificate",
                "source": "",
                "expiry": (sup.get("cert_expiry") or "").strip(),
                "doc_id": (sup.get("cert_doc_id") or "").strip(),
            })
        sup["certificates"] = certs
    for k in ("certifications", "cert_expiry", "cert_doc_id"):
        sup.pop(k, None)
    return sup


def _clean_certificates(raw, existing):
    """Validate the certificate list sent by the page. The page never sends
    `file`/`filename`/`doc_id` — those are carried over from the stored entry
    with the same id, so saving the form can never drop an uploaded PDF.
    Returns (certs, error)."""
    if not isinstance(raw, list):
        return None, "certificates must be a list"
    old = {c.get("id"): c for c in existing}
    out, seen = [], set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            return None, "bad certificate"
        cid = str(c.get("id") or "")
        if not _CERT_ID_RE.match(cid) or cid in seen:
            cid = datetime.now().strftime("%Y%m%d%H%M%S%f") + str(i)
        seen.add(cid)
        name = (c.get("name") or "").strip()
        expiry = (c.get("expiry") or "").strip()
        if not name:
            return None, "Every certificate needs a name"
        if not _valid_expiry(expiry):
            return None, "Certificate expiry must be a date"
        cert = {"id": cid, "name": name,
                "source": (c.get("source") or "").strip(), "expiry": expiry}
        prev = old.get(cid) or {}
        for k in ("file", "filename", "doc_id"):
            if prev.get(k):
                cert[k] = prev[k]
        out.append(cert)
    return out, None


def _remove_cert_files(certs):
    """Delete the uploaded PDFs of certificates that are going away."""
    for c in certs:
        if c.get("file"):
            try:
                os.remove(os.path.join(CERT_DIR, c["file"]))
            except OSError:
                pass


def _annotate(sup, today=None):
    """Add (not store) status to each certificate, and the supplier's WORST
    status as cert_status / cert_days_left — what a one-line summary shows."""
    sup = dict(sup)
    certs = []
    for c in sup.get("certificates", []):
        c = dict(c)
        c["status"], c["days_left"] = _cert_status(c.get("expiry"), today)
        certs.append(c)
    sup["certificates"] = certs
    worst = min(certs, key=lambda c: (_STATUS_RANK[c["status"]], c["days_left"] or 0), default=None)
    sup["cert_status"] = worst["status"] if worst else "none"
    sup["cert_days_left"] = worst["days_left"] if worst else None
    return sup


def renewals_due(today=None):
    """Certificates expired or within CERT_RENEW_DAYS of expiry, soonest
    first — the Daily Summary's "Request renewal" list. A certificate leaves
    the list once its expiry is moved forward (the renewal is on file)."""
    out = []
    for sup in _load_suppliers():
        for c in _annotate(sup, today)["certificates"]:
            if c["status"] in ("expired", "renew"):
                out.append({"supplier": sup.get("name", ""), "supplier_id": sup.get("id"),
                            "type": sup.get("type"), "certificate": c.get("name", ""),
                            "source": c.get("source", ""), "expiry": c.get("expiry", ""),
                            "days_left": c["days_left"], "status": c["status"]})
    out.sort(key=lambda r: r["days_left"])
    return out


def _save_suppliers(data):
    """Persist the suppliers list to disk."""
    _save_json(SUPPLIERS_PATH, data)


_TEXT_FIELDS = ("contact_name", "phone", "email", "address", "website", "notes")


def _apply_fields(supplier, data):
    """Copy the editable fields from a request body onto a supplier. Returns
    an error string or None."""
    if "type" in data:
        if data["type"] not in SUPPLIER_TYPES:
            return "type must be supplier or distributor"
        supplier["type"] = data["type"]
    if "certificates" in data:
        certs, err = _clean_certificates(data["certificates"], supplier.get("certificates", []))
        if err:
            return err
        kept = {c["id"] for c in certs}
        _remove_cert_files([c for c in supplier.get("certificates", []) if c.get("id") not in kept])
        supplier["certificates"] = certs
    for field in _TEXT_FIELDS:
        if field in data:
            supplier[field] = (data[field] or "").strip()
    return None


@suppliers_bp.route("/api/suppliers", methods=["GET"])
@manager_required
def get_suppliers():
    """GET /api/suppliers - return all suppliers, each certificate annotated
    (not stored) with status / days_left — see _cert_status."""
    return jsonify([_annotate(s) for s in _load_suppliers()])


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
        "type": "supplier",
        "ingredients": data.get("ingredients") or [],
        "certificates": [],
    }
    err = _apply_fields(supplier, data)
    if err:
        return jsonify({"error": err}), 400
    suppliers.append(supplier)
    _save_suppliers(suppliers)
    return jsonify(_annotate(supplier)), 201


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
    err = _apply_fields(suppliers[idx], data)
    if err:
        return jsonify({"error": err}), 400
    _save_suppliers(suppliers)
    return jsonify(_annotate(suppliers[idx]))


def _find_cert(sid, cid):
    """(suppliers, supplier, cert) or Nones."""
    suppliers = _load_suppliers()
    sup = next((s for s in suppliers if s["id"] == sid), None)
    cert = next((c for c in (sup or {}).get("certificates", []) if c.get("id") == cid), None)
    return suppliers, sup, cert


@suppliers_bp.route("/api/suppliers/<sid>/certificates/<cid>/file", methods=["POST"])
@manager_required
def upload_cert_file(sid, cid):
    """POST (multipart `file`) - attach a PDF copy to one certificate,
    replacing any earlier one."""
    suppliers, sup, cert = _find_cert(sid, cid)
    if not cert:
        return jsonify({"error": "Not found"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400
    blob = f.read(CERT_MAX_BYTES + 1)
    if len(blob) > CERT_MAX_BYTES:
        return jsonify({"error": "PDF is larger than 15 MB"}), 400
    if not blob.startswith(b"%PDF"):
        return jsonify({"error": "That file is not a PDF"}), 400
    os.makedirs(CERT_DIR, exist_ok=True)
    stored = f"{sid}_{cid}.pdf"
    tmp = os.path.join(CERT_DIR, stored + ".tmp")
    with open(tmp, "wb") as out:
        out.write(blob)
    os.replace(tmp, os.path.join(CERT_DIR, stored))
    cert["file"] = stored
    cert["filename"] = secure_filename(f.filename) or stored
    cert.pop("doc_id", None)
    _save_suppliers(suppliers)
    return jsonify(_annotate(sup))


@suppliers_bp.route("/api/suppliers/<sid>/certificates/<cid>/file", methods=["GET"])
@manager_required
def download_cert_file(sid, cid):
    """GET - open a certificate's PDF in the browser."""
    _, _, cert = _find_cert(sid, cid)
    if not cert or not cert.get("file"):
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(CERT_DIR, cert["file"])
    if not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404
    return send_file(path, mimetype="application/pdf",
                     download_name=cert.get("filename") or cert["file"])


@suppliers_bp.route("/api/suppliers/<sid>", methods=["DELETE"])
@manager_required
def delete_supplier(sid):
    """DELETE /api/suppliers/<id> - remove a supplier."""
    suppliers = _load_suppliers()
    for s in suppliers:
        if s["id"] == sid:
            _remove_cert_files(s.get("certificates", []))
    suppliers = [s for s in suppliers if s["id"] != sid]
    _save_suppliers(suppliers)
    return jsonify({"ok": True})


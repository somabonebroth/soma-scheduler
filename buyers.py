"""buyers.py — Buyer CRUD blueprint extracted from app.py.

Third step of the app.py split (CLAUDE.md "Pending architectural work").
Pure code-move: identical routes, URLs, and auth behaviour.

Scope is the 6 /api/buyers CRUD endpoints only. The shared buyer helpers
(_load_buyers, _save_buyers, _buyer_resolver, _all_sku_catalog, BUYERS_PATH)
deliberately STAY in app.py because analytics/sales/Ripe-internal code there
also calls them — moving them would force a bidirectional import. This
blueprint reaches them at request time via `import app` (the standard pattern
for the blueprint<->app circular case: the bare `import app` binds the
partially-initialised module at load and resolves attributes only when a
request actually runs, by which point app.py has finished importing).

Defines its own manager_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, redirect, url_for

import app

buyers_bp = Blueprint("buyers", __name__)


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


@buyers_bp.route("/api/buyers", methods=["GET"])
@manager_required
def get_buyers():
    """GET /api/buyers - return all buyer accounts."""
    return jsonify(app._load_buyers())


@buyers_bp.route("/api/buyers/sku-catalog", methods=["GET"])
@manager_required
def get_buyer_sku_catalog():
    """GET /api/buyers/sku-catalog - master SKU catalogue grouped by brand."""
    catalog = app._all_sku_catalog()
    groups = {}
    for sku in catalog:
        b = sku["brand"] or "No Brand"
        if b not in groups:
            groups[b] = []
        groups[b].append(sku)
    return jsonify([{"brand": b, "skus": groups[b]} for b in sorted(groups.keys())])


def _normalize_sku_pricing(sku):
    """Round and complete a buyer SKU's pricing triangle IN PLACE.

    price = cogs * (1 + margin_pct/100). Whichever two are present decide the
    third; price+cogs win, since those are the two a human actually types.

    This is the ONLY place the relationship is enforced. It used to live in the
    browser (buyer_edit.html recomputes margin in an input listener) with the
    server storing whatever it was handed — so an untouched row, which the edit
    page initialises with margin_pct=null, silently wiped the stored margin on
    every save. Deriving it here means the stored trio is coherent no matter
    what a client sends, including a stale tab or a future integration.
    """
    for pf in ("price", "cogs", "margin_pct"):
        if pf in sku and sku[pf] is not None:
            try:
                sku[pf] = round(float(sku[pf]), 2)
            except (TypeError, ValueError):
                sku[pf] = None

    price, cogs, margin = sku.get("price"), sku.get("cogs"), sku.get("margin_pct")
    if price is not None and cogs is not None:
        sku["margin_pct"] = round(((price / cogs) - 1) * 100, 2) if cogs > 0 else 0.0
    elif price is not None and margin is not None and (1 + margin / 100) > 0:
        sku["cogs"] = round(price / (1 + margin / 100), 2)
    elif cogs is not None and margin is not None:
        sku["price"] = round(cogs * (1 + margin / 100), 2)
    return sku


@buyers_bp.route("/api/buyers", methods=["POST"])
@manager_required
def create_buyer():
    """POST /api/buyers - create a buyer (optionally with assigned SKUs/pricing)."""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    buyers = app._load_buyers()
    if any(b["name"].lower() == name.lower() for b in buyers):
        return jsonify({"error": "Buyer already exists"}), 409
    buyer = {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "name": name}

    if "skus" in data:
        catalog_by_key = {s["sku_key"]: s for s in app._all_sku_catalog()}
        new_skus = []
        for sku in (data["skus"] or []):
            merged = dict(sku)
            cat = catalog_by_key.get(sku.get("sku_key", ""), {})
            for ident in ("brand", "format"):
                if not merged.get(ident) and cat.get(ident):
                    merged[ident] = cat[ident]
            _normalize_sku_pricing(merged)
            new_skus.append(merged)
        buyer["skus"] = new_skus
    else:
        sku_catalog = app._all_sku_catalog()
        buyer["skus"] = [s for s in sku_catalog if s["brand"].lower() == name.lower()]

    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            buyer[field] = (data[field] or "").strip()

    if "locations" in data and isinstance(data["locations"], list):
        buyer["locations"] = [
            {"id": l.get("id") or str(i),
             "name": (l.get("name") or "").strip(),
             "address": (l.get("address") or "").strip()}
            for i, l in enumerate(data["locations"])
            if (l.get("name") or "").strip()
        ]

    buyers.append(buyer)
    app._save_buyers(buyers)
    return jsonify(buyer), 201


@buyers_bp.route("/api/buyers/<bid>", methods=["PUT"])
@manager_required
def update_buyer(bid):
    """PUT /api/buyers/<bid> - update a buyer's profile, SKUs, and locations."""
    data = request.get_json(force=True) or {}
    buyers = app._load_buyers()
    idx = next((i for i, b in enumerate(buyers) if b["id"] == bid), None)
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if any(b["name"].lower() == name.lower() and b["id"] != bid for b in buyers):
            return jsonify({"error": "Name taken"}), 409
        buyers[idx]["name"] = name
    if "skus" in data:
        # Preserve pricing fields if they already exist and incoming data
        # doesn't explicitly include them (allows partial SKU updates).
        # Backfill brand/format from the master catalog when missing — the
        # buyer-edit JS doesn't send those fields, so newly-assigned SKUs
        # would otherwise arrive empty and land in Ripe's "Other" bucket.
        existing_by_key = {s.get("sku_key",""): s for s in (buyers[idx].get("skus") or [])}
        catalog_by_key  = {s["sku_key"]: s for s in app._all_sku_catalog()}
        new_skus = []
        for sku in data["skus"]:
            key = sku.get("sku_key", "")
            existing = existing_by_key.get(key, {})
            merged = dict(existing)
            merged.update(sku)
            cat = catalog_by_key.get(key, {})
            for ident in ("brand", "format"):
                if not merged.get(ident) and cat.get(ident):
                    merged[ident] = cat[ident]
            _normalize_sku_pricing(merged)
            new_skus.append(merged)
        buyers[idx]["skus"] = new_skus
    for field in ("contact_name","phone","email","address","website","certifications","notes"):
        if field in data:
            buyers[idx][field] = (data[field] or "").strip()
    if "locations" in data:
        locs = data["locations"]
        if isinstance(locs, list):
            buyers[idx]["locations"] = [
                {"id": l.get("id") or str(i),
                 "name": (l.get("name") or "").strip(),
                 "address": (l.get("address") or "").strip()}
                for i, l in enumerate(locs)
                if (l.get("name") or "").strip()
            ]
    app._save_buyers(buyers)
    return jsonify(buyers[idx])


@buyers_bp.route("/api/buyers/<bid>", methods=["DELETE"])
@manager_required
def delete_buyer(bid):
    """DELETE /api/buyers/<bid> - remove a buyer."""
    app._save_buyers([b for b in app._load_buyers() if b["id"] != bid])
    return jsonify({"ok": True})

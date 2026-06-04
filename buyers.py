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

Defines its own login_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, session, redirect, url_for

import app

buyers_bp = Blueprint("buyers", __name__)


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


@buyers_bp.route("/api/buyers", methods=["GET"])
@login_required
def get_buyers():
    """GET /api/buyers - return all buyer accounts."""
    return jsonify(app._load_buyers())


@buyers_bp.route("/api/buyers/sku-catalog", methods=["GET"])
@login_required
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


@buyers_bp.route("/api/buyers", methods=["POST"])
@login_required
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
            for pf in ("price", "cogs", "margin_pct"):
                if pf in merged and merged[pf] is not None:
                    try:
                        merged[pf] = round(float(merged[pf]), 2)
                    except (TypeError, ValueError):
                        merged[pf] = None
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
@login_required
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
            for pf in ("price", "cogs", "margin_pct"):
                if pf in merged and merged[pf] is not None:
                    try:
                        merged[pf] = round(float(merged[pf]), 2)
                    except (TypeError, ValueError):
                        merged[pf] = None
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


@buyers_bp.route("/api/buyers/<bid>/skus/<path:sku_key>/pricing", methods=["PATCH"])
@login_required
def update_buyer_sku_pricing(bid, sku_key):
    """Update pricing for a single SKU on a buyer.
    Body: { price, cogs, margin_pct, buyer_sku, active }
    Computes missing values using the price=cogs*(1+margin/100) relationship.
    """
    data = request.get_json() or {}
    buyers = app._load_buyers()
    idx = next((i for i, b in enumerate(buyers) if b["id"] == bid), None)
    if idx is None:
        return jsonify({"error": "Buyer not found"}), 404

    skus = buyers[idx].get("skus") or []
    sku_idx = next((i for i, s in enumerate(skus) if s.get("sku_key") == sku_key), None)
    if sku_idx is None:
        return jsonify({"error": "SKU not assigned to this buyer"}), 404

    sku = dict(skus[sku_idx])

    # Derive pricing triangle: price = cogs * (1 + margin/100)
    price      = float(data["price"])      if "price"      in data and data["price"]      is not None else sku.get("price")
    cogs       = float(data["cogs"])       if "cogs"       in data and data["cogs"]       is not None else sku.get("cogs")
    margin_pct = float(data["margin_pct"]) if "margin_pct" in data and data["margin_pct"] is not None else sku.get("margin_pct")

    if price is not None and cogs is not None:
        margin_pct = round(((price / cogs) - 1) * 100, 2) if cogs > 0 else 0.0
    elif price is not None and margin_pct is not None:
        cogs = round(price / (1 + margin_pct / 100), 2) if (1 + margin_pct / 100) > 0 else price
    elif cogs is not None and margin_pct is not None:
        price = round(cogs * (1 + margin_pct / 100), 2)

    if price      is not None: sku["price"]      = round(price, 2)
    if cogs       is not None: sku["cogs"]        = round(cogs, 2)
    if margin_pct is not None: sku["margin_pct"]  = round(margin_pct, 2)

    if "buyer_sku" in data:
        sku["buyer_sku"] = (data["buyer_sku"] or "").strip()
    if "active" in data:
        sku["active"] = bool(data["active"])

    skus[sku_idx] = sku
    buyers[idx]["skus"] = skus
    app._save_buyers(buyers)
    return jsonify({"ok": True, "sku": sku})


@buyers_bp.route("/api/buyers/<bid>", methods=["DELETE"])
@login_required
def delete_buyer(bid):
    """DELETE /api/buyers/<bid> - remove a buyer."""
    app._save_buyers([b for b in app._load_buyers() if b["id"] != bid])
    return jsonify({"ok": True})

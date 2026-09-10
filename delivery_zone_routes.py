"""Delivery Zone Lookup — the thin Flask layer over ``delivery_zones``.

Internal quoting tool (added 2026-09-10): type a wholesale customer's postal
code, optionally a case count, and read off the zone, minimum order and fee.
It has NO pathways in or out — nothing reads it, no order writes it. The SBBC
portal's zones.json is a separate, retail-facing table and stays untouched.

Routes (manager + FOH — the people who quote; production is locked out):
  GET /delivery-zones                     the page
  GET /api/delivery-zones/lookup?postal=&cases=
  GET /api/delivery-zones                 zone reference (terms + patterns)
"""

from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

import delivery_zones as dz

delivery_zones_bp = Blueprint("delivery_zones", __name__)


def foh_required(f):
    """Local copy pattern (see end_of_day.foh_required): manager + foh may act,
    production may not."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        if (session.get("role") or "manager") not in ("manager", "foh"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "FOH access required"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


@delivery_zones_bp.route("/delivery-zones")
@foh_required
def delivery_zones_page():
    """The lookup page. Zone terms are rendered server-side so the reference
    table is on screen before any JS runs; lookups go through the API."""
    try:
        summary = dz.zone_summary()
        config_error = None
    except dz.ZoneConfigError as e:
        summary = {"store": {}, "case_size": 12, "fallback_zone": None, "zones": []}
        config_error = str(e)
    return render_template("delivery_zones.html", summary=summary, config_error=config_error)


@delivery_zones_bp.route("/api/delivery-zones")
@foh_required
def api_delivery_zones():
    """Zone reference: every zone's terms and pattern list."""
    try:
        return jsonify(dz.zone_summary())
    except dz.ZoneConfigError as e:
        return jsonify({"error": f"Zone table unavailable: {e}"}), 503


@delivery_zones_bp.route("/api/delivery-zones/lookup")
@foh_required
def api_delivery_zone_lookup():
    """?postal=M5J2M2[&cases=6] → the quote dict from delivery_zones.quote().
    400 on a malformed postal code or case count, 503 if the zone file is broken."""
    postal = request.args.get("postal", "")
    raw_cases = (request.args.get("cases") or "").strip()
    cases = None
    if raw_cases:
        try:
            cases = int(raw_cases)
            if cases < 0:
                raise ValueError
        except ValueError:
            return jsonify({"error": "Case quantity must be a whole number of 0 or more."}), 400
    try:
        return jsonify(dz.quote(postal, cases))
    except dz.InvalidPostalCode as e:
        return jsonify({"error": str(e)}), 400
    except dz.ZoneConfigError as e:
        return jsonify({"error": f"Zone table unavailable: {e}"}), 503

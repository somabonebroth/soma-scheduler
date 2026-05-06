"""
ripe_orders.py — Soma × Ripe order integration
================================================
Adds to the Soma production app:
  - /ripe-orders          order management list (Soma view)
  - /api/ripe-orders      GET list / PATCH status

Status flow:
  pending → approve → approved
  approved → approve-for-production → approved-for-production  (+FIFO FG deduction)
  pending|approved → decline → declined
  approved|approved-for-production → fulfill → fulfilled

On approve-for-production, FIFO FG units are deducted from Soma's
finished_goods.json for each line item in the order (matched by SKU).

Communication with Ripe portal:
  HTTP PATCH /api/internal/orders/<id>  (X-Internal-Key header)
  GET        /api/internal/orders        to sync

Environment variables (set on Soma's Render service):
  RIPE_PORTAL_URL   — e.g. https://ripe-portal.onrender.com
  INTERNAL_API_KEY  — same value set on both services
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

import urllib.request
import urllib.error

from flask import Blueprint, render_template, request, jsonify, session

logger = logging.getLogger(__name__)

ripe_orders_bp = Blueprint("ripe_orders", __name__)

# ── Config ────────────────────────────────────────────────────────────────────
RIPE_PORTAL_URL = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# Soma FG data path (same as organic.py)
_INVENTORY_DIR = None
_FG_PATH = None
_SALES_PATH = None


def init_paths(inventory_dir):
    """Called from app.py after INVENTORY_DIR is known."""
    global _INVENTORY_DIR, _FG_PATH, _SALES_PATH
    _INVENTORY_DIR = inventory_dir
    _FG_PATH = os.path.join(inventory_dir, "finished_goods.json")
    _SALES_PATH = os.path.join(inventory_dir, "sales.json")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _configured():
    return bool(RIPE_PORTAL_URL and INTERNAL_API_KEY)


def _ripe_request(method, path, body=None):
    """Make an authenticated request to the Ripe portal internal API.
    Returns (status_code, dict_or_None).
    """
    if not _configured():
        return 503, {"error": "Ripe portal not configured. Set RIPE_PORTAL_URL and INTERNAL_API_KEY."}
    url = f"{RIPE_PORTAL_URL}{path}"
    headers = {
        "X-Internal-Key": INTERNAL_API_KEY,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode()
            return e.code, json.loads(body_text)
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        logger.exception("Ripe API request failed: %s %s", method, url)
        return 503, {"error": str(e)}


def _load_fg():
    if not _FG_PATH or not os.path.exists(_FG_PATH):
        return []
    with open(_FG_PATH) as f:
        return json.load(f)


def _save_fg(fg):
    if not _FG_PATH:
        return
    tmp = _FG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(fg, f, indent=2)
    os.replace(tmp, _FG_PATH)


def _load_sales():
    if not _SALES_PATH or not os.path.exists(_SALES_PATH):
        return []
    with open(_SALES_PATH) as f:
        return json.load(f)


def _save_sales(sales):
    if not _SALES_PATH:
        return
    tmp = _SALES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sales, f, indent=2)
    os.replace(tmp, _SALES_PATH)


def _sku_key(brand, recipe, fmt):
    """Canonical SKU key matching Soma's organic module."""
    return "|".join([
        (brand or "").strip().lower(),
        (recipe or "").strip().lower(),
        (fmt or "").strip().upper(),
    ])


def _fifo_deduct_order(order):
    """FIFO-deduct FG inventory for each line item in the order.

    Matches items by SKU (name + format). Records a sale entry per item.
    Returns (success: bool, errors: list[str]).
    """
    fg = _load_fg()
    sales = _load_sales()
    errors = []
    sale_records = []

    for item in order.get("items", []):
        product_name = item.get("name", "")
        fmt = (item.get("format") or "").upper()
        units_to_deduct = int(item.get("units", 0))

        if units_to_deduct <= 0:
            continue

        # Match FG entries: name match (recipe field) + format match
        # Soma stores recipe names like "Beef Basil Rose FZ-750ML"; try exact then partial
        candidates = []
        for entry in fg:
            entry_recipe = (entry.get("recipe") or "").strip()
            entry_fmt = (entry.get("format") or "").strip().upper()
            entry_remaining = int(entry.get("quantity_remaining") or 0)
            if entry_remaining <= 0:
                continue
            # Match: entry recipe contains the product name OR vice versa, and format matches
            name_match = (
                product_name.lower() in entry_recipe.lower()
                or entry_recipe.lower() in product_name.lower()
            )
            fmt_match = not fmt or entry_fmt.startswith(fmt.split("-")[0])
            if name_match and fmt_match:
                candidates.append(entry)

        if not candidates:
            errors.append(f"No FG inventory found for: {product_name} ({fmt})")
            continue

        # Sort FIFO: oldest production date first
        def _prod_date(e):
            wid = e.get("week_id")
            d_idx = e.get("day_idx")
            if wid and d_idx is not None:
                try:
                    return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return (e.get("created_at") or "")[:10]

        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot", ""), e.get("id", "")))

        total_available = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
        if units_to_deduct > total_available:
            errors.append(
                f"Insufficient FG stock for {product_name} ({fmt}): "
                f"need {units_to_deduct} units, have {total_available}"
            )
            continue

        # Deduct FIFO
        remaining_to_take = units_to_deduct
        lot_summary = {}
        for entry in candidates:
            if remaining_to_take <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            take = min(avail, remaining_to_take)
            entry["quantity_remaining"] = avail - take
            remaining_to_take -= take
            lot = entry.get("lot", "")
            if lot not in lot_summary:
                lot_summary[lot] = {"lot": lot, "quantity": 0, "fg_ids": [], "breakdown": []}
            lot_summary[lot]["quantity"] += take
            lot_summary[lot]["fg_ids"].append(entry["id"])
            lot_summary[lot]["breakdown"].append({"fg_id": entry["id"], "quantity": take})

        first = candidates[0]
        sale_records.append({
            "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales) + len(sale_records)),
            "sku_key": _sku_key(first.get("brand", ""), first.get("recipe", ""), first.get("format", "")),
            "brand": first.get("brand", ""),
            "recipe": first.get("recipe", ""),
            "format": first.get("format", ""),
            "quantity": units_to_deduct,
            "lots": list(lot_summary.values()),
            "fg_lot": list(lot_summary.keys())[0] if len(lot_summary) == 1 else "",
            "fg_id": "",
            "buyer": "Ripe",
            "sale_date": datetime.now(timezone.utc).date().isoformat(),
            "case_lot": order.get("id", ""),
            "po_number": order.get("id", ""),
            "ripe_order_id": order.get("id", ""),
            "created_at": datetime.now().isoformat(),
        })

    if errors:
        return False, errors

    # Persist only if no errors
    _save_fg(fg)
    sales.extend(sale_records)
    _save_sales(sales)
    return True, []


# ── Routes ────────────────────────────────────────────────────────────────────

def _soma_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


@ripe_orders_bp.route("/ripe-orders")
@_soma_login_required
def ripe_orders_page():
    status, data = _ripe_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    pending_count = sum(1 for o in orders if o.get("status") == "pending")
    configured = _configured()
    error = None if status == 200 else data.get("error") if isinstance(data, dict) else "Unknown error"
    return render_template(
        "ripe_orders.html",
        orders=orders,
        pending_count=pending_count,
        configured=configured,
        error=error,
    )


@ripe_orders_bp.route("/api/ripe-orders/pending-count")
@_soma_login_required
def ripe_pending_count():
    if not _configured():
        return jsonify({"count": 0, "configured": False})
    status, data = _ripe_request("GET", "/api/internal/orders")
    if status != 200 or not isinstance(data, list):
        return jsonify({"count": 0, "configured": True})
    count = sum(1 for o in data if o.get("status") == "pending")
    return jsonify({"count": count, "configured": True})


@ripe_orders_bp.route("/api/ripe-orders/<order_id>", methods=["PATCH"])
@_soma_login_required
def ripe_order_action(order_id):
    body = request.get_json() or {}
    action = body.get("action")

    if action == "approve-for-production":
        # 1. Get the order first to know what to deduct
        get_status, order_data = _ripe_request("GET", "/api/internal/orders")
        if get_status != 200 or not isinstance(order_data, list):
            return jsonify({"error": "Could not fetch orders from Ripe portal"}), 502
        order = next((o for o in order_data if o["id"] == order_id), None)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order.get("status") != "approved":
            return jsonify({"error": f"Order must be approved first (currently {order.get('status')})"}), 409

        # 2. FIFO FG deduction
        ok, errors = _fifo_deduct_order(order)
        if not ok:
            return jsonify({"error": "FG deduction failed", "details": errors}), 422

        # 3. Push status to Ripe portal
        status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}", {"action": "approve-for-production"})
        if status != 200:
            # FG already deducted — note this in response but don't fail silently
            return jsonify({
                "error": "FG deducted but Ripe status update failed",
                "ripe_error": resp.get("error") if isinstance(resp, dict) else str(resp),
            }), 502
        return jsonify({"ok": True, "order": resp.get("order")})

    # All other actions proxy directly to Ripe
    status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}", body)
    return jsonify(resp), status

"""
ripe_orders.py — Soma × Ripe order integration

Status flow pushed to Ripe portal via internal API:
  pending → approve (+ delivery_date) → approved
  approved → decline → declined
  approved → fulfill → fulfilled

On approve: _create_ripe_sale_records() writes a scheduled sale record per
line item (deducted=False, payment_pending per payment method).
app.py._run_scheduled_deductions() runs on startup and via API to process
records whose deduction_date has arrived.

Env vars on Soma's Render service:
  RIPE_PORTAL_URL   — e.g. https://ripe-portal.onrender.com
  INTERNAL_API_KEY  — same value on both Render services
"""

import os, json, logging
from datetime import datetime, timezone
from functools import wraps
import urllib.request, urllib.error

from flask import Blueprint, render_template, request, jsonify, session

logger = logging.getLogger(__name__)
ripe_orders_bp = Blueprint("ripe_orders", __name__)

RIPE_PORTAL_URL = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# Paths set by init_paths() once app.py knows INVENTORY_DIR
_FG_PATH = None
_SALES_PATH = None


def init_paths(inventory_dir):
    global _FG_PATH, _SALES_PATH
    _FG_PATH = os.path.join(inventory_dir, "finished_goods.json")
    _SALES_PATH = os.path.join(inventory_dir, "sales.json")


def _configured():
    return bool(RIPE_PORTAL_URL and INTERNAL_API_KEY)


def _ripe_request(method, path, body=None):
    if not _configured():
        return 503, {"error": "Ripe portal not configured. Set RIPE_PORTAL_URL and INTERNAL_API_KEY."}
    url = f"{RIPE_PORTAL_URL}{path}"
    headers = {"X-Internal-Key": INTERNAL_API_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        logger.exception("Ripe API %s %s failed", method, url)
        return 503, {"error": str(e)}


def _load(path, default):
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else []


def _save(path, data):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def create_ripe_sale_records(order, delivery_date, payment_key):
    """Write scheduled sale records for each line item in a Ripe order.
    Called on approval. FG is NOT deducted here — deduction_date controls when.
    Returns (ok: bool, error: str|None).
    """
    sales = _load(_SALES_PATH, [])
    fg_all = _load(_FG_PATH, [])
    payment_pending = (payment_key == "cc_net14")
    created = []

    for item in order.get("items", []):
        product_name = (item.get("name") or "").strip()
        fmt = (item.get("format") or "").upper()
        units = int(item.get("units") or 0)
        if units <= 0:
            continue

        # Match a FG entry to get canonical recipe/brand/format/cert
        match = next((
            f for f in fg_all
            if product_name.lower() in (f.get("recipe") or "").lower()
            and (f.get("format") or "").upper().startswith(fmt.split("-")[0])
        ), None)

        recipe_name = match.get("recipe", product_name) if match else product_name
        brand = match.get("brand", "") if match else ""
        recipe_fmt = match.get("format", fmt) if match else fmt
        cert = match.get("certification", "") if match else ""
        sku = "|".join([brand.strip().lower(), recipe_name.strip().lower(), recipe_fmt.strip().upper()])

        sale = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f") + str(len(sales) + len(created)),
            "sku_key": sku,
            "brand": brand,
            "recipe": recipe_name,
            "format": recipe_fmt,
            "certification": cert,
            "quantity": units,
            "lots": [],
            "fg_lot": "",
            "fg_id": "",
            "buyer": "Ripe",
            "sale_date": delivery_date,
            "deduction_date": delivery_date,
            "deducted": False,
            "payment_pending": payment_pending,
            "payment_method": payment_key,
            "ripe_order_id": order.get("id", ""),
            "delivery_label": order.get("delivery_label", ""),
            "po_number": order.get("id", ""),
            "case_lot": "",
            "created_at": datetime.now().isoformat(),
        }
        created.append(sale)

    sales.extend(created)
    _save(_SALES_PATH, sales)
    return True, None


def _soma_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

@ripe_orders_bp.route("/ripe-orders")
@_soma_login_required
def ripe_orders_page():
    status, data = _ripe_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    pending_count = sum(1 for o in orders if o.get("status") == "pending")
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Unknown error")
    return render_template("ripe_orders.html", orders=orders,
        pending_count=pending_count, configured=configured, error=error)


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
    """
    approve  — requires delivery_date in body. Creates sale records, then
               pushes approve to Ripe portal (fires Stripe invoice for Net14).
    decline  — proxies to Ripe, voids invoice if pending.
    fulfill  — proxies to Ripe, records actual fulfillment date.
    """
    body = request.get_json() or {}
    action = body.get("action")

    if action == "approve":
        delivery_date = (body.get("delivery_date") or "").strip()
        if not delivery_date:
            return jsonify({"error": "delivery_date is required to approve"}), 400

        # Fetch the order from Ripe
        get_status, order_data = _ripe_request("GET", "/api/internal/orders")
        if get_status != 200 or not isinstance(order_data, list):
            return jsonify({"error": "Could not fetch orders from Ripe portal"}), 502
        order = next((o for o in order_data if o["id"] == order_id), None)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order.get("status") != "pending":
            return jsonify({"error": f"Order is already {order.get('status')}"}), 409

        payment_key = order.get("payment_key", "etransfer")

        # Write scheduled sale records in Soma
        ok, err = create_ripe_sale_records(order, delivery_date, payment_key)
        if not ok:
            return jsonify({"error": err or "Could not create sale records"}), 500

        # Run deductions in case delivery date is today or past
        try:
            from app import _run_scheduled_deductions
            _run_scheduled_deductions()
        except Exception:
            pass  # non-fatal — will run on next startup

        # Push approve to Ripe (fires Stripe invoice for Net14 there)
        ripe_status, ripe_resp = _ripe_request(
            "PATCH", f"/api/internal/orders/{order_id}",
            {"action": "approve", "fulfillment_date": delivery_date},
        )
        if ripe_status != 200:
            return jsonify({
                "warning": "Sale records created but Ripe status update failed",
                "ripe_error": ripe_resp.get("error") if isinstance(ripe_resp, dict) else str(ripe_resp),
            }), 502

        return jsonify({"ok": True, "payment_pending": payment_key == "cc_net14"})

    if action in ("decline", "fulfill"):
        status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}", body)
        return jsonify(resp), status

    return jsonify({"error": f"Unknown action: {action}"}), 400

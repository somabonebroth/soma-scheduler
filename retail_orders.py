"""
retail_orders.py — Soma × SBBC Wholesale Portal order integration

Mirrors ripe_orders.py (which is NOT touched — it shares a live contract with
the Ripe portal) for the SBBC retail portal. Key differences from Ripe:

  * Orders arrive ALREADY PAID (Stripe Checkout at portal checkout), so approve
    has no invoice/payment step — it deducts FG + writes sale records, then
    pushes the status to the portal.
  * Decline triggers a full Stripe REFUND on the portal side (the portal owns
    the refund; Soma just proxies the action).
  * Items are retail units carrying sku_key directly (no name-matching), and
    there are no wholesale business rules (SS minimums / FZ-BB lead times).
  * NO auto-approve — every order is reviewed by hand.

Status flow pushed to the portal via its internal API:
  pending → approve (+ ship date) → approved → fulfill → fulfilled
  pending|approved → decline → declined (+ Stripe refund)
  (fulfill = courier-CONFIRMED delivery, portal commit 0c22e5c — buyers see
  "Delivered ✓ <date>"; Soma's UI labels it "Mark Delivered")

Env vars on Soma's Render service:
  RETAIL_PORTAL_URL — e.g. https://sbbc-wholesale-portalinternal-api-key.onrender.com
  INTERNAL_API_KEY  — same value on both Render services
"""

import os, json, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
import urllib.request, urllib.error

from flask import Blueprint, render_template, request, jsonify, session, redirect

from helpers import _sku_key, _load_company_info

logger = logging.getLogger(__name__)
retail_orders_bp = Blueprint("retail_orders", __name__)

RETAIL_PORTAL_URL = os.environ.get("RETAIL_PORTAL_URL", "").rstrip("/")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# Paths set by init_paths() once app.py knows INVENTORY_DIR
_FG_PATH = None
_SALES_PATH = None


def init_paths(inventory_dir):
    """Bind the finished-goods + sales JSON paths once app.py knows INVENTORY_DIR."""
    global _FG_PATH, _SALES_PATH
    _FG_PATH = os.path.join(inventory_dir, "finished_goods.json")
    _SALES_PATH = os.path.join(inventory_dir, "sales.json")


def _configured():
    """True when both RETAIL_PORTAL_URL and INTERNAL_API_KEY are set."""
    return bool(RETAIL_PORTAL_URL and INTERNAL_API_KEY)


def _retail_request(method, path, body=None):
    """Make an authenticated internal API call to the SBBC portal.

    Returns (status_code, parsed_json). Yields 503 when unconfigured or on a
    transport-level failure; HTTP error responses pass through with their code.
    """
    if not _configured():
        return 503, {"error": "SBBC portal not configured. Set RETAIL_PORTAL_URL and INTERNAL_API_KEY."}
    url = f"{RETAIL_PORTAL_URL}{path}"
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
        logger.exception("SBBC portal API %s %s failed", method, url)
        return 503, {"error": str(e)}


def _load(path, default):
    """Read a JSON file, returning default (or [] when default is None) if missing."""
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else []


def _save(path, data):
    """Atomically write data as JSON (.tmp then os.replace); no-op if path is unset."""
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def create_retail_sale_records(order, ship_date):
    """Write a sale record per line item and immediately FIFO-deduct FG inventory.

    The order was already paid at portal checkout, so approval = commit the
    stock. All-or-nothing: every line is deducted in memory first and nothing
    is saved unless the whole order fits — a paid multi-item order must never
    half-deduct. Returns (ok: bool, error: str|None).
    """
    from sales import _deduct_fifo

    sales = _load(_SALES_PATH, [])
    fg_all = _load(_FG_PATH, [])

    # Idempotency guard (keyed on the portal order id). FG is deducted + sales
    # written BEFORE the status push to the portal; if that push fails the
    # caller returns 502 with inventory already moved and the order still
    # "pending" on the portal — a retry would otherwise deduct a SECOND time.
    # If sale records already exist for this order, skip the deduction and let
    # the caller re-push the status. (Same shape as the Ripe guard.)
    order_id = order.get("id", "")
    if order_id and any(s.get("retail_order_id") == order_id for s in sales):
        logger.warning(
            "Retail order %s already has sale records — skipping duplicate FG deduction",
            order_id,
        )
        return True, None

    # Two-tier organic boundary guard: organic-certified SKUs are lot-tracked
    # and wholesale-manual-only (the packer records the REAL lots shipped via
    # the Record Sale modal). FIFO-deducting one here would silently corrupt
    # its lot accuracy, so refuse — record the sale manually instead.
    organic_lines = []
    for item in order.get("items", []):
        sk = (item.get("sku_key") or "").strip()
        if not sk:
            continue
        rep = next((f for f in fg_all
                    if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sk
                    and (f.get("certification") or "") == "Organic"), None)
        if rep is not None:
            organic_lines.append(item.get("name") or sk)
    if organic_lines:
        return False, (
            "Cannot auto-approve: "
            + ", ".join(organic_lines)
            + " is organic-certified (lot-tracked). Organic SKUs must be sold "
            "via Manage Inventory → Record Sale with a packer lot allocation."
        )

    delivery = order.get("delivery") or {}
    address = ", ".join(x for x in (delivery.get("address"), delivery.get("postal")) if x)
    now_iso = datetime.now().isoformat()
    created = []
    shortfalls = []

    for item in order.get("items", []):
        sk = (item.get("sku_key") or "").strip()
        try:
            qty = int(item.get("qty") or 0)
        except (ValueError, TypeError):
            qty = 0
        if not sk or qty <= 0:
            continue

        # FIFO-deduct in memory (mutates fg_all). Insufficient stock raises —
        # collect every shortfall so the error names all problem lines at once.
        try:
            sale_lots, brand, recipe, fmt = _deduct_fifo(fg_all, sk, qty)
        except ValueError as e:
            shortfalls.append(f"{item.get('name') or sk}: {e}")
            continue

        # Certification snapshot from the entries actually deducted
        cert = ""
        for lot_entry in sale_lots:
            for b in (lot_entry.get("breakdown") or []):
                target = next((f for f in fg_all if f.get("id") == b.get("fg_id")), None)
                if target and target.get("certification"):
                    cert = target["certification"]
                    break
            if cert:
                break

        created.append({
            "id":             datetime.now().strftime("%Y%m%d%H%M%S%f") + str(len(sales) + len(created)),
            "sku_key":        sk,
            "brand":          brand,
            "recipe":         recipe,
            "format":         fmt,
            "certification":  cert,
            "quantity":       qty,
            "lots":           sale_lots,
            "fg_lot":         sale_lots[0]["lot"] if len(sale_lots) == 1 else "",
            "fg_id":          sale_lots[0]["fg_ids"][0] if len(sale_lots) == 1 and sale_lots[0]["fg_ids"] else "",
            "buyer":          order.get("buyer", ""),
            "sale_date":      ship_date,
            "deduction_date": ship_date,
            "deducted":       True,               # immediate — stock committed on approval
            "deducted_at":    now_iso,
            "payment_pending":  False,            # paid at portal checkout
            "payment_method":   "stripe_checkout",
            "retail_order_id":  order_id,
            "delivery_label":   f"Mail — {order.get('delivery_zone', '')}".rstrip(" —"),
            "location_name":    delivery.get("contact_name", ""),
            "location_address": address,
            "po_number":        order_id,
            "case_lot":         "",
            "unit_price":       float(item.get("unit_price") or 0),
            "line_total":       float(item.get("line_total") or 0),
            "created_at":       now_iso,
        })

    if shortfalls:
        # Nothing was saved — the in-memory fg_all mutations are discarded.
        return False, (
            "Insufficient stock to approve — nothing was deducted. "
            + "; ".join(shortfalls)
        )
    if not created:
        return False, "Order has no chargeable line items."

    sales.extend(created)
    _save(_SALES_PATH, sales)
    _save(_FG_PATH, fg_all)   # save FG with deducted quantity_remaining values
    return True, None


def _soma_manager_required(f):
    """Decorator: require an authenticated Soma session with the manager or
    FOH role. Buyer-portal order handling moved off the HOO's desk to front
    of house (2026-08-26 three-role split; full actions by Jeremy's call) —
    the production tablet stays locked out. Sessions from before roles
    existed count as manager (see app.current_role)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        if (session.get("role") or "manager") not in ("manager", "foh"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Manager access required"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

def _month_label(key):
    """Render a 'YYYY-MM' key as 'June 2026'. Falls back to the raw key."""
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return key or "Undated"


def _group_orders_by_month(orders):
    """Group orders (already sorted newest-first) into month buckets, newest
    month first. The current Toronto month is flagged expanded; past months
    collapse."""
    current_month = datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m")
    months, seen = [], {}
    for o in orders:
        key = (o.get("created_at") or "")[:7] or "unknown"
        grp = seen.get(key)
        if grp is None:
            grp = {"key": key, "label": _month_label(key),
                   "expanded": key == current_month, "orders": []}
            seen[key] = grp
            months.append(grp)
        grp["orders"].append(o)
    return months


@retail_orders_bp.route("/retail-orders")
@_soma_manager_required
def retail_orders_page():
    """Render the SBBC orders page: pending orders pinned, settled grouped by month."""
    status, data = _retail_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    pending_orders = [o for o in orders if o.get("status") == "pending"]
    settled_orders = [o for o in orders if o.get("status") != "pending"]
    settled_months = _group_orders_by_month(settled_orders)
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Unknown error")
    return render_template("retail_orders.html",
        pending_orders=pending_orders, settled_months=settled_months,
        pending_count=len(pending_orders), configured=configured, error=error)


@retail_orders_bp.route("/api/retail-orders/pending-count")
@_soma_manager_required
def retail_pending_count():
    """Return counts for the dashboard badges: new (pending) and in-progress
    (approved but not yet delivered) SBBC orders. Both 0 when unconfigured."""
    if not _configured():
        return jsonify({"count": 0, "in_progress": 0, "configured": False})
    status, data = _retail_request("GET", "/api/internal/orders")
    if status != 200 or not isinstance(data, list):
        return jsonify({"count": 0, "in_progress": 0, "configured": True})
    count = sum(1 for o in data if o.get("status") == "pending")
    in_progress = sum(1 for o in data if o.get("status") == "approved")
    return jsonify({"count": count, "in_progress": in_progress, "configured": True})


@retail_orders_bp.route("/retail-orders/<order_id>/packing-slip")
@_soma_manager_required
def retail_packing_slip(order_id):
    """Fetch order detail from the SBBC portal and render a packing slip.

    No pricing anywhere on the slip. Orders from Alma Care get an extra
    "Special Ingredient Package" line — an item they supply for us to include
    in every box alongside the broth.
    """
    status, data = _retail_request("GET", f"/api/internal/order-detail/{order_id}")
    if status != 200 or not isinstance(data, dict) or not data.get("id"):
        from flask import abort
        abort(404)
    buyer = (data.get("buyer") or "").strip().lower()
    return render_template(
        "retail_packing_slip.html",
        order=data,
        company=_load_company_info(),
        today=datetime.now().strftime("%B %d, %Y"),
        alma_extra="alma care" in buyer,
    )


@retail_orders_bp.route("/api/retail-orders/<order_id>", methods=["PATCH"])
@_soma_manager_required
def retail_order_action(order_id):
    """
    approve — requires ship_date in body. Deducts FG + writes sale records,
              then pushes approve to the portal.
    decline — proxies to the portal, which issues a full Stripe REFUND.
    fulfill — proxies to the portal, marking courier-confirmed DELIVERY
              (optional fulfillment_date in body; portal defaults to today).
              Portal returns 409 unless the order is approved.
    """
    body = request.get_json() or {}
    action = body.get("action")

    if action == "approve":
        ship_date = (body.get("ship_date") or "").strip()
        if not ship_date:
            return jsonify({"error": "ship_date is required to approve"}), 400

        get_status, order_data = _retail_request("GET", "/api/internal/orders")
        if get_status != 200 or not isinstance(order_data, list):
            return jsonify({"error": "Could not fetch orders from the SBBC portal"}), 502
        order = next((o for o in order_data if o.get("id") == order_id), None)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order.get("status") != "pending":
            return jsonify({"error": f"Order is already {order.get('status')}"}), 409
        # The portal only exposes paid orders, but this is the money/stock
        # boundary — verify rather than trust the list filter.
        if order.get("payment_status") != "paid":
            return jsonify({"error": f"Order payment is {order.get('payment_status')}, not paid"}), 409

        ok, err = create_retail_sale_records(order, ship_date)
        if not ok:
            return jsonify({"error": err or "Could not create sale records"}), 409

        # Push approve to the portal. If this fails, inventory has already
        # moved — the idempotency guard makes a retry safe (status re-push only).
        portal_status, portal_resp = _retail_request(
            "PATCH", f"/api/internal/orders/{order_id}",
            {"action": "approve", "fulfillment_date": ship_date},
        )
        if portal_status != 200:
            return jsonify({
                "warning": "Sale records created but portal status update failed — retry Approve to re-push.",
                "portal_error": portal_resp.get("error") if isinstance(portal_resp, dict) else str(portal_resp),
            }), 502

        return jsonify({"ok": True})

    if action in ("decline", "fulfill"):
        # Decline triggers the Stripe refund on the portal side; a portal
        # failure (e.g. refund error) passes through with its status code.
        status, resp = _retail_request("PATCH", f"/api/internal/orders/{order_id}", body)
        return jsonify(resp), status

    return jsonify({"error": f"Unknown action: {action}"}), 400

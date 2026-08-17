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
from zoneinfo import ZoneInfo
from functools import wraps
import urllib.request, urllib.error

from flask import Blueprint, render_template, request, jsonify, session, Response

logger = logging.getLogger(__name__)
ripe_orders_bp = Blueprint("ripe_orders", __name__)

RIPE_PORTAL_URL = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
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
    """True when both RIPE_PORTAL_URL and INTERNAL_API_KEY are set."""
    return bool(RIPE_PORTAL_URL and INTERNAL_API_KEY)


def _ripe_request(method, path, body=None):
    """Make an authenticated internal API call to the Ripe portal.

    Returns (status_code, parsed_json). Yields 503 when unconfigured or on a
    transport-level failure; HTTP error responses pass through with their code.
    """
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


def create_ripe_sale_records(order, delivery_date, payment_key):
    """Write sale records for each line item and immediately FIFO-deduct FG inventory.

    On approval the stock is physically committed — units are moved to the front
    of the warehouse awaiting delivery/pickup. We deduct immediately so FG
    inventory reflects reality from the moment of approval.

    Returns (ok: bool, error: str|None).
    """
    from app import _sku_key as _make_sku_key, timedelta, _compute_available_stock

    sales  = _load(_SALES_PATH, [])
    fg_all = _load(_FG_PATH, [])

    # Idempotency guard (keyed on the Ripe order id). FG is deducted + sales
    # written BEFORE the status push to Ripe; if that push fails the caller
    # returns 502 with inventory already moved, and the order stays "pending"
    # on Ripe — so a retry would otherwise pass the pending-status check and
    # deduct a SECOND time. If sale records already exist for this order, the
    # deduction already happened: skip it and let the caller re-push the status.
    order_id = order.get("id", "")
    if order_id and any(s.get("ripe_order_id") == order_id for s in sales):
        logger.warning(
            "Ripe order %s already has sale records — skipping duplicate FG deduction",
            order_id,
        )
        return True, None

    payment_pending = (payment_key == "cc_net14")
    created = []
    shortfalls = []

    # Pre-flight stock check for SS items (shelf-stable / make-to-stock).
    # FZ/BB are made-to-order and may legitimately fall short — those flow
    # through unchanged below. For SS, refuse approval if the order would
    # exceed currently-available stock (gross minus buffer), protecting
    # against the read/write race where two buyers see the same stock and
    # both order it.
    stock_map = _compute_available_stock()
    insufficient = []
    for item in order.get("items", []):
        units = int(item.get("units") or 0)
        if units <= 0:
            continue
        fmt = (item.get("format") or "").upper()
        if not fmt.startswith("SS"):
            continue
        product_name = (item.get("name") or "").strip()
        fmt_prefix = fmt.split("-")[0]
        match = next((
            f for f in fg_all
            if (f.get("recipe") or "").lower() == product_name.lower()
            and (f.get("format") or "").upper().startswith(fmt_prefix)
        ), None) or next((
            f for f in fg_all
            if product_name.lower() in (f.get("recipe") or "").lower()
            and (f.get("format") or "").upper().startswith(fmt_prefix)
        ), None)
        if not match:
            insufficient.append(f"{product_name} ({item.get('format','')}): no matching stock on hand")
            continue
        sku = _make_sku_key(match.get("brand",""), match.get("recipe",""), match.get("format",""))
        available = stock_map.get(sku, {}).get("available", 0)
        if units > available:
            insufficient.append(
                f"{product_name} ({item.get('format','')}): "
                f"order needs {units} units, only {available} available"
            )
    if insufficient:
        # Wording differs by path. On wholesale the usual cause is a competing
        # approval; on a retail parcel the order is already paid and the packer
        # is standing at the bench, so the useful next step is cancelling it.
        if order.get("order_mode") == "retail":
            return False, (
                "Not enough stock to fill this parcel. " + "; ".join(insufficient)
                + ". Cancel the order and add a credit to Ripe's account."
            )
        return False, (
            "Insufficient SS stock to approve — another order may have been "
            "approved since this one was submitted. " + "; ".join(insufficient)
        )

    for item in order.get("items", []):
        product_name = (item.get("name") or "").strip()
        fmt   = (item.get("format") or "").upper()
        units = int(item.get("units") or 0)
        unit_price = float(item.get("unit_price") or 0)
        line_total = float(item.get("line_total") or 0)
        if units <= 0:
            continue

        # Match FG entry — exact name match first, substring fallback
        fmt_prefix = fmt.split("-")[0]
        match = next((
            f for f in fg_all
            if (f.get("recipe") or "").lower() == product_name.lower()
            and (f.get("format") or "").upper().startswith(fmt_prefix)
        ), None)
        if not match:
            # Substring fallback for legacy name mismatches
            match = next((
                f for f in fg_all
                if product_name.lower() in (f.get("recipe") or "").lower()
                and (f.get("format") or "").upper().startswith(fmt_prefix)
            ), None)

        recipe_name = match.get("recipe", product_name) if match else product_name
        brand       = match.get("brand", "")             if match else ""
        recipe_fmt  = match.get("format", fmt)           if match else fmt
        cert        = match.get("certification", "")     if match else ""
        try:
            sku = _make_sku_key(brand, recipe_name, recipe_fmt)
        except Exception:
            sku = "|".join([brand, recipe_name, recipe_fmt.upper()])

        # ── Immediate FIFO deduction ──────────────────────────────────────────
        candidates = [
            f for f in fg_all
            if (f.get("recipe") or "") == recipe_name
            and (f.get("format") or "").upper() == recipe_fmt.upper()
            and int(f.get("quantity_remaining") or 0) > 0
        ]

        def _prod_date(e):
            wid   = e.get("week_id")
            d_idx = e.get("day_idx")
            if wid and d_idx is not None:
                try:
                    return (datetime.strptime(wid, "%Y-%m-%d") +
                            timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return (e.get("created_at") or "")[:10]

        candidates.sort(key=lambda e: (_prod_date(e), e.get("lot", ""), e.get("id", "")))

        remaining   = units
        lot_summary = {}
        for entry in candidates:
            if remaining <= 0:
                break
            avail = int(entry.get("quantity_remaining") or 0)
            take  = min(avail, remaining)
            entry["quantity_remaining"] = avail - take
            remaining -= take
            lot = entry.get("lot", "")
            if lot not in lot_summary:
                lot_summary[lot] = {"lot": lot, "quantity": 0,
                                    "fg_ids": [], "breakdown": []}
            lot_summary[lot]["quantity"]  += take
            lot_summary[lot]["fg_ids"].append(entry["id"])
            lot_summary[lot]["breakdown"].append({"fg_id": entry["id"], "quantity": take})

        lots_list = list(lot_summary.values())
        shortfall = remaining  # > 0 if insufficient stock (e.g. FZ/BB made-to-order)

        sale = {
            "id":             datetime.now().strftime("%Y%m%d%H%M%S%f") + str(len(sales) + len(created)),
            "sku_key":        sku,
            "brand":          brand,
            "recipe":         recipe_name,
            "format":         recipe_fmt,
            "certification":  cert,
            "quantity":       units,
            "lots":           lots_list,
            "fg_lot":         lots_list[0]["lot"] if len(lots_list) == 1 else "",
            "fg_id":          lots_list[0]["fg_ids"][0] if len(lots_list) == 1 and lots_list[0]["fg_ids"] else "",
            "buyer":          "Ripe",
            "sale_date":      delivery_date,
            "deduction_date": delivery_date,
            "deducted":       True,          # immediate — no longer waiting for delivery date
            "deducted_at":    datetime.now().isoformat(),
            "payment_pending":    payment_pending,
            "payment_method":     payment_key,
            "ripe_order_id":      order.get("id", ""),
            "delivery_label":     order.get("delivery_label", ""),
            "location_name":      order.get("delivery_label", ""),
            "location_address":   order.get("delivery_address", ""),
            "po_number":          order.get("id", ""),
            "case_lot":           "",
            "unit_price":         unit_price,
            "line_total":         line_total,
            "cases":              units // 12 if units else 0,
            "created_at":         datetime.now().isoformat(),
        }
        if shortfall > 0:
            sale["shortfall"] = shortfall   # FZ/BB pre-orders may have partial stock
            shortfalls.append(f"{recipe_name} ({recipe_fmt}): {shortfall} units short")
        created.append(sale)

    sales.extend(created)
    _save(_SALES_PATH, sales)
    _save(_FG_PATH, fg_all)   # save FG with deducted quantity_remaining values

    if shortfalls:
        logger.warning("Ripe order approval: partial deductions — %s", "; ".join(shortfalls))

    return True, None


def _soma_login_required(f):
    """Decorator: require an authenticated Soma session (401 JSON otherwise)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

def _is_awaiting_payment(o):
    """True when the order still needs money in. Declined orders are settled
    (nothing to collect); paid orders are settled. Everything else — pending,
    invoice-sent, payment-failed, awaiting-etransfer — counts as awaiting."""
    if o.get("status") == "declined":
        return False
    return o.get("payment_status") != "paid"


def _month_label(key):
    """Render a 'YYYY-MM' key as 'June 2026'. Falls back to the raw key."""
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return key or "Undated"


def _group_orders_by_month(orders):
    """Group orders (already sorted newest-first) into month buckets, newest
    month first. The current Toronto month is flagged expanded; past months
    collapse. `orders` must be pre-sorted by created_at desc so both the month
    order and the within-month order come out newest-first."""
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


@ripe_orders_bp.route("/ripe-orders")
@_soma_login_required
def ripe_orders_page():
    """Render the Ripe orders page: awaiting-payment orders plus settled orders grouped by month."""
    status, data = _ripe_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    # Retail direct-ship parcels live on /ripe-retail. They share the "pending"
    # status with unapproved wholesale orders, so without this filter they'd
    # show up here as wholesale orders awaiting approval.
    orders = [o for o in orders if o.get("order_mode") != "retail"]
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    awaiting_orders = [o for o in orders if _is_awaiting_payment(o)]
    settled_orders  = [o for o in orders if not _is_awaiting_payment(o)]
    settled_months  = _group_orders_by_month(settled_orders)
    pending_count = sum(1 for o in orders if o.get("status") == "pending")
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Unknown error")

    # Monthly pick-and-pack service fee. Non-fatal: a portal that predates the
    # endpoint, or is briefly unreachable, just renders the page without it.
    fee_status, fee_data = _ripe_request("GET", "/api/internal/service-fees")
    if fee_status == 200 and isinstance(fee_data, dict):
        service_fees = fee_data.get("fees") or []
        service_fee_outstanding = fee_data.get("outstanding") or 0
    else:
        service_fees, service_fee_outstanding = [], 0

    return render_template("ripe_orders.html",
        awaiting_orders=awaiting_orders, settled_months=settled_months,
        pending_count=pending_count, configured=configured, error=error,
        service_fees=service_fees, service_fee_outstanding=service_fee_outstanding)


@ripe_orders_bp.route("/api/ripe-orders/service-fees/<fee_id>", methods=["PATCH"])
@_soma_login_required
def ripe_service_fee_action(fee_id):
    """Confirm an e-transfer against Ripe's monthly service fee.

    Soma is the only party that can see the money land, so Soma marks it
    received — same division of labour as the wholesale e-transfer flow. The
    Ripe portal owns the record and enforces idempotency; this just proxies.
    """
    body = request.get_json() or {}
    if (body.get("action") or "").strip() != "confirm-etransfer":
        return jsonify({"error": "Unsupported action"}), 400

    status, data = _ripe_request("PATCH", f"/api/internal/service-fees/{fee_id}", {
        "action": "confirm-etransfer",
        "reference": (body.get("reference") or "").strip(),
        "confirmed_by": session.get("user") or "soma",
    })
    if status != 200:
        msg = data.get("error") if isinstance(data, dict) else "Unknown error"
        return jsonify({"error": msg}), status
    return jsonify({"ok": True, "fee": data.get("fee")})


@ripe_orders_bp.route("/api/ripe-orders/pending-count")
@_soma_login_required
def ripe_pending_count():
    """Return counts for the dashboard badges: new (pending) and in-progress
    (approved but not yet fulfilled) Ripe orders. Both 0 when unconfigured.

    Wholesale only — retail parcels are also "pending" and have their own badge.
    """
    if not _configured():
        return jsonify({"count": 0, "in_progress": 0, "configured": False})
    status, data = _ripe_request("GET", "/api/internal/orders")
    if status != 200 or not isinstance(data, list):
        return jsonify({"count": 0, "in_progress": 0, "configured": True})
    data = [o for o in data if o.get("order_mode") != "retail"]
    count = sum(1 for o in data if o.get("status") == "pending")
    in_progress = sum(1 for o in data
                      if o.get("status") in ("approved", "approved-for-production"))
    return jsonify({"count": count, "in_progress": in_progress, "configured": True})


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

        # Enforce order rules from Soma company settings before approving
        from app import _load_company_info
        from datetime import datetime as _dt, timedelta as _td
        _company  = _load_company_info()
        _fzbb_small      = int(_company.get("fzbb_small_lead_days")  or 3)
        _fzbb_large      = int(_company.get("fzbb_large_lead_days")  or 7)
        _fzbb_thresh     = int(_company.get("fzbb_large_threshold")  or 8)

        # We need the order details to validate — fetch it
        _get_status, _order_data = _ripe_request("GET", "/api/internal/orders")
        if _get_status == 200 and isinstance(_order_data, list):
            _order_obj = next((o for o in _order_data if o["id"] == order_id), None)
            if _order_obj:
                _items      = _order_obj.get("items", [])
                _fzbb_cases = sum(i.get("cases",0) for i in _items if (i.get("format","") or "").upper().startswith(("FZ","BB")))
                _req_date   = _order_obj.get("requested_date","")

                # SS case minimums were removed 2026-08 — an order of any size is
                # approvable on any destination. FZ/BB lead times below are
                # unchanged. See RETAIL_CONTRACT.md.
                if _fzbb_cases > 0 and _req_date:
                    # Lead time only applies when FZ/BB stock cannot cover the
                    # order. If every FZ/BB line is fully in stock, the order
                    # can be picked up same-day. Any shortfall on any line
                    # falls through to the existing whole-order lead time.
                    from app import _sku_key as _make_sku_key, _compute_available_stock
                    _fg_all    = _load(_FG_PATH, [])
                    _stock_map = _compute_available_stock()
                    _fzbb_shortfall = False
                    for _it in _items:
                        _fmt = (_it.get("format") or "").upper()
                        if not _fmt.startswith(("FZ", "BB")):
                            continue
                        _units = int(_it.get("units") or 0)
                        if _units <= 0:
                            continue
                        _name   = (_it.get("name") or "").strip()
                        _prefix = _fmt.split("-")[0]
                        _match  = next((
                            f for f in _fg_all
                            if (f.get("recipe") or "").lower() == _name.lower()
                            and (f.get("format") or "").upper().startswith(_prefix)
                        ), None) or next((
                            f for f in _fg_all
                            if _name.lower() in (f.get("recipe") or "").lower()
                            and (f.get("format") or "").upper().startswith(_prefix)
                        ), None)
                        if not _match:
                            _fzbb_shortfall = True
                            break
                        _sku = _make_sku_key(
                            _match.get("brand",""),
                            _match.get("recipe",""),
                            _match.get("format",""),
                        )
                        _avail = _stock_map.get(_sku, {}).get("available", 0)
                        if _units > _avail:
                            _fzbb_shortfall = True
                            break

                    if _fzbb_shortfall:
                        try:
                            _req   = _dt.strptime(_req_date, "%Y-%m-%d").date()
                            _today = _dt.utcnow().date()
                            _lead_req = _fzbb_large if _fzbb_cases >= _fzbb_thresh else _fzbb_small
                            _lead_act = (_req - _today).days
                            if _lead_act < _lead_req:
                                return jsonify({
                                    "error": (
                                        f"Cannot approve: {_fzbb_cases} FZ/BB cases require "
                                        f"{_lead_req} days notice. Earliest approvable date: "
                                        f"{(_today + _td(days=_lead_req)).strftime('%B %d, %Y')}."
                                    )
                                }), 400
                        except ValueError:
                            pass

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

        # Auto-deplete any named account credits the order applied (e-transfer).
        # Runs exactly once per order: a re-approve hits the "already {status}"
        # 409 guard above before reaching here. Each applied credit decrements
        # its matching balance by id (floored at 0); a credit that reaches 0 is
        # dropped. Best-effort: a failure here must not undo the approval.
        _applied = order.get("credits_applied") or []
        if _applied:
            try:
                from app import _save_json as _sj, COMPANY_INFO_PATH as _cip, _active_ripe_credits as _act
                _ci = _load_company_info()
                _stored = _act(_ci)                       # migrated + active list
                _by_id = {c["id"]: c for c in _stored}
                for a in _applied:
                    cid = a.get("id")
                    try:
                        amt = max(0.0, float(a.get("amount") or 0))
                    except (TypeError, ValueError):
                        amt = 0.0
                    if cid in _by_id and amt > 0:
                        _by_id[cid]["amount"] = round(max(0.0, _by_id[cid]["amount"] - amt), 2)
                _ci["ripe_credits"] = [c for c in _stored if c["amount"] > 0.005]
                _ci.pop("ripe_credit", None)
                _sj(_cip, _ci)
            except Exception:
                logger.warning("Failed to deplete ripe credits on approve of %s", order_id, exc_info=True)

        return jsonify({"ok": True, "payment_pending": payment_key == "cc_net14"})

    if action in ("decline", "fulfill"):
        status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}", body)
        return jsonify(resp), status

    return jsonify({"error": f"Unknown action: {action}"}), 400


# ─── RETAIL DIRECT SHIP ──────────────────────────────────────────────────────
# Paid parcels waiting to be packed. Soma touches each order exactly once:
# Approve & Print records the sale, deducts stock and produces both documents.
# Do NOT add an intermediate "packed, awaiting pickup" state — it would double
# the interaction cost of the highest-volume path in the system.
# See RETAIL_CONTRACT.md.


def _fetch_ripe_orders():
    status, data = _ripe_request("GET", "/api/internal/orders")
    return (data if status == 200 and isinstance(data, list) else []), status


def _is_retail_to_pack(o):
    """Paid retail order Soma hasn't dealt with yet."""
    return (o.get("order_mode") == "retail"
            and o.get("payment_status") == "paid"
            and o.get("status") == "pending")


def _group_retail_by_batch(orders):
    """Group parcels by the checkout session that paid for them.

    That shared session id IS the batch — Ripe settles N orders with one payment
    and stamps the same id across them, so there is no batch record to join to.
    """
    batches = {}
    for o in orders:
        key = o.get("stripe_checkout_session_id") or "unbatched"
        batches.setdefault(key, []).append(o)
    out = []
    for key, group in batches.items():
        group.sort(key=lambda x: x.get("order_number") or "")
        out.append({
            "session_id": key,
            "orders": group,
            "count": len(group),
            "units": sum(int(x.get("total_units") or 0) for x in group),
            "paid_at": min((x.get("paid_at") or "") for x in group),
        })
    out.sort(key=lambda b: b["paid_at"])
    return out


@ripe_orders_bp.route("/ripe-retail")
@_soma_login_required
def ripe_retail_page():
    """Pack queue for retail direct-ship parcels, grouped by batch."""
    orders, status = _fetch_ripe_orders()
    to_pack = [o for o in orders if _is_retail_to_pack(o)]
    done = [o for o in orders
            if o.get("order_mode") == "retail" and o.get("status") in ("fulfilled", "declined")]
    done.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return render_template(
        "ripe_retail.html",
        batches=_group_retail_by_batch(to_pack),
        pack_count=len(to_pack),
        recent=done[:40],
        configured=_configured(),
        error=None if status == 200 else "Could not reach the Ripe portal.",
    )


@ripe_orders_bp.route("/api/ripe-retail/pack-count")
@_soma_login_required
def ripe_retail_pack_count():
    """Dashboard badge: paid parcels waiting to be packed."""
    if not _configured():
        return jsonify({"count": 0, "configured": False})
    orders, status = _fetch_ripe_orders()
    if status != 200:
        return jsonify({"count": 0, "configured": True})
    return jsonify({"count": sum(1 for o in orders if _is_retail_to_pack(o)), "configured": True})


@ripe_orders_bp.route("/ripe-retail/<order_id>/packing-slip")
@_soma_login_required
def ripe_retail_packing_slip(order_id):
    """Customer-facing packing slip. Goes in the box, so it carries NO pricing."""
    orders, _ = _fetch_ripe_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        return "Order not found", 404
    from app import _load_company_info
    return render_template("ripe_retail_packing_slip.html",
                           order=order, company=_load_company_info())


@ripe_orders_bp.route("/ripe-retail/<order_id>/label")
@_soma_login_required
def ripe_retail_label(order_id):
    """Proxy the shipping label PDF from the Ripe portal.

    Soma's browser can't fetch it directly — the portal's own attachment route
    is session-gated and Soma holds only the internal key, so this streams it
    through using the key-gated internal endpoint.
    """
    if not _configured():
        return "Ripe portal not configured", 503
    url = f"{RIPE_PORTAL_URL}/api/internal/orders/{order_id}/attachment"
    req = urllib.request.Request(url, headers={"X-Internal-Key": INTERNAL_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        return ("No shipping label is attached to this order." if e.code == 404
                else f"Could not fetch the label ({e.code})."), e.code
    except Exception:
        logger.exception("Could not proxy label for %s", order_id)
        return "Could not reach the Ripe portal.", 502
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{order_id}-label.pdf"'})


def _approve_one_retail_order(order):
    """Record the sale for one parcel and mark it fulfilled on Ripe.

    Returns (ok, error). The sale is dated TODAY — the moment Soma packs it —
    not any date carried on the order. Direct-ship orders carry no requested
    date at all.

    Soma touches the order once, so approve and fulfil collapse: this pushes
    straight to fulfilled rather than leaving an approved state nothing exits.
    """
    order_id = order["id"]
    today = datetime.now(ZoneInfo("America/Toronto")).date().isoformat()

    ok, err = create_ripe_sale_records(order, today, "stripe_checkout")
    if not ok:
        return False, err or "Could not create sale records"

    status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}",
                                 {"action": "fulfill", "fulfillment_date": today})
    if status != 200:
        msg = resp.get("error") if isinstance(resp, dict) else str(resp)
        # Stock has already moved at this point. Say so plainly rather than
        # implying nothing happened — see carried debt item on the two-phase
        # commit, which phase 05 addresses.
        return False, f"Sale recorded and stock deducted, but the Ripe portal did not update: {msg}"
    return True, None


@ripe_orders_bp.route("/api/ripe-retail/<order_id>", methods=["PATCH"])
@_soma_login_required
def ripe_retail_action(order_id):
    """approve — record the sale, deduct stock, mark fulfilled.
    cancel  — no money moves; Soma issues a credit by hand in Company Settings.
    """
    body = request.get_json() or {}
    action = (body.get("action") or "").strip()

    orders, status = _fetch_ripe_orders()
    if status != 200:
        return jsonify({"error": "Could not reach the Ripe portal."}), 502
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    if not _is_retail_to_pack(order):
        return jsonify({"error": "This order is not waiting to be packed."}), 409

    if action == "approve":
        ok, err = _approve_one_retail_order(order)
        if not ok:
            return jsonify({"error": err}), 502
        return jsonify({"ok": True})

    if action == "cancel":
        # Deliberately no refund and no void: the payment stays captured with
        # Soma, and a credit is added to Ripe's account by hand. Nothing is
        # reversed in inventory either, because nothing was recorded yet.
        st, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}",
                                 {"action": "decline",
                                  "reason": (body.get("reason") or "Cancelled by Soma").strip()})
        if st != 200:
            msg = resp.get("error") if isinstance(resp, dict) else str(resp)
            return jsonify({"error": msg}), st
        return jsonify({"ok": True, "credit_due": order.get("total")})

    return jsonify({"error": f"Unknown action: {action}"}), 400


@ripe_orders_bp.route("/api/ripe-retail/batch/<session_id>/approve", methods=["POST"])
@_soma_login_required
def ripe_retail_batch_approve(session_id):
    """Approve every parcel in one batch, per-order underneath.

    Deliberately NOT all-or-nothing: each order is recorded independently so one
    short-stock SKU can't block the rest. Failures stay in the queue for Soma to
    cancel, and the caller gets a per-order breakdown.
    """
    orders, status = _fetch_ripe_orders()
    if status != 200:
        return jsonify({"error": "Could not reach the Ripe portal."}), 502

    batch = [o for o in orders
             if _is_retail_to_pack(o) and (o.get("stripe_checkout_session_id") or "unbatched") == session_id]
    if not batch:
        return jsonify({"error": "No unpacked orders in this batch."}), 404

    approved, failed = [], []
    for o in sorted(batch, key=lambda x: x.get("order_number") or ""):
        ok, err = _approve_one_retail_order(o)
        (approved if ok else failed).append(
            {"id": o["id"], "order_number": o.get("order_number"), "error": err})

    logger.info("Retail batch %s: %d approved, %d failed", session_id, len(approved), len(failed))
    return jsonify({"ok": True, "approved": approved, "failed": failed,
                    "approved_count": len(approved), "failed_count": len(failed)})


@ripe_orders_bp.route("/ripe-products")
@_soma_login_required
def ripe_products_page():
    """Products & pricing are now managed via the Buyer edit page.
    Redirect to the Buyers & Suppliers page with a hint.
    """
    from flask import redirect, url_for, flash
    flash("Products and pricing are managed in Buyers & Suppliers → Edit Buyer → Catalogue & Pricing.", "info")
    return redirect("/contacts?tab=buyers")


@ripe_orders_bp.route("/ripe-analytics")
@_soma_login_required
def ripe_analytics_page():
    """Sales analytics — calls Ripe internal API."""
    status, data = _ripe_request("GET", "/api/internal/analytics")
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Could not reach Ripe portal")
    return render_template("ripe_analytics.html", analytics=data if status == 200 else {}, configured=configured, error=error)


@ripe_orders_bp.route("/ripe-orders/<order_id>/packing-slip")
@_soma_login_required
def ripe_packing_slip(order_id):
    """Fetch order detail from Ripe and render packing slip in Soma."""
    status, data = _ripe_request("GET", f"/api/internal/order-detail/{order_id}")
    if status != 200:
        from flask import abort
        abort(404)
    from datetime import datetime as _dt
    return render_template(
        "ripe_packing_slip.html",
        order=data,
        today=_dt.now().strftime("%B %d, %Y"),
    )


@ripe_orders_bp.route("/ripe-orders/export.csv")
@_soma_login_required
def ripe_export_csv():
    """Export all Ripe orders as CSV."""
    import io, csv
    status, data = _ripe_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order ID","Status","Date","Delivery","Payment","Cases","Units","Subtotal","Total"])
    for o in orders:
        w.writerow([
            o.get("id",""), o.get("status",""), o.get("created_at","")[:10],
            o.get("delivery_label",""), o.get("payment_label",""),
            sum(i.get("cases",0) for i in o.get("items",[])),
            sum(i.get("units",0) for i in o.get("items",[])),
            o.get("subtotal",""), o.get("total",""),
        ])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ripe-orders.csv"})


@ripe_orders_bp.route("/ripe-sku-audit")
@_soma_login_required
def ripe_sku_audit_page():
    """Run the cross-reference audit and render results."""
    from flask import current_app
    import os, json, urllib.request, urllib.error

    # Call own audit endpoint (same app, internal key)
    internal_key = os.environ.get("INTERNAL_API_KEY", "")
    audit = {}
    error = None
    try:
        base_url = os.environ.get("SOMA_APP_URL", "http://localhost:5000").rstrip("/")
        req = urllib.request.Request(
            f"{base_url}/api/internal/sku-audit",
            headers={"X-Internal-Key": internal_key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            audit = json.loads(resp.read())
    except Exception as e:
        # If SOMA_APP_URL isn't set, call the function directly
        try:
            from app import _sku_key, _normalize_format, build_display_name, FORMAT_RE
            from app import load_recipes, _load_json, ORGANIC_FG_PATH
            import os as _os

            recipes = load_recipes()
            soma_keys = {}
            for rname, rdata in recipes.items():
                if rdata.get("archived"): continue
                brand = (rdata.get("brand") or "").strip()
                fmt   = _normalize_format((rdata.get("format") or "").strip())
                key   = _sku_key(brand, rname, fmt)
                soma_keys[key] = {
                    "recipe_name": rname, "brand": brand, "format": fmt,
                    "display": build_display_name(rdata, rname),
                    "has_format_in_name": bool(FORMAT_RE.search(rname)),
                }
            fg = _load_json(ORGANIC_FG_PATH, [])
            fg_stock = {}
            for entry in fg:
                key = _sku_key(entry.get("brand",""), entry.get("recipe",""), entry.get("format",""))
                fg_stock[key] = fg_stock.get(key, 0) + int(entry.get("quantity_remaining") or 0)

            status2, ripe_data = _ripe_request("GET", "/api/internal/products")
            ripe_products = ripe_data if isinstance(ripe_data, list) else []

            ripe_audit = []
            ripe_matched = set()
            for p in ripe_products:
                sk = (p.get("soma_sku_key") or "").strip()
                res = {"ripe_id": p["id"], "ripe_name": p["name"],
                       "ripe_format": p.get("format",""), "soma_sku_key": sk,
                       "active": p.get("active", True)}
                if not sk:
                    res["status"] = "no_key"; res["issue"] = "soma_sku_key not set"
                elif sk in soma_keys:
                    res["status"] = "matched"; res["soma_recipe"] = soma_keys[sk]
                    res["fg_units"] = fg_stock.get(sk, 0); res["fg_cases"] = fg_stock.get(sk, 0) // 12
                    ripe_matched.add(sk)
                    if soma_keys[sk]["has_format_in_name"]:
                        res["status"] = "matched_dirty_name"
                        res["issue"] = f"Recipe name contains format suffix"
                else:
                    res["status"] = "broken"; res["issue"] = f"'{sk}' not found in Soma"
                ripe_audit.append(res)

            unlinked = [{"sku_key": k, **v, "fg_units": fg_stock.get(k, 0)}
                        for k, v in soma_keys.items() if k not in ripe_matched]

            matched = sum(1 for r in ripe_audit if r["status"] in ("matched","matched_dirty_name"))
            broken  = sum(1 for r in ripe_audit if r["status"] == "broken")
            no_key  = sum(1 for r in ripe_audit if r["status"] == "no_key")
            dirty   = sum(1 for r in ripe_audit if r["status"] == "matched_dirty_name")

            audit = {
                "summary": {"ripe_products": len(ripe_products), "soma_recipes": len(soma_keys),
                            "matched": matched, "broken": broken, "no_key": no_key,
                            "dirty_names": dirty, "unlinked_soma_recipes": len(unlinked)},
                "ripe_products": ripe_audit,
                "unlinked_soma_recipes": unlinked,
            }
        except Exception as e2:
            error = str(e2)

    return render_template("ripe_sku_audit.html", audit=audit, error=error)


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
  SMTP_USER / SMTP_PASS — Fastmail account for the monthly bookkeeping
                      report (same values as on the Ripe service)
  BOOKKEEPER_EMAIL  — recipient(s) for the monthly report, comma-separated
"""

import os, json, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
import urllib.request, urllib.error

from flask import Blueprint, render_template, request, jsonify, session, redirect, Response

logger = logging.getLogger(__name__)
ripe_orders_bp = Blueprint("ripe_orders", __name__)

RIPE_PORTAL_URL = os.environ.get("RIPE_PORTAL_URL", "").rstrip("/")
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

# Monthly bookkeeping report — same Fastmail account the Ripe portal sends from.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.fastmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
BOOKKEEPER_EMAIL = os.environ.get("BOOKKEEPER_EMAIL", "")

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


def _ripe_push_with_retry(path, body, attempts=3):
    """PATCH the Ripe portal, retrying transient failures.

    Approve is a two-phase commit with no rollback: stock is deducted and sale
    records written BEFORE this push. If it fails, inventory has moved while the
    order still reads pending on Ripe — so a blip on the wire shouldn't be enough
    to strand it.

    Only transport-level failures and 5xx are retried. A 4xx is a decision the
    portal made (already approved, bad state) and repeating it won't help.

    Retrying the whole approve is safe regardless: create_ripe_sale_records is
    idempotency-guarded on the order id, so a second attempt re-pushes the status
    without deducting stock twice.
    """
    import time as _time
    last = (503, {"error": "not attempted"})
    for attempt in range(attempts):
        if attempt:
            _time.sleep(1.5 * attempt)
        status, resp = _ripe_request("PATCH", path, body)
        if status == 200:
            if attempt:
                logger.info("Ripe push %s succeeded on attempt %d", path, attempt + 1)
            return status, resp
        last = (status, resp)
        if status < 500:
            return last          # the portal decided; don't hammer it
        logger.warning("Ripe push %s attempt %d/%d returned %s", path, attempt + 1, attempts, status)
    return last


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


def _organic_lines_in_order(order, fg_all, make_sku_key):
    """Names of order lines whose SKU is organic-certified in FG. Resolves each
    line to FG the same way the deduction does (exact recipe name + format
    prefix, then substring fallback) and calls the SKU organic if ANY FG entry
    under that SKU carries certification 'Organic' — the same test
    retail_orders.py applies. Read-only."""
    lines = []
    for item in order.get("items", []):
        if int(item.get("units") or 0) <= 0:
            continue
        product_name = (item.get("name") or "").strip()
        fmt = (item.get("format") or "").upper()
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
            continue
        sku = make_sku_key(match.get("brand", ""), match.get("recipe", ""), match.get("format", ""))
        if any(make_sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku
               and (f.get("certification") or "").strip().lower() == "organic"
               for f in fg_all):
            lines.append(product_name or sku)
    return lines


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

    # Two-tier organic boundary (same guard as retail_orders.py): organic-
    # certified SKUs are lot-tracked and wholesale-manual-only — the packer
    # records the REAL lots shipped via Manage Inventory → Record Sale. A FIFO
    # deduction here would silently corrupt that lot accuracy, so refuse
    # before anything moves. Organic SKUs are not catalogued on Ripe, so this
    # only fires on a catalogue mistake — and then fails closed with a reason.
    organic_lines = _organic_lines_in_order(order, fg_all, _make_sku_key)
    if organic_lines:
        return False, (
            "Cannot approve: " + ", ".join(organic_lines)
            + " is organic-certified (lot-tracked). Organic SKUs are sold via "
            "Manage Inventory → Record Sale with a packer lot allocation, not "
            "through Ripe. Decline this order and remove the SKU from the Ripe catalogue."
        )

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


def _credit_ledger(orders):
    """Join current credit balances with the usage recorded on Ripe's orders.

    Soma only stores each credit's REMAINING balance (company_info drops a
    credit entirely once approval depletes it to zero), so the issued amount
    is reconstructed per credit id: issued = remaining + what approved orders
    drew. Declined orders are skipped — their credit was never depleted. A
    pending order's draw is still inside the remaining balance, so it is
    reported as `reserved`, never added to `used`. Hand-edits to a balance in
    Company Settings shift the inferred issued figure; that is inherent.
    A monthly promo's running balance also stores a cumulative `issued`,
    which wins when it is larger than the reconstruction (a hand-edit lowered
    the balance) so the row still shows every month's top-up.
    Returns (rows, totals) — rows active-first, then fully-used.
    """
    from app import _load_company_info, _active_ripe_credits, _sanitize_ripe_credits
    try:
        _ci = _load_company_info()
        stored = _sanitize_ripe_credits(_ci["ripe_credits"]) if isinstance(_ci.get("ripe_credits"), list) \
            else _sanitize_ripe_credits(_active_ripe_credits(_ci))
    except Exception:
        logger.warning("Credit ledger: could not read company info", exc_info=True)
        stored = []

    ledger = {}

    def slot(cid, name):
        if cid not in ledger:
            ledger[cid] = {"id": cid, "name": name or "Credit", "used": 0.0,
                           "reserved": 0.0, "remaining": 0.0,
                           "kind": "once", "uses": []}
        elif name and ledger[cid]["name"] == "Credit":
            ledger[cid]["name"] = name
        return ledger[cid]

    for o in sorted(orders, key=lambda o: o.get("created_at", "")):
        status = o.get("status")
        if status == "declined":
            continue
        for a in (o.get("credits_applied") or []):
            try:
                amt = round(float(a.get("amount") or 0), 2)
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            entry = slot(str(a.get("id") or ""), str(a.get("name") or "").strip())
            bucket = "reserved" if status == "pending" else "used"
            entry[bucket] = round(entry[bucket] + amt, 2)
            entry["uses"].append({
                "order_id": o.get("id"),
                "order_number": o.get("order_number"),
                "date": (o.get("created_at") or "")[:10],
                "amount": amt,
                "pending": status == "pending",
            })
    stored_issued = {}
    for c in stored:
        e = slot(c["id"], c["name"])
        e["kind"] = c["kind"]
        if c["kind"] == "monthly":
            stored_issued[c["id"]] = c.get("issued", c["amount"])
        if c["amount"] > 0:
            e["remaining"] = c["amount"]

    rows = []
    for e in ledger.values():
        e["issued"] = round(e["used"] + e["remaining"], 2)
        if e["id"] in stored_issued:
            e["issued"] = round(max(e["issued"], stored_issued[e["id"]]), 2)
        e["used_pct"] = round(e["used"] / e["issued"] * 100) if e["issued"] > 0 else 100
        e["uses"].reverse()  # newest first
        rows.append(e)
    rows.sort(key=lambda e: (e["remaining"] <= 0.005, e["name"].lower()))
    totals = {
        "issued": round(sum(e["issued"] for e in rows), 2),
        "used": round(sum(e["used"] for e in rows), 2),
        "remaining": round(sum(e["remaining"] for e in rows), 2),
    }
    return rows, totals


@ripe_orders_bp.route("/ripe-orders")
@_soma_manager_required
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


@ripe_orders_bp.route("/api/ripe-credit-ledger")
@_soma_manager_required
def ripe_credit_ledger():
    """Credit history for Company Settings: issued/used/remaining per credit.

    Loaded async by company_settings.html so the settings page never blocks
    on the Ripe portal. A 502 carries the portal error for a status line.
    """
    status, data = _ripe_request("GET", "/api/internal/orders")
    if status != 200 or not isinstance(data, list):
        err = data.get("error") if isinstance(data, dict) else "Unknown error"
        return jsonify({"error": err}), 502
    rows, totals = _credit_ledger(data)
    return jsonify({"credits": rows, "totals": totals})


@ripe_orders_bp.route("/api/ripe-orders/service-fees/<fee_id>", methods=["PATCH"])
@_soma_manager_required
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
@_soma_manager_required
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
@_soma_manager_required
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

                # SS case minimums were removed 2026-08 — an order of any size is
                # approvable on any destination. FZ/BB lead times below are
                # unchanged. See RETAIL_CONTRACT.md.
                # The lead time is validated against the delivery_date being
                # approved for — not the order's original requested_date, which
                # is frozen at submit time and would make an order that asked
                # for a too-soon date permanently unapprovable.
                if _fzbb_cases > 0 and delivery_date:
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
                            from zoneinfo import ZoneInfo
                            _req   = _dt.strptime(delivery_date, "%Y-%m-%d").date()
                            _today = _dt.now(ZoneInfo("America/Toronto")).date()
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
        ripe_status, ripe_resp = _ripe_push_with_retry(
            f"/api/internal/orders/{order_id}",
            {"action": "approve", "fulfillment_date": delivery_date},
        )
        if ripe_status != 200:
            return jsonify({
                "warning": "Sale records created and stock deducted, but the Ripe "
                           "portal did not update. Approve again once it is "
                           "reachable — stock will not be deducted twice.",
                "ripe_error": ripe_resp.get("error") if isinstance(ripe_resp, dict) else str(ripe_resp),
            }), 502

        # Auto-deplete any named account credits the order applied (e-transfer).
        # Runs exactly once per order: a re-approve hits the "already {status}"
        # 409 guard above before reaching here. Each applied credit decrements
        # its matching balance by id (floored at 0); a credit that reaches 0 is
        # dropped. Works on the full stored list so a monthly promo's running
        # balance keeps its bookkeeping fields (issued, month) through the
        # rewrite. Best-effort: a failure here must not undo the approval.
        _applied = order.get("credits_applied") or []
        if _applied:
            try:
                from app import (_save_json as _sj, COMPANY_INFO_PATH as _cip,
                                 _active_ripe_credits as _act, _sanitize_ripe_credits as _san)
                _ci = _load_company_info()
                _stored = _san(_ci["ripe_credits"]) if isinstance(_ci.get("ripe_credits"), list) \
                    else _san(_act(_ci))              # migrate the legacy scalar
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


def _retail_batch_key(o):
    """The id of the payment that covered this parcel.

    Ripe settles N orders with one payment and stamps the same id across them —
    a Stripe checkout session id for card, a generated retail_batch_id ("ET-…")
    for e-transfer. That shared id IS the batch; there is no batch record.
    """
    return o.get("retail_batch_id") or o.get("stripe_checkout_session_id") or "unbatched"


def _group_retail_by_batch(orders):
    """Group parcels by the payment that covered them. See _retail_batch_key."""
    batches = {}
    for o in orders:
        key = _retail_batch_key(o)
        batches.setdefault(key, []).append(o)
    out = []
    for key, group in batches.items():
        group.sort(key=lambda x: x.get("order_number") or "")
        out.append({
            "session_id": key,
            "etransfer": any(x.get("payment_key") == "etransfer" for x in group),
            "orders": group,
            "count": len(group),
            "units": sum(int(x.get("total_units") or 0) for x in group),
            "paid_at": min((x.get("paid_at") or "") for x in group),
        })
    out.sort(key=lambda b: b["paid_at"])
    return out


@ripe_orders_bp.route("/ripe-retail")
@_soma_manager_required
def ripe_retail_page():
    """Pack queue for retail direct-ship parcels, grouped by batch."""
    orders, status = _fetch_ripe_orders()
    to_pack = [o for o in orders if _is_retail_to_pack(o)]
    done = [o for o in orders
            if o.get("order_mode") == "retail" and o.get("status") in ("fulfilled", "declined")]
    done.sort(key=lambda o: o.get("created_at", ""), reverse=True)

    # Batches Ripe has committed to e-transfer but Soma hasn't confirmed. Summary
    # only — the parcels stay hidden until confirmed. Non-fatal: a portal that
    # predates the endpoint just renders the page without the section.
    et_status, et_data = _ripe_request("GET", "/api/internal/retail-etransfer-batches")
    etransfer_batches = (et_data.get("batches") or []
                         if et_status == 200 and isinstance(et_data, dict) else [])

    return render_template(
        "ripe_retail.html",
        etransfer_batches=etransfer_batches,
        batches=_group_retail_by_batch(to_pack),
        pack_count=len(to_pack),
        recent=done[:40],
        configured=_configured(),
        error=None if status == 200 else "Could not reach the Ripe portal.",
    )


@ripe_orders_bp.route("/api/ripe-retail/pack-count")
@_soma_manager_required
def ripe_retail_pack_count():
    """Dashboard badge: paid parcels waiting to be packed."""
    if not _configured():
        return jsonify({"count": 0, "configured": False})
    orders, status = _fetch_ripe_orders()
    if status != 200:
        return jsonify({"count": 0, "configured": True})
    return jsonify({"count": sum(1 for o in orders if _is_retail_to_pack(o)), "configured": True})


@ripe_orders_bp.route("/ripe-retail/<order_id>/packing-slip")
@_soma_manager_required
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
@_soma_manager_required
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

    ok, err = create_ripe_sale_records(order, today, order.get("payment_key") or "stripe_checkout")
    if not ok:
        return False, err or "Could not create sale records"

    status, resp = _ripe_push_with_retry(f"/api/internal/orders/{order_id}",
                                         {"action": "fulfill", "fulfillment_date": today})
    if status != 200:
        msg = resp.get("error") if isinstance(resp, dict) else str(resp)
        # Stock has already moved. Say so plainly rather than implying nothing
        # happened, and point at the safe recovery: approving again re-pushes
        # the status without deducting twice.
        return False, (f"Sale recorded and stock deducted, but the Ripe portal did not "
                       f"update: {msg}. Approve again once it is reachable — stock will "
                       f"not be deducted twice.")
    return True, None


@ripe_orders_bp.route("/api/ripe-retail/<order_id>", methods=["PATCH"])
@_soma_manager_required
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


@ripe_orders_bp.route("/api/ripe-retail/etransfer-batches/<batch_id>/confirm", methods=["POST"])
@_soma_manager_required
def ripe_retail_confirm_etransfer(batch_id):
    """Confirm Ripe's e-transfer for a retail batch, releasing it to the pack queue.

    Soma is the only party that can see the money land — same division of labour
    as wholesale e-transfer and the monthly service fee. The Ripe portal owns the
    records and enforces idempotency (409 on a second confirm); this just proxies.
    """
    body = request.get_json(silent=True) or {}
    status, data = _ripe_request(
        "POST", f"/api/internal/retail-etransfer-batches/{batch_id}/confirm", {
            "reference": (body.get("reference") or "").strip(),
            "confirmed_by": session.get("user") or "soma",
        })
    if status != 200:
        msg = data.get("error") if isinstance(data, dict) else "Unknown error"
        return jsonify({"error": msg}), status
    return jsonify({"ok": True, "order_count": data.get("order_count")})


@ripe_orders_bp.route("/api/ripe-retail/batch/<session_id>/approve", methods=["POST"])
@_soma_manager_required
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
             if _is_retail_to_pack(o) and _retail_batch_key(o) == session_id]
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
@_soma_manager_required
def ripe_products_page():
    """Legacy bookmark catcher: products & pricing moved to the Buyer edit page.

    Redirects to Buyers & Suppliers. It used to flash a hint, but no template in
    this codebase renders get_flashed_messages(), so that message was only ever
    banked in the session cookie — dropped rather than left to accumulate.
    """
    from flask import redirect
    return redirect("/contacts?tab=buyers")


@ripe_orders_bp.route("/ripe-analytics")
@_soma_manager_required
def ripe_analytics_page():
    """Sales analytics — calls Ripe internal API."""
    status, data = _ripe_request("GET", "/api/internal/analytics")
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Could not reach Ripe portal")
    return render_template("ripe_analytics.html", analytics=data if status == 200 else {}, configured=configured, error=error)


@ripe_orders_bp.route("/ripe-orders/<order_id>/packing-slip")
@_soma_manager_required
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


def _build_bookkeeping_csv(date_from=None, date_to=None):
    """Fetch Ripe orders and build the bookkeeping CSV (one row per order).

    Includes only approved + fulfilled orders — Soma records the sale at
    approval, so this matches Soma's own books; pending and declined orders
    are not revenue. Window is on the ORDER date (created_at, inclusive);
    the fulfillment date is a column so either basis can be read off.

    Returns (error, payload): error is a message when the Ripe portal can't
    be reached, else payload is (csv_text, order_count, sum_total).
    """
    import io, csv
    status, data = _ripe_request("GET", "/api/internal/orders")
    if status != 200 or not isinstance(data, list):
        msg = data.get("error") if isinstance(data, dict) else "Unexpected response"
        return f"Could not fetch orders from the Ripe portal: {msg}", None

    rows = []
    for o in data:
        if o.get("status") not in ("approved", "fulfilled"):
            continue
        od = (o.get("created_at") or "")[:10]
        if date_from or date_to:
            if not od:
                continue          # a period report must not repeat undated rows every month
            if date_from and od < date_from:
                continue
            if date_to and od > date_to:
                continue
        rows.append(o)
    rows.sort(key=lambda o: (o.get("created_at") or ""))

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order ID", "Order Date", "Fulfillment Date", "Status", "Mode",
                "Payment", "Payment Status", "Paid Date", "Cases", "Units",
                "Subtotal", "Surcharge", "Small Order Fee", "Credit Applied", "Total"])

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    sums = {"subtotal": 0.0, "surcharge": 0.0, "fee": 0.0, "credit": 0.0, "total": 0.0}
    for o in rows:
        subtotal = _f(o.get("subtotal"))
        surcharge = _f(o.get("surcharge"))
        fee = _f(o.get("small_order_fee"))
        credit = _f(o.get("credit_applied"))
        total = _f(o.get("total"))
        sums["subtotal"] += subtotal
        sums["surcharge"] += surcharge
        sums["fee"] += fee
        sums["credit"] += credit
        sums["total"] += total
        w.writerow([
            o.get("id", ""), (o.get("created_at") or "")[:10],
            o.get("fulfillment_date") or "", o.get("status", ""),
            o.get("order_mode") or "wholesale",
            o.get("payment_label", ""), o.get("payment_status", ""),
            (o.get("paid_at") or "")[:10],
            sum(int(i.get("cases") or 0) for i in o.get("items", [])),
            sum(int(i.get("units") or 0) for i in o.get("items", [])),
            f"{subtotal:.2f}", f"{surcharge:.2f}", f"{fee:.2f}",
            f"{credit:.2f}", f"{total:.2f}",
        ])
    w.writerow(["TOTAL", "", "", "", "", "", "", "", "", "",
                f"{sums['subtotal']:.2f}", f"{sums['surcharge']:.2f}",
                f"{sums['fee']:.2f}", f"{sums['credit']:.2f}", f"{sums['total']:.2f}"])
    return None, (buf.getvalue(), len(rows), sums["total"])


def _valid_iso_date(s):
    """True when s parses as a real YYYY-MM-DD date."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


@ripe_orders_bp.route("/ripe-orders/export.csv")
@_soma_manager_required
def ripe_export_csv():
    """Bookkeeping CSV of approved + fulfilled Ripe orders.

    Query params (optional, inclusive, on the order date):
        from=YYYY-MM-DD   to=YYYY-MM-DD
    Omitting both exports every approved/fulfilled order.
    """
    date_from = (request.args.get("from") or "").strip() or None
    date_to = (request.args.get("to") or "").strip() or None
    for d in (date_from, date_to):
        if d and not _valid_iso_date(d):
            return jsonify({"error": f"Invalid date: {d} (expected YYYY-MM-DD)"}), 400
    error, payload = _build_bookkeeping_csv(date_from, date_to)
    if error:
        return jsonify({"error": error}), 502
    csv_text, _, _ = payload
    name = "ripe-sales"
    if date_from or date_to:
        name += f"-{date_from or 'start'}_to_{date_to or 'today'}"
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


def _send_bookkeeping_email(recipients, month_label, date_from, date_to,
                            csv_text, order_count, sum_total):
    """Email the bookkeeping CSV as an attachment. Raises on SMTP failure —
    the caller turns that into a non-200 so the cron run shows as failed."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    body = f"""Hello,

Attached is the Soma Bone Broth × Ripe wholesale sales report for {month_label}.

Period:       {date_from} to {date_to} (order date)
Orders:       {order_count} (approved + fulfilled)
Total:        ${sum_total:,.2f}

Each row is one order, with subtotal, surcharge, small-order fee, applied
credit and total broken out. The final row sums the period.

This is an automated monthly report from the Soma production system.
For questions contact {SMTP_USER}.

— Soma Bone Broth Co Ltd.
"""
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Ripe wholesale sales report — {month_label} | Soma Bone Broth"
    msg.attach(MIMEText(body, "plain"))
    att = MIMEApplication(csv_text.encode("utf-8"), _subtype="csv")
    att.add_header("Content-Disposition", "attachment",
                   filename=f"ripe-sales-{date_from[:7]}.csv")
    msg.attach(att)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, recipients, msg.as_string())


@ripe_orders_bp.route("/api/internal/ripe-sales-report", methods=["POST"])
def internal_ripe_sales_report():
    """Internal endpoint for the monthly Render Cron Job: email last month's
    bookkeeping CSV to BOOKKEEPER_EMAIL.

    Auth: X-Internal-Key header must match INTERNAL_API_KEY.
    Query param month=YYYY-MM overrides the default (previous calendar month
    in America/Toronto) for testing or resending a period.

    Fails LOUDLY: missing config is 503, an unreachable Ripe portal or an
    SMTP failure is 502 — run the cron with `curl -f` so a non-2xx marks the
    run failed instead of a month silently going unmailed. A month with zero
    orders still sends (an empty report is distinguishable from a broken one).
    """
    import hmac as _hmac
    provided = (request.headers.get("X-Internal-Key") or "").strip()
    if not INTERNAL_API_KEY or not _hmac.compare_digest(
        provided.encode(), INTERNAL_API_KEY.encode()
    ):
        return jsonify({"error": "Unauthorized"}), 401

    missing = [n for n, v in [("SMTP_USER", SMTP_USER), ("SMTP_PASS", SMTP_PASS),
                              ("BOOKKEEPER_EMAIL", BOOKKEEPER_EMAIL)] if not v]
    if missing:
        return jsonify({"error": f"Not configured — set {', '.join(missing)} on the Soma Render service"}), 503

    month = (request.args.get("month") or "").strip()
    if month:
        try:
            first = datetime.strptime(month + "-01", "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": f"Invalid month: {month} (expected YYYY-MM)"}), 400
    else:
        try:
            today = datetime.now(ZoneInfo("America/Toronto")).date()
        except Exception:
            today = datetime.now().date()
        first = today.replace(day=1)
        first = (first.replace(year=first.year - 1, month=12) if first.month == 1
                 else first.replace(month=first.month - 1))
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    from datetime import timedelta as _td
    last = next_first - _td(days=1)
    date_from, date_to = first.isoformat(), last.isoformat()
    month_label = first.strftime("%B %Y")

    error, payload = _build_bookkeeping_csv(date_from, date_to)
    if error:
        return jsonify({"error": error}), 502
    csv_text, order_count, sum_total = payload

    recipients = [r.strip() for r in BOOKKEEPER_EMAIL.split(",") if r.strip()]
    try:
        _send_bookkeeping_email(recipients, month_label, date_from, date_to,
                                csv_text, order_count, sum_total)
    except Exception as e:
        logger.exception("Bookkeeping report email failed for %s", month_label)
        return jsonify({"error": f"Email send failed: {e}"}), 502

    logger.info("Bookkeeping report for %s sent to %s (%d orders, $%.2f)",
                month_label, recipients, order_count, sum_total)
    return jsonify({"ok": True, "month": month_label, "from": date_from,
                    "to": date_to, "orders": order_count,
                    "total": round(sum_total, 2), "sent_to": recipients})

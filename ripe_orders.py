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
    """Write sale records for each line item and immediately FIFO-deduct FG inventory.

    On approval the stock is physically committed — units are moved to the front
    of the warehouse awaiting delivery/pickup. We deduct immediately so FG
    inventory reflects reality from the moment of approval.

    Returns (ok: bool, error: str|None).
    """
    from app import _sku_key as _make_sku_key, timedelta, _compute_available_stock

    sales  = _load(_SALES_PATH, [])
    fg_all = _load(_FG_PATH, [])
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


@ripe_orders_bp.route("/ripe-orders")
@_soma_login_required
def ripe_orders_page():
    status, data = _ripe_request("GET", "/api/internal/orders")
    orders = data if isinstance(data, list) else []
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    awaiting_orders = [o for o in orders if _is_awaiting_payment(o)]
    settled_orders  = [o for o in orders if not _is_awaiting_payment(o)]
    pending_count = sum(1 for o in orders if o.get("status") == "pending")
    configured = _configured()
    error = None if status == 200 else (data.get("error") if isinstance(data, dict) else "Unknown error")
    return render_template("ripe_orders.html",
        awaiting_orders=awaiting_orders, settled_orders=settled_orders,
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

        # Enforce order rules from Soma company settings before approving
        from app import _load_company_info
        from datetime import datetime as _dt, timedelta as _td
        _company  = _load_company_info()
        _ss_min      = int(_company.get("ss_min_cases_delivery") or 40)
        _fzbb_small  = int(_company.get("fzbb_small_lead_days")  or 3)
        _fzbb_large  = int(_company.get("fzbb_large_lead_days")  or 7)
        _fzbb_thresh = int(_company.get("fzbb_large_threshold")  or 8)

        # We need the order details to validate — fetch it
        _get_status, _order_data = _ripe_request("GET", "/api/internal/orders")
        if _get_status == 200 and isinstance(_order_data, list):
            _order_obj = next((o for o in _order_data if o["id"] == order_id), None)
            if _order_obj:
                _items      = _order_obj.get("items", [])
                _ss_cases   = sum(i.get("cases",0) for i in _items if (i.get("format","") or "").upper().startswith("SS"))
                _fzbb_cases = sum(i.get("cases",0) for i in _items if (i.get("format","") or "").upper().startswith(("FZ","BB")))
                _delivery   = _order_obj.get("delivery_key","")
                _req_date   = _order_obj.get("requested_date","")

                if _delivery != "pickup" and _ss_cases < _ss_min:
                    return jsonify({
                        "error": f"Cannot approve: delivery orders require at least {_ss_min} SS cases (order has {_ss_cases})."
                    }), 400

                if _fzbb_cases > 0 and _req_date:
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

        return jsonify({"ok": True, "payment_pending": payment_key == "cc_net14"})

    if action in ("decline", "fulfill"):
        status, resp = _ripe_request("PATCH", f"/api/internal/orders/{order_id}", body)
        return jsonify(resp), status

    return jsonify({"error": f"Unknown action: {action}"}), 400


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
    return render_template("ripe_packing_slip.html", order=data, today=_dt.now().strftime("%B %d, %Y"))


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

@ripe_orders_bp.route("/ripe-contract")
@_soma_login_required
def ripe_contract_page():
    """Render the Ripe wholesale contract using live Soma data.
    Pricing sourced directly from the Ripe buyer record in Soma.
    Same document Ripe sees — single source of truth.
    """
    from datetime import date
    from app import (
        _load_buyers, _sku_display, _load_json,
        _load_company_info,
        ORGANIC_FG_PATH, SKU_META_PATH
    )
    company = _load_company_info()

    # Contract constants (keep in sync with Ripe app)
    CONTRACT_EFFECTIVE_DATE = date(2026, 7, 1)
    CONTRACT_EXPIRY_DATE    = date(2027, 6, 30)
    CONTRACT_VERSION        = "1.0 — Jul 2026"
    UNITS_PER_CASE          = 12
    MIN_SS_CASES_DELIVERY   = int(company.get("ss_min_cases_delivery") or 40)
    DELIVERY_LOCATIONS = [
        {"label": "Coco Market",          "address": "2549 Yonge St, Toronto, ON M4P 2H9"},
        {"label": "Woodville",            "address": "155 Woodville Ave, East York, ON M4J 2R4"},
        {"label": "Summerhill Warehouse", "address": "244 Bartley Dr, North York, ON M4A 1G1"},
        {"label": "Soma (Pickup)",        "address": "676 Pape Ave, Toronto, ON"},
    ]

    # Build products list from the Ripe buyer record in Soma
    # (same data the Ripe portal gets via /api/internal/catalogue)
    buyers = _load_buyers()
    ripe_buyer = next(
        (b for b in buyers if b["name"].lower() == "ripe"),
        None
    )

    products = []
    if ripe_buyer:
        for sku in (ripe_buyer.get("skus") or []):
            if not sku.get("active", True):
                continue
            price = sku.get("price") or 0.0
            products.append({
                "name":   sku.get("recipe", ""),
                "format": sku.get("format", ""),
                "sku":    sku.get("buyer_sku", ""),
                "price":  price,
            })
        # Sort: SS → FZ → BB, then by name
        fmt_order = {"SS": 0, "FZ": 1, "BB": 2}
        products.sort(key=lambda p: (
            fmt_order.get((p["format"] or "")[:2].upper(), 9),
            p["name"].lower()
        ))

    return render_template(
        "ripe_contract.html",
        products=products,
        effective_date=CONTRACT_EFFECTIVE_DATE.strftime("%B %d, %Y"),
        expiry_date=CONTRACT_EXPIRY_DATE.strftime("%B %d, %Y"),
        contract_version=CONTRACT_VERSION,
        fzbb_small_lead=int(company.get("fzbb_small_lead_days") or 3),
        fzbb_large_lead=int(company.get("fzbb_large_lead_days") or 7),
        fzbb_threshold=int(company.get("fzbb_large_threshold") or 8),
        units_per_case=UNITS_PER_CASE,
        min_ss_cases=MIN_SS_CASES_DELIVERY,
        delivery_locations=DELIVERY_LOCATIONS,
    )

@ripe_orders_bp.route("/api/ripe-contract/sign", methods=["POST"])
@_soma_login_required
def ripe_contract_sign():
    """Proxy signature submission to Ripe — Soma signing on their side."""
    from flask import request as _req
    body = _req.get_json() or {}
    # Soma's role is 'soma' — pass that explicitly
    body["role_override"] = "soma"
    status, data = _ripe_request("POST", "/api/contract/sign", body)
    from flask import jsonify
    return jsonify(data), status


@ripe_orders_bp.route("/api/ripe-contract/signatures", methods=["GET"])
@_soma_login_required
def ripe_contract_signatures():
    """Proxy — get current signatures from Ripe."""
    status, data = _ripe_request("GET", "/api/contract/signatures")
    from flask import jsonify
    return jsonify(data), status


"""sales.py — Organic sales blueprint extracted from app.py.

Fifth step of the app.py split (CLAUDE.md "Pending architectural work"). Scope:
the 7 organic-sales CRUD/document routes (/api/organic/sales*). NOT in scope:
channel imports (shopify/clover — deliberate near-dupes in app.py), sales
analytics (api_sales_by_buyer — future analytics blueprint), api_settle_ripe_sale (Ripe), company-info, and the trace route —
those share the sales region physically but belong to other domains.

Pattern (matches buyers/recipes): routes move, shared helpers STAY. The buyer
helper _load_buyers and the path constants ORGANIC_FG_PATH / ORGANIC_SALES_PATH
live in app.py (other code there uses them) and are reached via `import app` +
app.-qualification. Foundation-layer names (_load_json, _save_json, _sku_key,
_add_contact, _load_company_info) are imported directly from helpers, same as
suppliers.py. stdlib (os, datetime, timedelta) imported directly.

The FG/FIFO deduction logic inside add_organic_sale / add_sale_order operates
inline on the finished-goods JSON (via _load_json/_save_json on
ORGANIC_FG_PATH) — there is no shared deduction helper to qualify, which is why
only three app.py names are referenced.

`__file__` (used in get_packing_slip to locate static/logo.*) resolves to this
module's directory, which is the same repo root as app.py — so the logo path is
unchanged.

Defines its own manager_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
import os
import copy
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, url_for, send_file, render_template

# Foundation layer (dependency-free) — imported directly, same as suppliers.py.
from helpers import (
    _load_json,
    _save_json,
    _sku_key,
    _add_contact,
    _load_company_info,
    _in_date_window,
    _organic_sku_code,
    _sku_display,
    _parse_emails,
    _send_email,
)

import app

sales_bp = Blueprint("sales", __name__)


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


def _fg_prod_date(e):
    """Production date for FIFO ordering: week_id+day_idx, else the created_at date."""
    wid, d_idx = e.get("week_id"), e.get("day_idx")
    if wid and d_idx is not None:
        try:
            return (datetime.strptime(wid, "%Y-%m-%d") + timedelta(days=int(d_idx))).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return (e.get("created_at") or "")[:10]


def _deduct_fifo(fg, sku_key, quantity):
    """FIFO-deduct `quantity` of sku_key across its lots (oldest production date
    first), mutating fg in place. Returns (sale_lots, brand, recipe, fmt). Raises
    ValueError if the SKU has no stock or not enough. (Extracted verbatim from the
    prior inline logic so non-allocation sales behave identically.)"""
    candidates = [f for f in fg
                  if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
                  and int(f.get("quantity_remaining") or 0) > 0]
    if not candidates:
        raise ValueError("No inventory available for this SKU")
    candidates.sort(key=lambda e: (_fg_prod_date(e), e.get("lot", ""), e.get("id", "")))
    total = sum(int(e.get("quantity_remaining") or 0) for e in candidates)
    if quantity > total:
        raise ValueError(f"Not enough inventory: requested {quantity}, available {total}")
    first = candidates[0]
    remaining = quantity
    lot_summary = {}
    for entry in candidates:
        if remaining <= 0:
            break
        avail = int(entry.get("quantity_remaining") or 0)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        entry["quantity_remaining"] = avail - take
        remaining -= take
        lot = entry.get("lot", "")
        if lot not in lot_summary:
            lot_summary[lot] = {"lot": lot, "quantity": 0, "fg_ids": [], "breakdown": []}
        lot_summary[lot]["quantity"] += take
        lot_summary[lot]["fg_ids"].append(entry.get("id"))
        lot_summary[lot]["breakdown"].append({"fg_id": entry.get("id"), "quantity": take})
    return (list(lot_summary.values()), first.get("brand", ""),
            first.get("recipe", ""), first.get("format", ""))


def _deduct_allocation(fg, sku_key, allocation, quantity):
    """Deduct EXACTLY the lots named in `allocation` (list of {lot, quantity}) —
    for organic sales where the packer records the real lot(s) shipped instead of
    FIFO-guessing. Validates the allocation sums to `quantity` and each lot has
    enough stock; deducts FIFO within a lot if it spans entries. Mutates fg.
    Returns (sale_lots, brand, recipe, fmt). Raises ValueError on any mismatch."""
    items = [a for a in (allocation or []) if isinstance(a, dict)]
    total = 0
    for a in items:
        try:
            total += int(a.get("quantity") or 0)
        except (ValueError, TypeError):
            pass
    if total != quantity:
        raise ValueError(f"Lot allocation ({total}) must equal the sale quantity ({quantity})")
    rep = next((f for f in fg
                if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key), None)
    if rep is None:
        raise ValueError("No inventory available for this SKU")
    sale_lots = []
    for a in items:
        lot = (a.get("lot") or "").strip()
        try:
            need = int(a.get("quantity") or 0)
        except (ValueError, TypeError):
            need = 0
        if need <= 0:
            continue
        entries = [f for f in fg
                   if _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
                   and (f.get("lot") or "") == lot
                   and int(f.get("quantity_remaining") or 0) > 0]
        entries.sort(key=lambda e: (e.get("created_at") or "", e.get("id", "")))
        avail = sum(int(e.get("quantity_remaining") or 0) for e in entries)
        if need > avail:
            raise ValueError(f"Lot {lot or '(blank)'}: requested {need}, only {avail} available")
        rec = {"lot": lot, "quantity": 0, "fg_ids": [], "breakdown": []}
        rem = need
        for e in entries:
            if rem <= 0:
                break
            take = min(int(e.get("quantity_remaining") or 0), rem)
            e["quantity_remaining"] = int(e.get("quantity_remaining") or 0) - take
            rem -= take
            rec["quantity"] += take
            rec["fg_ids"].append(e.get("id"))
            rec["breakdown"].append({"fg_id": e.get("id"), "quantity": take})
        sale_lots.append(rec)
    return sale_lots, rep.get("brand", ""), rep.get("recipe", ""), rep.get("format", "")


def _restore_sale_lots(fg, sale):
    """Restore the FG quantities a sale drew from, back into `fg` (mutated in
    place) — the exact inverse of a deduction. Handles all three sale shapes:
    new lots[] with per-fg_id breakdown (restores to the exact entries),
    legacy lots[] without breakdown (best-effort to the first fg_id),
    and the pre-multi-LOT single-fg_id shape. Shared by delete and edit so
    both reverse a deduction identically."""
    if sale.get("lots"):
        for lot_entry in sale["lots"]:
            qty_to_restore = int(lot_entry.get("quantity") or 0)
            if qty_to_restore <= 0:
                continue
            breakdown = lot_entry.get("breakdown")
            if breakdown:
                for b in breakdown:
                    target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                    if target:
                        target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(b.get("quantity") or 0)
                continue
            fg_ids = lot_entry.get("fg_ids") or []
            for fid in fg_ids:
                target = next((f for f in fg if f.get("id") == fid), None)
                if target:
                    target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + qty_to_restore
                    break
    elif sale.get("fg_id"):
        target = next((f for f in fg if f.get("id") == sale["fg_id"]), None)
        if target:
            target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(sale.get("quantity") or 0)


@sales_bp.route("/sales-receiving")
@manager_required
def sales_receiving_page():
    """Render the Sales & Receiving Record page (sales, receiving, search & trace).

    Extracted from the Records tab of Manage Inventory (organic.html) on
    2026-08-18; /organic?tab=records redirects here.
    """
    return render_template("sales_receiving.html")


@sales_bp.route("/api/organic/sales", methods=["GET"])
@manager_required
def get_organic_sales():
    """Return sales records.

    Optional query params, all AND-ed, all omittable:
        ?certification=X   — that tier only
        ?from=YYYY-MM-DD   — sale_date on or after (inclusive)
        ?to=YYYY-MM-DD     — sale_date on or before (inclusive)

    Sale records carry the certification of the SKU sold, derived from the
    recipe at creation time.

    Omitting from/to returns EVERYTHING, so every existing caller is unaffected.
    The window is applied server-side on purpose: filtering in the browser would
    still ship the whole table, and these params are the same predicate a
    database would take as a WHERE clause — so moving sales to SQL later changes
    this function's body, never its contract.

    Filtered on sale_date (the business date), falling back to created_at. NOT
    deducted_at: that is when stock left FG, which for a wholesale order is a
    different day from the sale itself.
    """
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        sales = [s for s in sales
                 if (s.get("certification") or "").lower() == cert_filter.lower()]
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    if date_from or date_to:
        sales = [s for s in sales
                 if _in_date_window(s.get("sale_date") or s.get("created_at"),
                                    date_from, date_to)]
    return jsonify(sales)


@sales_bp.route("/api/organic/sales", methods=["POST"])
@manager_required
def add_organic_sale():
    """Record a sale. Two body shapes accepted:

    NEW (preferred): {sku_key, quantity, buyer, sale_date, case_lot}
        FIFO-deducts across LOTs of that SKU (oldest production date first).
        Sale record stores a 'lots' array with the breakdown.

    LEGACY: {fg_id, quantity, buyer, sale_date, case_lot}
        Deducts from a specific FG entry (per-kettle batch). Kept for
        traceability flows that target a specific batch.
    """
    data = request.json or {}
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])

    try:
        quantity = int(data.get("quantity", 0))
    except (ValueError, TypeError):
        quantity = 0
    if quantity <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400

    sku_key = (data.get("sku_key") or "").strip()
    fg_id = (data.get("fg_id") or "").strip()

    # Auto-derive sku_key from brand/recipe/format if not supplied directly
    if not sku_key and not fg_id:
        brand_f = (data.get("brand") or "").strip()
        recipe_f = (data.get("recipe") or "").strip()
        format_f = (data.get("format") or "").strip()
        if recipe_f:
            sku_key = _sku_key(brand_f, recipe_f, format_f)
        else:
            return jsonify({"error": "Either sku_key, fg_id, or recipe required"}), 400

    # Go-live 2026-10-01: organic stock leaves ONLY through Organic Sale, where
    # every case is scanned so its real LOT# is on the sale.
    legacy_fg = next((f for f in fg if f.get("id") == fg_id), None) if fg_id and not sku_key else None
    if (sku_key and _is_organic_sku(fg, sku_key)) or (
            legacy_fg and (legacy_fg.get("certification") or "").strip().lower() == "organic"):
        return jsonify({"error": ORGANIC_USE_SCAN}), 400

    sale_lots = []   # records what was deducted
    brand = recipe = fmt = ""

    allocation = data.get("allocated_lots") or None
    if sku_key:
        # Organic sales pass an explicit lot allocation (the packer-recorded
        # lots); everything else FIFO-deducts oldest production date first. Both
        # paths produce the same sale_lots / breakdown shape.
        try:
            if allocation:
                sale_lots, brand, recipe, fmt = _deduct_allocation(fg, sku_key, allocation, quantity)
            else:
                sale_lots, brand, recipe, fmt = _deduct_fifo(fg, sku_key, quantity)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    else:
        # LEGACY path: single fg_id
        fg_entry = next((f for f in fg if f.get("id") == fg_id), None)
        if not fg_entry:
            return jsonify({"error": "Finished good not found"}), 404
        avail = int(fg_entry.get("quantity_remaining") or 0)
        if quantity > avail:
            return jsonify({"error": f"Not enough inventory: requested {quantity}, available {avail}"}), 400
        fg_entry["quantity_remaining"] = avail - quantity
        brand = fg_entry.get("brand", "")
        recipe = fg_entry.get("recipe", "")
        fmt = fg_entry.get("format", "")
        sale_lots = [{
            "lot": fg_entry.get("lot", ""),
            "quantity": quantity,
            "fg_ids": [fg_entry.get("id")],
            "breakdown": [{"fg_id": fg_entry.get("id"), "quantity": quantity}],
        }]

    # Determine certification from the FG entry(ies) we deducted from
    # (consistent within a SKU, so we can pull from the first one).
    sale_cert = ""
    for lot_entry in sale_lots:
        for b in (lot_entry.get("breakdown") or []):
            target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
            if target and target.get("certification"):
                sale_cert = target["certification"]
                break
        if sale_cert:
            break

    sale = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S") + str(len(sales)),
        "sku_key": sku_key or _sku_key(brand, recipe, fmt),
        "brand": brand,
        "recipe": recipe,
        "format": fmt,
        "certification": sale_cert,
        "quantity": quantity,
        "lots": sale_lots,
        # Convenience fields for legacy display
        "fg_lot": (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
        "fg_id": (sale_lots[0]["fg_ids"][0] if len(sale_lots) == 1 and len(sale_lots[0]["fg_ids"]) == 1 else ""),
        "buyer": data.get("buyer", ""),
        "sale_date": data.get("sale_date", ""),
        "case_lot": data.get("case_lot", ""),
        "po_number": data.get("po_number", ""),
        "created_at": datetime.now().isoformat(),
    }
    sales.append(sale)
    _save_json(app.ORGANIC_SALES_PATH, sales)
    _save_json(app.ORGANIC_FG_PATH, fg)
    buyer = data.get("buyer", "").strip()
    if buyer:
        _add_contact("buyer", buyer)
    return jsonify({"success": True, "id": sale["id"], "sale": sale})


def _order_line_record(fg, order_id, data, sku_key, brand, recipe, fmt, quantity, sale_lots, unit_price):
    """One sale row of a multi-line order — the shape Record Sale and Organic
    Sale both write, so the packing slip, Organic Lots and the trace read
    either the same way. `data` is the order body (buyer, dates, location)."""
    sale_cert = ""
    for lot_entry in sale_lots:
        for b in (lot_entry.get("breakdown") or []):
            target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
            if target and target.get("certification"):
                sale_cert = target["certification"]
                break
        if sale_cert:
            break
    return {
        "id":          "SL-" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "order_id":    order_id,
        "sku_key":     sku_key,
        "brand":       brand,
        "recipe":      recipe,
        "format":      fmt,
        "certification": sale_cert,
        "quantity":    quantity,
        "cases":       quantity // 12,
        "lots":        sale_lots,
        "fg_lot":      (sale_lots[0]["lot"] if len(sale_lots) == 1 else ""),
        "fg_id":       (sale_lots[0]["fg_ids"][0] if len(sale_lots) == 1 and sale_lots[0]["fg_ids"] else ""),
        "buyer":       (data.get("buyer") or "").strip(),
        "buyer_id":    data.get("buyer_id", ""),
        "sale_date":   data.get("sale_date") or datetime.now().date().isoformat(),
        "po_number":   (data.get("po_number") or "").strip(),
        "location_name":    (data.get("location_name") or "").strip(),
        "location_address": (data.get("location_address") or "").strip(),
        "unit_price":  unit_price,
        "line_total":  round(unit_price * quantity, 2) if unit_price is not None else None,
        "created_at":  datetime.now().isoformat(),
    }


@sales_bp.route("/api/organic/sales/order", methods=["POST"])
@manager_required
def add_sale_order():
    """Record a complete sale order — multiple SKUs in one transaction.

    Body: {
        buyer:            str,
        buyer_id:         str (optional),
        sale_date:        str YYYY-MM-DD,
        po_number:        str (optional),
        location_name:    str (optional),
        location_address: str (optional),
        lines: [
            { sku_key: str, brand: str, recipe: str, format: str, quantity: int },
            ...
        ]
    }

    All lines share one order_id. Each line gets its own sale record (for
    per-SKU LOT tracking and inventory deduction) but they are linked by
    order_id so the Records view, packing slip, and invoice treat them as
    one transaction.
    """
    data = request.get_json() or {}
    lines = data.get("lines") or []
    if not lines:
        return jsonify({"error": "lines array required"}), 400

    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    fg    = _load_json(app.ORGANIC_FG_PATH, [])

    buyer        = (data.get("buyer") or "").strip()

    # Go-live 2026-10-01: refuse the WHOLE order if any line is organic — this
    # route keeps the lines that work, so a per-line skip would quietly ship an
    # order without its organic product.
    for line in lines:
        key = (line.get("sku_key") or "").strip() or (
            _sku_key(line.get("brand", ""), line.get("recipe", ""), line.get("format", ""))
            if line.get("recipe") else "")
        if key and _is_organic_sku(fg, key):
            return jsonify({"error": ORGANIC_USE_SCAN, "details": [key]}), 400

    order_id = "ORD-" + datetime.now().strftime("%Y%m%d%H%M%S")

    saved_ids   = []
    saved_sales = []
    errors      = []

    for line in lines:
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            continue

        sku_key    = (line.get("sku_key") or "").strip()
        brand      = (line.get("brand")   or "").strip()
        recipe     = (line.get("recipe")  or "").strip()
        fmt        = (line.get("format")  or "").strip()
        unit_price = line.get("unit_price")
        if unit_price is not None:
            try:
                unit_price = round(float(unit_price), 2)
            except (TypeError, ValueError):
                unit_price = None

        if not sku_key:
            if recipe:
                sku_key = _sku_key(brand, recipe, fmt)
            else:
                errors.append(f"Line missing sku_key and recipe: {line}")
                continue

        # Organic lines carry an explicit lot allocation (packer-recorded);
        # others FIFO-deduct. Same sale_lots/breakdown shape either way.
        allocation = line.get("allocated_lots") or None
        try:
            if allocation:
                sale_lots, brand_d, recipe_d, fmt_d = _deduct_allocation(fg, sku_key, allocation, quantity)
            else:
                sale_lots, brand_d, recipe_d, fmt_d = _deduct_fifo(fg, sku_key, quantity)
        except ValueError as e:
            errors.append(f"{sku_key}: {e}")
            continue
        brand  = brand_d  or brand
        recipe = recipe_d or recipe
        fmt    = fmt_d    or fmt

        sale = _order_line_record(fg, order_id, data, sku_key, brand, recipe, fmt,
                                  quantity, sale_lots, unit_price)
        saved_ids.append(sale["id"])
        saved_sales.append(sale)
        sales.append(sale)

    if errors and not saved_ids:
        return jsonify({"error": "All lines failed", "details": errors}), 400

    _save_json(app.ORGANIC_SALES_PATH, sales)
    _save_json(app.ORGANIC_FG_PATH, fg)

    if buyer:
        _add_contact("buyer", buyer)

    return jsonify({
        "success":  True,
        "order_id": order_id,
        "ids":      saved_ids,
        "saved":    len(saved_ids),
        "errors":   errors,
    })


ORGANIC_USE_SCAN = "Organic products are sold through Organic Sale, where each case is scanned"


def _is_organic_sku(fg, sku_key):
    """Any finished-goods entry under the SKU certified Organic — the same
    test the automated channels refuse on."""
    return any((f.get("certification") or "").strip().lower() == "organic"
               and _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")) == sku_key
               for f in fg)


@sales_bp.route("/organic-sale")
@manager_required
def organic_sale_page():
    """Organic Sale: scan organic cases on the phone, add the rest, one order."""
    return render_template("organic_sale.html")


@sales_bp.route("/api/organic/sale-stock", methods=["GET"])
@manager_required
def get_organic_sale_stock():
    """What the Organic Sale page can sell, so a scan resolves on the phone
    with no round trip. `organic`: one row per organic (SKU, LOT#) in stock,
    with the SKU code its case QR carries and whole cases held. `plain`: every
    other SKU in stock (FIFO lines). The order route re-checks everything."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    organic, plain = {}, {}
    for f in fg:
        held = int(f.get("quantity_remaining") or 0)
        if held <= 0:
            continue
        key = _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        name = _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        if _is_organic_sku(fg, key):
            row = organic.setdefault((key, f.get("lot") or ""), {
                "sku_key": key, "name": name, "lot": f.get("lot") or "",
                "recipe": f.get("recipe", ""), "format": f.get("format", ""),
                "sku_code": _organic_sku_code(key), "held": 0})
        else:
            row = plain.setdefault(key, {"sku_key": key, "name": name, "held": 0,
                                         "recipe": f.get("recipe", ""), "format": f.get("format", "")})
        row["held"] += held
    for row in organic.values():
        row["cases"] = row["held"] // 12
    return jsonify({
        "organic": sorted(organic.values(), key=lambda r: (r["name"], r["lot"])),
        "plain": sorted(plain.values(), key=lambda r: r["name"]),
    })


@sales_bp.route("/api/organic/sales/organic-order", methods=["POST"])
@manager_required
def add_organic_scan_order():
    """Organic Sale (2026-09-30): an order built by scanning organic cases.

    Body: {
        buyer, buyer_id, sale_date (delivery), po_number, location_name, location_address,
        cases: [{sku_key, lot, cases}, ...]   # scanned organic cases, 12 jars each
        lines: [{sku_key, cases}, ...]        # everything else, FIFO
    }

    Organic cases come off EXACTLY the lot scanned (_deduct_allocation); the
    other lines FIFO. An organic SKU cannot be sold as a plain line and a plain
    SKU cannot be scanned. ALL-OR-NOTHING: the deduction runs on a copy of
    finished goods and nothing is saved unless every line fits — unlike
    /api/organic/sales/order, which keeps the lines that worked. Prices come
    from the buyer's catalogue on file (per jar), not from the phone.
    `deducted_at` is stamped because stock leaves at recording, while
    sale_date is the delivery date.
    """
    data = request.get_json() or {}
    fg = copy.deepcopy(_load_json(app.ORGANIC_FG_PATH, []))
    buyers = app._load_buyers()
    buyer_rec = next((b for b in buyers if b.get("id") == data.get("buyer_id")), None)
    if not buyer_rec:
        return jsonify({"error": "Pick a buyer"}), 400
    data["buyer"] = buyer_rec.get("name", "")
    prices = {s.get("sku_key"): s.get("price") for s in buyer_rec.get("skus") or []}

    def _count(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    organic = {}   # sku_key -> {lot: jars}
    for c in data.get("cases") or []:
        key, lot, n = (c.get("sku_key") or "").strip(), (c.get("lot") or "").strip(), _count(c.get("cases"))
        if key and lot and n > 0:
            organic.setdefault(key, {})
            organic[key][lot] = organic[key].get(lot, 0) + n * 12
    plain = {}
    for ln in data.get("lines") or []:
        key, n = (ln.get("sku_key") or "").strip(), _count(ln.get("cases"))
        if key and n > 0:
            plain[key] = plain.get(key, 0) + n * 12
    if not organic and not plain:
        return jsonify({"error": "Nothing to record: scan a case or add a product"}), 400

    errors, deducted = [], []
    for key, by_lot in organic.items():
        if not _is_organic_sku(fg, key):
            errors.append(f"{key}: not an organic product, so it cannot be scanned")
            continue
        allocation = [{"lot": lot, "quantity": q} for lot, q in by_lot.items()]
        try:
            deducted.append((key, sum(by_lot.values()), _deduct_allocation(fg, key, allocation, sum(by_lot.values()))))
        except ValueError as e:
            errors.append(f"{key}: {e}")
    for key, qty in plain.items():
        if _is_organic_sku(fg, key):
            errors.append(f"{key}: organic, so its cases must be scanned")
            continue
        try:
            deducted.append((key, qty, _deduct_fifo(fg, key, qty)))
        except ValueError as e:
            errors.append(f"{key}: {e}")
    if errors:
        return jsonify({"error": "Nothing was saved", "details": errors}), 400

    order_id = "ORD-" + datetime.now().strftime("%Y%m%d%H%M%S")
    now_iso = datetime.now().isoformat()
    new_rows = []
    for key, qty, (sale_lots, brand, recipe, fmt) in deducted:
        price = prices.get(key)
        try:
            price = round(float(price), 2) if price is not None else None
        except (TypeError, ValueError):
            price = None
        row = _order_line_record(fg, order_id, data, key, brand, recipe, fmt, qty, sale_lots, price)
        row["deducted_at"] = now_iso
        row["entry"] = "organic_sale"
        new_rows.append(row)

    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    sales.extend(new_rows)
    _save_json(app.ORGANIC_SALES_PATH, sales)
    _save_json(app.ORGANIC_FG_PATH, fg)
    _add_contact("buyer", data["buyer"])
    # The sale is saved; the email comes after and can never undo or block it.
    email = _email_organic_slip(new_rows)
    return jsonify({"success": True, "order_id": order_id, "ids": [r["id"] for r in new_rows],
                    "slip_sale_id": new_rows[0]["id"], "email": email})


def _email_organic_slip(rows):
    """Email an Organic Sale's packing slip (PDF) to Company Settings'
    organic_slip_emails (2026-10-01). Returns what happened for the done
    screen; never raises — a failed email must not look like a failed sale."""
    recipients, _ = _parse_emails(_load_company_info().get("organic_slip_emails", ""))
    if not recipients:
        return {"sent": False, "reason": "no_recipients"}
    try:
        built = _packing_slip_pdf(rows[0]["id"])
        if built is None:
            return {"sent": False, "error": "packing slip not found"}
        pdf, filename = built
        first = rows[0]
        lines = "\n".join(
            f"  {r['recipe']} {r['format']}: {r['quantity'] // 12} case(s)"
            + (f"  LOT# {', '.join(l['lot'] for l in r.get('lots') or [])}" if r.get("lots") else "")
            for r in rows)
        po = f" · PO {first['po_number']}" if first.get("po_number") else ""
        body = (f"Organic sale recorded for {first['buyer']}{po}.\n"
                f"Delivery date: {first['sale_date']}\n\n{lines}\n\n"
                "The packing slip is attached.\n\n— Soma Bone Broth (sent automatically by Soma)\n")
        _send_email(recipients, f"Packing slip · {first['buyer']}{po} · {first['sale_date']}",
                    body, [(filename, pdf, "pdf")])
        return {"sent": True, "to": recipients}
    except Exception as e:   # noqa: BLE001 — reported on screen, never fatal
        app.logger.warning("Organic slip email failed: %s", e)
        return {"sent": False, "error": str(e), "to": recipients}


@sales_bp.route("/api/organic/sales/<sale_id>/email-slip", methods=["POST"])
@manager_required
def email_slip_again(sale_id):
    """Email an already-recorded order's packing slip to the organic list —
    for a sale whose automatic email failed or went before addresses were set."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"sent": False, "error": "Sale not found"}), 404
    rows = [s for s in sales if s.get("order_id") and s.get("order_id") == sale.get("order_id")] or [sale]
    rows.sort(key=lambda s: s.get("created_at", ""))
    result = _email_organic_slip(rows)
    return jsonify(result), (200 if result.get("sent") else 502 if result.get("error") else 400)


@sales_bp.route("/api/organic/slip-email/test", methods=["POST"])
@manager_required
def test_organic_slip_email():
    """Send a test message to the organic packing-slip list, so the setup can
    be checked before a real sale depends on it."""
    recipients, invalid = _parse_emails(_load_company_info().get("organic_slip_emails", ""))
    if not recipients:
        return jsonify({"ok": False, "error": "Add at least one address first"}), 400
    try:
        _send_email(recipients, "Test · Soma organic packing slips",
                    "This is a test from Soma. Organic Sale packing slips will be emailed to "
                    + ", ".join(recipients) + ".\n")
    except Exception as e:   # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "to": recipients})


@sales_bp.route("/api/organic/sales/<sale_id>", methods=["PATCH"])
@manager_required
def edit_organic_sale(sale_id):
    """Edit a completed sale record. Supports updating:
      - sale_date
      - buyer
      - location_name / location_address
      - quantity (adjusts FG by the delta — restores or deducts)
      - po_number / notes

    Quantity changes: if new qty > old qty, tries to FIFO-deduct the difference.
    If new qty < old qty, restores the difference to the most recent LOT drawn.
    """
    data = request.get_json() or {}
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    idx = next((i for i, s in enumerate(sales) if s.get("id") == sale_id), None)
    if idx is None:
        return jsonify({"error": "Sale not found"}), 404
    sale = dict(sales[idx])

    for field in ("sale_date", "buyer", "po_number", "notes", "location_name", "location_address"):
        if field in data:
            sale[field] = (data[field] or "").strip()

    if "quantity" in data:
        new_qty = int(data["quantity"])
        if new_qty < 0:
            return jsonify({"error": "Quantity must be non-negative"}), 400
        old_qty = int(sale.get("quantity") or 0)

        if new_qty != old_qty:
            # Organic sales are lot-exact — the units shipped tie to specific
            # packer-recorded lots. Re-quantifying needs a fresh lot allocation
            # we don't have here, so refuse and direct to delete + re-record.
            if (sale.get("certification") or "").strip().lower() == "organic":
                return jsonify({"error": "Editing the quantity of an organic (lot-tracked) sale isn't supported. Delete it and re-record with the correct lot allocation."}), 422

            sku_key = sale.get("sku_key") or _sku_key(
                sale.get("brand", ""), sale.get("recipe", ""), sale.get("format", ""))

            # Reverse the ORIGINAL deduction in full, then re-deduct the new
            # quantity FIFO — on a trial copy so an insufficient-stock failure
            # leaves both FG and the sale record untouched (no partial drift).
            trial = copy.deepcopy(fg)
            _restore_sale_lots(trial, sale)
            if new_qty == 0:
                new_lots = []
            else:
                try:
                    new_lots, _b, _r, _f = _deduct_fifo(trial, sku_key, new_qty)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 422

            # Commit the trial inventory and rewrite the sale's lot record so it
            # always reflects what is actually deducted (a later delete then
            # restores the right amount).
            fg[:] = trial
            sale["lots"] = new_lots
            sale["fg_lot"] = (new_lots[0]["lot"] if len(new_lots) == 1 else "")
            sale["fg_id"] = (new_lots[0]["fg_ids"][0]
                             if len(new_lots) == 1 and len(new_lots[0]["fg_ids"]) == 1 else "")
            sale["quantity"] = new_qty
            _save_json(app.ORGANIC_FG_PATH, fg)

    sale["edited_at"] = datetime.now().isoformat()
    sales[idx] = sale
    _save_json(app.ORGANIC_SALES_PATH, sales)
    return jsonify({"ok": True, "sale": sale})


@sales_bp.route("/api/organic/sales/<sale_id>", methods=["DELETE"])
@manager_required
def delete_organic_sale(sale_id):
    """Restore quantity back to the FG entries the sale drew from.
    Handles both new (lots[] array) and legacy (single fg_id) sale shapes."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"success": True})  # Already gone

    _restore_sale_lots(fg, sale)

    sales = [s for s in sales if s.get("id") != sale_id]
    _save_json(app.ORGANIC_SALES_PATH, sales)
    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})


def _packing_slip_data(sale_id):
    """Everything the packing slip shows for a sale's whole order, or None if
    the sale does not exist. ONE read for both renderings — the PDF (email,
    desktop) and the HTML page (prints from a phone) — so they can never differ."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return None

    # Collect all lines for this order (order_id groups multi-SKU transactions)
    order_id = sale.get("order_id")
    if order_id:
        order_lines = [s for s in sales if s.get("order_id") == order_id]
        order_lines.sort(key=lambda s: s.get("created_at", ""))
    else:
        order_lines = [sale]  # legacy single-line sale

    company = _load_company_info()
    buyers = app._load_buyers()
    buyer_name = sale.get("buyer") or "—"
    buyer_rec = next((b for b in buyers if b.get("name") == buyer_name), {})
    buyer_address = sale.get("location_address") or buyer_rec.get("address") or ""
    buyer_contact = sale.get("location_name") or ""
    buyer_phone = buyer_rec.get("phone") or ""
    buyer_email = buyer_rec.get("email") or ""

    # One row per LOT drawn (organic and new-shape sales), else one per line.
    lines = []
    for line in order_lines:
        product = ((line.get("brand","")+" " if line.get("brand") else "") + (line.get("recipe") or "")).strip()
        fmt = line.get("format") or ""
        for lot in (line.get("lots") or [{"lot": line.get("fg_lot"), "quantity": line.get("quantity")}]):
            lines.append({"product": product, "format": fmt,
                          "lot": lot.get("lot") or "—", "qty": int(lot.get("quantity") or 0)})
    total_units = sum(l["qty"] for l in lines)

    sale_date = sale.get("sale_date") or "—"
    safe_buyer = "".join(c for c in buyer_name if c.isalnum() or c in "-_ ")[:20]
    return {
        "sale_id": sale_id,
        "company": company,
        "buyer_name": buyer_name, "buyer_contact": buyer_contact,
        "buyer_address": buyer_address, "buyer_phone": buyer_phone,
        "buyer_email": buyer_email,
        "sale_date": sale_date,
        "po": sale.get("po_number") or sale.get("case_lot") or "—",
        "ref": sale_id[-10:],
        "lines": lines,
        "total_units": total_units,
        "total_cases": total_units // 12,
        "filename": f"packing-slip-{safe_buyer}-{sale_date}.pdf",
    }


def _packing_slip_pdf(sale_id):
    """(pdf_bytes, filename) of the packing slip for a sale's whole order, or
    None if the sale does not exist. Used by the Organic Sale email and the
    PDF download; reads `_packing_slip_data`, as the HTML slip does."""
    d = _packing_slip_data(sale_id)
    if d is None:
        return None
    company = d["company"]
    buyer_name = d["buyer_name"]
    buyer_contact = d["buyer_contact"]
    buyer_address = d["buyer_address"]
    buyer_phone = d["buyer_phone"]
    buyer_email = d["buyer_email"]

    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable, Image)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
    import io as _io

    # Sized for the 4in x 6in THERMAL label printer, not letter: a letter page
    # shrunk to fit a label prints at ~47% and is unreadable. Thermal heads
    # print black or nothing, so there is no grey text and no tinted fill —
    # weight and rules carry the hierarchy. A long order runs onto a second
    # label (the column header repeats; rows never split).
    BLACK   = colors.black
    # SimpleDocTemplate's frame pads 6pt inside the margin, so the paper
    # margin is MARGIN + 6pt (~0.16in) and tables must fit the INNER width.
    MARGIN  = 0.08 * inch
    WIDTH   = 4 * inch - 2 * (MARGIN + 6)

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(4 * inch, 6 * inch),
                            rightMargin=MARGIN, leftMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN)
    styles = getSampleStyleSheet()

    def _ps(name, **kw):
        base = styles.get(name, styles["Normal"])
        kw.setdefault("textColor", BLACK)
        return ParagraphStyle("_"+name+"_"+str(abs(hash(str(kw)))), parent=base, **kw)

    story = []

    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")

    company_name = company.get("name") or "Soma Bone Broth"
    company_lines = [f"<b>{company_name}</b>"]
    for fld in ("address", "city", "phone", "email", "website"):
        if company.get(fld): company_lines.append(company[fld])
    company_para = Paragraph("<br/>".join(company_lines),
                             _ps("Normal", fontSize=7.5, alignment=TA_RIGHT, leading=9.5))

    if os.path.exists(logo_path):
        left = Image(logo_path, width=0.85*inch, height=0.6*inch, kind="proportional")
        left.hAlign = "LEFT"
    else:
        left = Paragraph(company_name, _ps("Normal", fontSize=13, fontName="Helvetica-Bold"))
    header_tbl = Table([[left, company_para]], colWidths=[1.1*inch, WIDTH - 1.1*inch])
    header_tbl.setStyle(TableStyle([
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN",        (0,0), (0,0),  "LEFT"),
        ("LEFTPADDING",  (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING",   (0,0), (-1,-1), 0),
        ("BOTTOMPADDING",(0,0), (-1,-1), 2),
    ]))
    story.append(header_tbl)
    story.append(HRFlowable(width="100%", thickness=2, color=BLACK, spaceBefore=2, spaceAfter=3))
    story.append(Paragraph("PACKING SLIP",
        _ps("Normal", fontSize=14, fontName="Helvetica-Bold", leading=16, spaceAfter=3)))

    # Ship To, then Date / Ref / PO on one row beneath it
    sale_date = d["sale_date"]
    po        = d["po"]
    ref       = d["ref"]

    lbl = _ps("Normal", fontSize=7, fontName="Helvetica-Bold", leading=9)
    val = _ps("Normal", fontSize=10, leading=12)

    ship_lines = [f'<font size="12"><b>{buyer_name}</b></font>']
    if buyer_contact: ship_lines.append(buyer_contact)
    if buyer_address: ship_lines.append(buyer_address)
    if buyer_phone:   ship_lines.append(buyer_phone)
    if buyer_email:   ship_lines.append(buyer_email)

    order_info = Table([
        [Paragraph("SHIP TO", lbl), "", ""],
        [Paragraph("<br/>".join(ship_lines), _ps("Normal", fontSize=10, leading=13)), "", ""],
        [Paragraph("DATE", lbl), Paragraph("ORDER REF", lbl), Paragraph("PO #", lbl)],
        [Paragraph(sale_date, val), Paragraph(ref, val), Paragraph(po, val)],
    ], colWidths=[1.15*inch, 1.35*inch, WIDTH - 2.5*inch])
    order_info.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("SPAN",          (0,0), (2,0)),
        ("SPAN",          (0,1), (2,1)),
        ("BOX",           (0,0), (-1,-1), 1, BLACK),
        ("LINEABOVE",     (0,2), (-1,2),  1, BLACK),
        ("LINEBEFORE",    (1,2), (2,3),   0.5, BLACK),
        ("TOPPADDING",    (0,0), (-1,-1), 2),
        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
        ("BOTTOMPADDING", (0,1), (-1,1),  4),
        ("LEFTPADDING",   (0,0), (-1,-1), 4),
        ("RIGHTPADDING",  (0,0), (-1,-1), 4),
    ]))
    story.append(order_info)
    story.append(Spacer(1, 0.08*inch))

    hdr_l  = _ps("Normal", fontSize=7.5, fontName="Helvetica-Bold", alignment=TA_LEFT)
    hdr_c  = _ps("Normal", fontSize=7.5, fontName="Helvetica-Bold", alignment=TA_CENTER)
    cell_l = _ps("Normal", fontSize=10.5, leading=12.5, alignment=TA_LEFT)
    cell_c = _ps("Normal", fontSize=10.5, leading=12.5, alignment=TA_CENTER)
    qty_s  = _ps("Normal", fontSize=13, leading=15, fontName="Helvetica-Bold", alignment=TA_CENTER)
    tot_s  = _ps("Normal", fontSize=11, leading=13, fontName="Helvetica-Bold", alignment=TA_RIGHT)

    # FORMAT sits under the product name — a fourth column does not fit 4in.
    rows = [[Paragraph("PRODUCT", hdr_l), Paragraph("LOT #", hdr_c), Paragraph("QTY", hdr_c)]]
    for ln in d["lines"]:
        cell = Paragraph(f'<b>{ln["product"]}</b>'
                         + (f'<br/><font size="8.5">{ln["format"]}</font>' if ln["format"] else ""), cell_l)
        rows.append([cell, Paragraph(ln["lot"], cell_c), Paragraph(str(ln["qty"]), qty_s)])
    total_units = d["total_units"]
    total_cases = d["total_cases"]
    rows.append([Paragraph(f"TOTAL&nbsp;&nbsp;{total_units} units  ({total_cases} cases)", tot_s), "", ""])

    rc = len(rows)
    items_tbl = Table(rows, colWidths=[WIDTH - 1.75*inch, 1.1*inch, 0.65*inch], repeatRows=1)
    items_tbl.setStyle(TableStyle([
        ("LINEBELOW",     (0,0),  (-1,0),    1.5, BLACK),
        ("LINEBELOW",     (0,1),  (-1,rc-2), 0.5, BLACK),
        ("SPAN",          (0,-1), (-1,-1)),
        ("LINEABOVE",     (0,-1), (-1,-1),   2, BLACK),
        ("TOPPADDING",    (0,0),  (-1,-1),   3),
        ("BOTTOMPADDING", (0,0),  (-1,-1),   3),
        ("LEFTPADDING",   (0,0),  (-1,-1),   2),
        ("RIGHTPADDING",  (0,0),  (-1,-1),   2),
        ("VALIGN",        (0,0),  (-1,-1),   "MIDDLE"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 0.1*inch))

    footer = ["Thank you for your business."]
    if company.get("registration"): footer.append(f"Reg: {company['registration']}")
    story.append(Paragraph("  |  ".join(footer),
                            _ps("Normal", fontSize=7.5, alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue(), d["filename"]


@sales_bp.route("/sales/<sale_id>/packing-slip", methods=["GET"])
@manager_required
def packing_slip_page(sale_id):
    """The packing slip as a web page with a Print button. A PDF opened from
    the home-screen app on an iPhone has no share or print control, so the
    phone prints this page instead; the PDF stays for email and download."""
    d = _packing_slip_data(sale_id)
    if d is None:
        return "Sale not found", 404
    return render_template("sale_packing_slip.html", s=d,
                           today=datetime.now().strftime("%Y-%m-%d"))


@sales_bp.route("/api/organic/sales/<sale_id>/packing-slip", methods=["GET"])
@manager_required
def get_packing_slip(sale_id):
    """GET .../packing-slip - render a packing-slip PDF for a sale/order."""
    built = _packing_slip_pdf(sale_id)
    if built is None:
        return jsonify({"error": "Sale not found"}), 404
    import io as _io
    pdf, filename = built
    return send_file(_io.BytesIO(pdf), mimetype="application/pdf", as_attachment=False,
                     download_name=filename)


@sales_bp.route("/api/organic/sales/<sale_id>/qbo-csv", methods=["GET"])
@manager_required
def get_qbo_csv(sale_id):
    """GET .../qbo-csv - export a sale as a QuickBooks-importable CSV."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"error": "Sale not found"}), 404
    import csv
    import io as _io
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["InvoiceNo","Customer","InvoiceDate","DueDate",
                     "Item(Product/Service)","ItemQuantity","ItemRate","ItemAmount","Memo"])
    lots = sale.get("lots") or []
    product = ((sale.get("brand","")+" " if sale.get("brand") else "") +
               (sale.get("recipe") or "") +
               (" "+sale.get("format") if sale.get("format") else "")).strip()
    lot_str = ", ".join(l.get("lot","") for l in lots if l.get("lot"))
    total_qty = sum(int(l.get("quantity") or 0) for l in lots)
    writer.writerow([sale_id[-8:].upper(), sale.get("buyer",""),
                     sale.get("sale_date",""), sale.get("sale_date",""),
                     product, total_qty, "", "", f"LOT#: {lot_str}" if lot_str else ""])
    buf.seek(0)
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=invoice-{sale_id[-8:].upper()}.csv"})

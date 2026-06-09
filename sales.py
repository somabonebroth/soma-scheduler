"""sales.py — Organic sales blueprint extracted from app.py.

Fifth step of the app.py split (CLAUDE.md "Pending architectural work"). Scope:
the 7 organic-sales CRUD/document routes (/api/organic/sales*). NOT in scope:
channel imports (shopify/clover — deliberate near-dupes in app.py), sales
analytics (api_sales_by_buyer, backfill_sale_prices — future analytics
blueprint), api_settle_ripe_sale (Ripe), company-info, and the trace route —
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

Defines its own login_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, url_for, send_file

# Foundation layer (dependency-free) — imported directly, same as suppliers.py.
from helpers import (
    _load_json,
    _save_json,
    _sku_key,
    _add_contact,
    _load_company_info,
)

import app

sales_bp = Blueprint("sales", __name__)


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


@sales_bp.route("/api/organic/sales", methods=["GET"])
@login_required
def get_organic_sales():
    """Return sales records. Optional query param ?certification=X filters
    to that tier. Sale records carry the certification of the SKU sold,
    derived from the recipe at creation time."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    cert_filter = (request.args.get("certification") or "").strip()
    if cert_filter:
        sales = [s for s in sales
                 if (s.get("certification") or "").lower() == cert_filter.lower()]
    return jsonify(sales)


@sales_bp.route("/api/organic/sales", methods=["POST"])
@login_required
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


@sales_bp.route("/api/organic/sales/order", methods=["POST"])
@login_required
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
    sale_date    = data.get("sale_date") or datetime.now().date().isoformat()
    po_number    = (data.get("po_number") or "").strip()
    location_name    = (data.get("location_name") or "").strip()
    location_address = (data.get("location_address") or "").strip()

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

        sale_cert = ""
        for lot_entry in sale_lots:
            for b in (lot_entry.get("breakdown") or []):
                target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                if target and target.get("certification"):
                    sale_cert = target["certification"]
                    break
            if sale_cert:
                break

        line_total = round(unit_price * quantity, 2) if unit_price is not None else None
        sale = {
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
            "buyer":       buyer,
            "buyer_id":    data.get("buyer_id", ""),
            "sale_date":   sale_date,
            "po_number":   po_number,
            "location_name":    location_name,
            "location_address": location_address,
            "unit_price":  unit_price,
            "line_total":  line_total,
            "created_at":  datetime.now().isoformat(),
        }
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


@sales_bp.route("/api/organic/sales/<sale_id>", methods=["PATCH"])
@login_required
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
        delta = new_qty - old_qty

        if delta > 0:
            # Need to deduct more — FIFO from same SKU
            sku_key = sale.get("sku_key", "")
            candidates = [
                f for f in fg
                if _sku_key(f.get("brand",""), f.get("recipe",""), f.get("format","")) == sku_key
                and int(f.get("quantity_remaining") or 0) > 0
            ]
            candidates.sort(key=lambda e: (e.get("created_at",""), e.get("lot","")))
            remaining = delta
            for entry in candidates:
                if remaining <= 0:
                    break
                avail = int(entry.get("quantity_remaining") or 0)
                take = min(avail, remaining)
                entry["quantity_remaining"] = avail - take
                remaining -= take
            if remaining > 0:
                return jsonify({"error": f"Insufficient FG stock for delta of +{delta} units"}), 422

        elif delta < 0:
            # Restore units — put back into the most recent LOT drawn
            restore = abs(delta)
            lots = sale.get("lots") or []
            for lot_info in reversed(lots):
                if restore <= 0:
                    break
                for fg_entry in fg:
                    if fg_entry.get("lot") == lot_info.get("lot") and restore > 0:
                        fg_entry["quantity_remaining"] = int(fg_entry.get("quantity_remaining") or 0) + restore
                        restore = 0
                        break
            # If we couldn't trace back to a specific LOT, restore to most recent entry
            if restore > 0:
                sku_key = sale.get("sku_key", "")
                matching = [f for f in fg if _sku_key(f.get("brand",""), f.get("recipe",""), f.get("format","")) == sku_key]
                if matching:
                    matching.sort(key=lambda e: e.get("created_at",""), reverse=True)
                    matching[0]["quantity_remaining"] = int(matching[0].get("quantity_remaining") or 0) + restore

        sale["quantity"] = new_qty
        _save_json(app.ORGANIC_FG_PATH, fg)

    sale["edited_at"] = datetime.now().isoformat()
    sales[idx] = sale
    _save_json(app.ORGANIC_SALES_PATH, sales)
    return jsonify({"ok": True, "sale": sale})


@sales_bp.route("/api/organic/sales/<sale_id>", methods=["DELETE"])
@login_required
def delete_organic_sale(sale_id):
    """Restore quantity back to the FG entries the sale drew from.
    Handles both new (lots[] array) and legacy (single fg_id) sale shapes."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"success": True})  # Already gone

    if sale.get("lots"):
        for lot_entry in sale["lots"]:
            qty_to_restore = int(lot_entry.get("quantity") or 0)
            if qty_to_restore <= 0:
                continue
            # Preferred: per-fg_id breakdown (post-2026-05 sales) — restores
            # exactly to where each unit was deducted from.
            breakdown = lot_entry.get("breakdown")
            if breakdown:
                for b in breakdown:
                    target = next((f for f in fg if f.get("id") == b.get("fg_id")), None)
                    if target:
                        target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(b.get("quantity") or 0)
                continue
            # Fallback: legacy lot record without breakdown — restore everything
            # to the first matching fg_id (best-effort; preserves SKU total).
            fg_ids = lot_entry.get("fg_ids") or []
            for fid in fg_ids:
                target = next((f for f in fg if f.get("id") == fid), None)
                if target:
                    target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + qty_to_restore
                    break
    elif sale.get("fg_id"):
        # Pre-multi-LOT legacy shape
        target = next((f for f in fg if f.get("id") == sale["fg_id"]), None)
        if target:
            target["quantity_remaining"] = int(target.get("quantity_remaining") or 0) + int(sale.get("quantity") or 0)

    sales = [s for s in sales if s.get("id") != sale_id]
    _save_json(app.ORGANIC_SALES_PATH, sales)
    _save_json(app.ORGANIC_FG_PATH, fg)
    return jsonify({"success": True})


@sales_bp.route("/api/organic/sales/<sale_id>/packing-slip", methods=["GET"])
@login_required
def get_packing_slip(sale_id):
    """GET .../packing-slip - render a packing-slip PDF for a sale/order."""
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    sale = next((s for s in sales if s.get("id") == sale_id), None)
    if not sale:
        return jsonify({"error": "Sale not found"}), 404

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

    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, HRFlowable, Image)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
    import io as _io

    DARK_GREEN  = colors.HexColor("#1b5e20")
    MID_GREEN   = colors.HexColor("#2e7d32")
    LIGHT_GREEN = colors.HexColor("#e8f5e9")
    BORDER      = colors.HexColor("#c8d8c8")
    GREY_TEXT   = colors.HexColor("#555555")
    LIGHT_ROW   = colors.HexColor("#f5f9f5")

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=0.65*inch, leftMargin=0.65*inch,
                            topMargin=0.55*inch, bottomMargin=0.65*inch)
    styles = getSampleStyleSheet()

    def _ps(name, **kw):
        base = styles.get(name, styles["Normal"])
        return ParagraphStyle("_"+name+"_"+str(abs(hash(str(kw)))), parent=base, **kw)

    story = []

    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(__file__), "static", "logo.png")

    company_lines = [company.get("name") or "Soma Bone Broth"]
    for fld in ("address", "city", "phone", "email", "website"):
        if company.get(fld): company_lines.append(company[fld])

    company_para = Paragraph(
        "<br/>".join(company_lines),
        _ps("Normal", fontSize=8, textColor=GREY_TEXT, alignment=TA_RIGHT, leading=12)
    )

    if os.path.exists(logo_path):
        logo_img = Image(logo_path, width=1.4*inch, height=1.4*inch, kind="proportional")
        header_tbl = Table([[logo_img, company_para]], colWidths=[2.5*inch, 5.0*inch])
        header_tbl.setStyle(TableStyle([
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("ALIGN",  (1,0), (1,0),  "RIGHT"),
        ]))
    else:
        header_tbl = Table(
            [[Paragraph(company.get("name") or "Soma Bone Broth",
                        _ps("Normal", fontSize=18, textColor=DARK_GREEN, fontName="Helvetica-Bold")),
              company_para]],
            colWidths=[3.0*inch, 4.5*inch]
        )
    story.append(header_tbl)
    story.append(Spacer(1, 0.1*inch))
    story.append(HRFlowable(width="100%", thickness=2, color=MID_GREEN, spaceAfter=6))
    story.append(Paragraph("PACKING SLIP",
        _ps("Normal", fontSize=20, fontName="Helvetica-Bold", textColor=DARK_GREEN, spaceAfter=2)))

    # Ship To / Order Info block
    sale_date = sale.get("sale_date") or "—"
    po        = sale.get("po_number") or sale.get("case_lot") or "—"
    ref       = sale_id[-10:]

    lbl   = _ps("Normal", fontSize=8, textColor=GREY_TEXT, fontName="Helvetica-Bold", leading=11, spaceBefore=2)
    val   = _ps("Normal", fontSize=10, leading=13)
    val_s = _ps("Normal", fontSize=9, textColor=GREY_TEXT, leading=12)

    ship_lines = [f"<b>{buyer_name}</b>"]
    if buyer_contact: ship_lines.append(buyer_contact)
    if buyer_address: ship_lines.append(buyer_address)
    if buyer_phone:   ship_lines.append(buyer_phone)
    if buyer_email:   ship_lines.append(buyer_email)
    ship_para = Paragraph("<br/>".join(ship_lines), val)

    order_info = Table([
        [Paragraph("SHIP TO", lbl), ship_para,
         Paragraph("DATE",      lbl), Paragraph(sale_date, val)],
        ["", "", Paragraph("ORDER REF", lbl), Paragraph(ref, val_s)],
        ["", "", Paragraph("PO #",      lbl), Paragraph(po,  val_s)],
    ], colWidths=[0.9*inch, 3.6*inch, 1.0*inch, 2.0*inch])
    order_info.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("SPAN",          (0,0), (0,2)),
        ("SPAN",          (1,0), (1,2)),
        ("BACKGROUND",    (0,0), (1,2),  LIGHT_GREEN),
        ("BOX",           (0,0), (1,2),  0.5, BORDER),
        ("BOX",           (2,0), (3,2),  0.5, BORDER),
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(Spacer(1, 0.15*inch))
    story.append(order_info)
    story.append(Spacer(1, 0.2*inch))

    hdr_s  = _ps("Normal", fontSize=9, fontName="Helvetica-Bold", textColor=colors.white, alignment=TA_CENTER)
    cell_l = _ps("Normal", fontSize=10, alignment=TA_LEFT)
    cell_c = _ps("Normal", fontSize=10, alignment=TA_CENTER)
    tot_s  = _ps("Normal", fontSize=10, fontName="Helvetica-Bold", alignment=TA_CENTER)

    rows = [[Paragraph("PRODUCT", hdr_s), Paragraph("FORMAT", hdr_s),
             Paragraph("LOT #", hdr_s),   Paragraph("QTY (units)", hdr_s)]]
    total_units = 0

    for line in order_lines:
        lp  = ((line.get("brand","")+" " if line.get("brand") else "") + (line.get("recipe") or "")).strip()
        lf  = line.get("format") or ""
        ll  = line.get("lots") or []
        if ll:
            for lot in ll:
                qty = int(lot.get("quantity") or 0)
                total_units += qty
                rows.append([Paragraph(lp, cell_l), Paragraph(lf, cell_c),
                             Paragraph(lot.get("lot") or "—", cell_c), Paragraph(str(qty), cell_c)])
        else:
            qty = int(line.get("quantity") or 0)
            total_units += qty
            rows.append([Paragraph(lp, cell_l), Paragraph(lf, cell_c),
                         Paragraph(line.get("fg_lot") or "—", cell_c), Paragraph(str(qty), cell_c)])
    total_cases = total_units // 12
    rows.append(["", "", Paragraph("TOTAL", tot_s),
                 Paragraph(f"{total_units} units  ({total_cases} cases)", tot_s)])

    rc = len(rows)
    items_tbl = Table(rows, colWidths=[3.2*inch, 1.1*inch, 1.4*inch, 1.8*inch])
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),    MID_GREEN),
        ("TEXTCOLOR",     (0,0),  (-1,0),    colors.white),
        ("ROWBACKGROUNDS",(0,1),  (-1,rc-2), [colors.white, LIGHT_ROW]),
        ("BACKGROUND",    (0,-1), (-1,-1),   LIGHT_GREEN),
        ("GRID",          (0,0),  (-1,rc-2), 0.5, BORDER),
        ("LINEABOVE",     (0,-1), (-1,-1),   1.0, MID_GREEN),
        ("TOPPADDING",    (0,0),  (-1,-1),   8),
        ("BOTTOMPADDING", (0,0),  (-1,-1),   8),
        ("LEFTPADDING",   (0,0),  (-1,-1),   8),
        ("RIGHTPADDING",  (0,0),  (-1,-1),   8),
        ("VALIGN",        (0,0),  (-1,-1),   "MIDDLE"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 0.3*inch))

    story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=8))
    footer = ["Thank you for your business."]
    if company.get("registration"): footer.append(f"Reg: {company['registration']}")
    story.append(Paragraph("  |  ".join(footer),
                            _ps("Normal", fontSize=8, textColor=GREY_TEXT, alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    safe_buyer = "".join(c for c in buyer_name if c.isalnum() or c in "-_ ")[:20]
    return send_file(buf, mimetype="application/pdf", as_attachment=False,
                     download_name=f"packing-slip-{safe_buyer}-{sale_date}.pdf")


@sales_bp.route("/api/organic/sales/<sale_id>/qbo-csv", methods=["GET"])
@login_required
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

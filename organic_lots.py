"""organic_lots.py — Organic Lots: one folder per organic LOT# (2026-09-30).

The place to go for an organic batch: what came IN (the invoice of every
delivery whose raw lots the batch used), what was MADE (per SKU), and what went
OUT (every sale with its whole order, every recorded reduction), ending in what
is still HELD. A folder is Held while any jar remains and Closed at zero.

READ-ONLY. It creates nothing and writes nothing: every line is re-read from
the records that already exist (finished goods, production runs, sales, the
adjustments log, raw materials), so a folder can never disagree with them.

Only certified-Organic finished goods get folders (certification == "Organic",
the same test as everywhere else). Jars are joined to sales and reductions by
fg_id, never by lot string, so a lot number shared by two SKUs stays apart.

`build_folders` is pure (records in, folders out) so tests drive it directly;
the routes only load files and serialise.
"""
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from helpers import ADJUSTMENTS_PATH, ORGANIC_RUNS_PATH, _delivery_id, _load_json, _sku_display, _sku_key

import app

organic_lots_bp = Blueprint("organic_lots", __name__)


def manager_required(f):
    """Local copy of app.py's manager_required (verbatim) — decorators apply at
    import time, before app.py has finished defining its own."""
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


# How stock that did NOT come from a production run got into a lot.
_ADDED_LABELS = {
    "reset_baseline": "Opening count",
    "audit_baseline": "Stock count found extra",
    "migration_baseline": "Opening count",
    "manual_addition": "Added by hand",
}


def _is_organic(entry):
    return (entry.get("certification") or "").strip().lower() == "organic"


def _added_kind(f):
    if f.get("source") in ("reset_baseline", "audit_baseline"):
        return f["source"]
    if f.get("migration_baseline"):
        return "migration_baseline"
    if f.get("manual_addition"):
        return "manual_addition"
    return "manual_addition"


def _day(value):
    """The YYYY-MM-DD part of an ISO date/datetime string, or ''."""
    return (value or "")[:10]


def _batch_date_from_lot(lot):
    """A production LOT# is start date + 365 (ddmmyy), so step back 365 days."""
    try:
        return (datetime.strptime(lot, "%d%m%y") - timedelta(days=365)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _run_start(run):
    try:
        return (datetime.strptime(run.get("week_id"), "%Y-%m-%d")
                + timedelta(days=int(run.get("day_idx")))).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _sale_draws(sale, fg_ids):
    """{fg_id: jars} this sale took from the given FG entries."""
    out = {}
    for lot_entry in sale.get("lots") or []:
        breakdown = lot_entry.get("breakdown")
        if breakdown:
            for b in breakdown:
                if b.get("fg_id") in fg_ids:
                    out[b["fg_id"]] = out.get(b["fg_id"], 0) + int(b.get("quantity") or 0)
        else:
            ids = lot_entry.get("fg_ids") or []
            if len(ids) == 1 and ids[0] in fg_ids:
                out[ids[0]] = out.get(ids[0], 0) + int(lot_entry.get("quantity") or 0)
    if not sale.get("lots") and sale.get("fg_id") in fg_ids:
        out[sale["fg_id"]] = int(sale.get("quantity") or 0)
    return out


def build_folders(fg, runs, sales, adjustments, materials, photo_ids):
    """Every organic LOT# as a folder dict, Held first, newest batch first.

    photo_ids: the set of raw-material entry ids that have an invoice photo.
    """
    by_lot = {}
    for f in fg:
        if _is_organic(f) and f.get("lot"):
            by_lot.setdefault(f["lot"], []).append(f)

    runs_by_id = {r.get("id"): r for r in runs}
    mats_by_id = {m.get("id"): m for m in materials}
    orders = {}
    for s in sales:
        orders.setdefault(s.get("order_id") or s.get("id"), []).append(s)

    folders = []
    for lot, entries in by_lot.items():
        fg_to_sku = {}
        skus = {}
        for f in entries:
            key = _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
            fg_to_sku[f.get("id")] = key
            s = skus.setdefault(key, {
                "sku_key": key,
                "name": _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")),
                "brand": f.get("brand", ""), "recipe": f.get("recipe", ""), "format": f.get("format", ""),
                "made": 0, "added": 0, "sold": 0, "reduced": 0, "held": 0, "hot_cups": 0,
            })
            if f.get("run_id"):
                s["made"] += int(f.get("quantity_produced") or 0)
                # Jars counted by the kitchen beyond full cases: never organic
                # stock, sold as hot cups under a non-certified label.
                s["hot_cups"] += int(f.get("loose_jars") or 0)
            else:
                s["added"] += int(f.get("quantity_produced") or 0)
            s["held"] += int(f.get("quantity_remaining") or 0)
        fg_ids = set(fg_to_sku)

        # IN: where stock came from — production batches (with the invoices of
        # the raw lots they used) and any stock added outside production.
        batches, invoices, no_lot = [], {}, []
        for run_id in sorted({f.get("run_id") for f in entries if f.get("run_id")}):
            run = runs_by_id.get(run_id)
            if not run:
                continue
            batches.append({"run_id": run_id, "brand": run.get("brand", ""), "recipe": run.get("recipe", ""),
                            "vessel": run.get("vessel", ""), "start_date": _run_start(run)})
            for line in run.get("ingredients_used") or []:
                rid = line.get("raw_material_id")
                used = {"item": line.get("item", ""), "supplier_lot": line.get("supplier_lot", ""),
                        "quantity": line.get("quantity_used"), "unit": line.get("unit", ""),
                        "vessel": run.get("vessel", "")}
                if not rid:
                    no_lot.append(used)
                    continue
                mat = mats_by_id.get(rid) or {}
                if mat.get("migration_baseline"):
                    key, photo = "opening:" + rid, None
                else:
                    did = _delivery_id(mat) if mat else None
                    key = did or ("lot:" + rid)
                    photo = did if did in photo_ids else (rid if rid in photo_ids else None)
                inv = invoices.setdefault(key, {
                    "supplier": line.get("supplier") or mat.get("supplier", ""),
                    "date_received": line.get("date_received") or mat.get("date_received", ""),
                    "opening_count": bool(mat.get("migration_baseline")),
                    "photo_id": photo, "lines": [],
                })
                inv["photo_id"] = inv["photo_id"] or photo
                inv["lines"].append(used)
        added = [{"sku": skus[fg_to_sku[f.get("id")]]["name"], "brand": f.get("brand", ""),
                  "recipe": f.get("recipe", ""), "format": f.get("format", ""),
                  "label": _ADDED_LABELS[_added_kind(f)],
                  "quantity": int(f.get("quantity_produced") or 0),
                  "date": _day(f.get("reset_at") or f.get("created_at"))}
                 for f in entries if not f.get("run_id")]

        # OUT: sales (with their whole order, as the packing slip shows it)
        # and recorded reductions.
        out = []
        for order_lines in orders.values():
            here = {}
            for s in order_lines:
                for fid, q in _sale_draws(s, fg_ids).items():
                    here[fid] = here.get(fid, 0) + q
            if not here:
                continue
            for fid, q in here.items():
                skus[fg_to_sku[fid]]["sold"] += q
            first = sorted(order_lines, key=lambda s: s.get("created_at", ""))[0]
            lines = []
            for s in sorted(order_lines, key=lambda s: s.get("created_at", "")):
                lines.append({
                    "name": _sku_display(s.get("brand", ""), s.get("recipe", ""), s.get("format", "")),
                    "brand": s.get("brand", ""), "recipe": s.get("recipe", ""), "format": s.get("format", ""),
                    "quantity": int(s.get("quantity") or 0),
                    "lots": [le.get("lot", "") for le in (s.get("lots") or []) if le.get("lot")]
                            or ([s["fg_lot"]] if s.get("fg_lot") else []),
                    "from_this_lot": sum(_sale_draws(s, fg_ids).values()),
                })
            out.append({
                "kind": "sale",
                "date": _day(first.get("deducted_at") or first.get("created_at") or first.get("sale_date")),
                "delivery_date": first.get("sale_date", ""),
                "buyer": first.get("buyer", ""),
                "po_number": first.get("po_number", ""),
                "order_id": first.get("order_id", ""),
                "slip_sale_id": first.get("id"),
                "jars": sum(here.values()),
                "lines": lines,
            })
        for adj in adjustments:
            if adj.get("kind") == "subtract":
                took = {}
                for d in adj.get("drained") or []:
                    if d.get("fg_id") in fg_ids:
                        took[d["fg_id"]] = took.get(d["fg_id"], 0) + int(d.get("quantity") or 0)
                for fid, q in took.items():
                    skus[fg_to_sku[fid]]["reduced"] += q
                    out.append({"kind": "reduction", "date": _day(adj.get("created_at")),
                                "sku": skus[fg_to_sku[fid]]["name"], "brand": skus[fg_to_sku[fid]]["brand"],
                                "recipe": skus[fg_to_sku[fid]]["recipe"],
                                "format": skus[fg_to_sku[fid]]["format"], "jars": q,
                                "reason": adj.get("reason", ""), "notes": adj.get("notes", "")})
            elif adj.get("kind") == "lot_increase":
                # A recount that found more (the lot Edit Qty, 2026-10-01): IN, not OUT.
                for d in adj.get("added_to") or []:
                    if d.get("fg_id") in fg_ids:
                        s = skus[fg_to_sku[d["fg_id"]]]
                        q = int(d.get("quantity") or 0)
                        s["added"] += q
                        added.append({"sku": s["name"], "brand": s["brand"], "recipe": s["recipe"], "format": s["format"],
                                      "label": "Correction: " + (adj.get("reason") or "found more"),
                                      "quantity": q, "date": _day(adj.get("created_at"))})
            elif adj.get("kind") == "audit_fg" and adj.get("lot") == lot and int(adj.get("diff") or 0) < 0:
                key = _sku_key(adj.get("brand", ""), adj.get("recipe", ""), adj.get("format", ""))
                if key in skus:
                    q = -int(adj["diff"])
                    skus[key]["reduced"] += q
                    out.append({"kind": "reduction", "date": _day(adj.get("created_at")),
                                "sku": skus[key]["name"], "brand": skus[key]["brand"], "recipe": skus[key]["recipe"],
                                "format": skus[key]["format"], "jars": q,
                                "reason": "Stock count found fewer", "notes": ""})
        out.sort(key=lambda o: o["date"])

        # Anything the records above do not explain (e.g. a lot corrected by
        # hand, which leaves no log line) shows as its own number, so a folder
        # always adds up to what is on the shelf.
        for s in skus.values():
            s["unrecorded"] = s["made"] + s["added"] - s["sold"] - s["reduced"] - s["held"]
        sku_list = sorted(skus.values(), key=lambda s: s["name"])
        held = sum(s["held"] for s in sku_list)
        batch_date = min((b["start_date"] for b in batches if b["start_date"]), default="") \
            or _batch_date_from_lot(lot)
        folders.append({
            "lot": lot,
            "status": "held" if held > 0 else "closed",
            "batch_date": batch_date,
            "made": sum(s["made"] + s["added"] for s in sku_list),
            "held": held,
            "skus": sku_list,
            "batches": batches,
            "invoices": sorted(invoices.values(), key=lambda i: (i["date_received"], i["supplier"])),
            "no_lot": no_lot,
            "added": added,
            "out": out,
        })

    folders.sort(key=lambda f: f["batch_date"], reverse=True)
    folders.sort(key=lambda f: f["status"] != "held")
    return folders


def _load_folders():
    try:
        photo_ids = {fn.rsplit(".", 1)[0] for fn in os.listdir(app.RM_RECEIPT_PHOTOS_DIR)}
    except FileNotFoundError:
        photo_ids = set()
    return build_folders(
        _load_json(app.ORGANIC_FG_PATH, []),
        _load_json(ORGANIC_RUNS_PATH, []),
        _load_json(app.ORGANIC_SALES_PATH, []),
        _load_json(ADJUSTMENTS_PATH, []),
        _load_json(app.ORGANIC_RAW_PATH, []),
        photo_ids,
    )


@organic_lots_bp.route("/organic-lots")
@manager_required
def organic_lots_page():
    """The Organic Lots page: the folder list, and one folder opened via #LOT."""
    return render_template("organic_lots.html")


@organic_lots_bp.route("/api/organic-lots", methods=["GET"])
@manager_required
def get_organic_lots():
    """Every organic LOT# folder, whole (it is a handful of SKUs, so small)."""
    return jsonify({"folders": _load_folders()})

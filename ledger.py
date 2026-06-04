"""ledger.py — inventory event-ledger subsystem.

Increment 1 (read-only): a per-fg_id finished-goods reconciliation / drift
detector. It independently recomputes each FG entry's *expected* remaining stock
from the recorded movements (production inflow minus recorded sale + manual-
subtract outflows that reference that fg_id) and diffs it against the materialised
`quantity_remaining`. Any non-zero drift is a stock change that happened WITHOUT a
ledger record — the silent paths `update_finished_good` (direct set),
`adjust_lot_remaining` (lot delta), `edit_organic_sale` (in-place, breakdown not
rewritten), a non-restoring delete, or an unrecorded sale.

This is deliberately different from app._compute_mass_balance, which is SKU-level
and date-ranged: a per-fg check surfaces *offsetting* errors inside one SKU (a
silent +5 on lot A and -5 on lot B that net to zero at SKU level) and dangling
references (sales/adjustments pointing at a deleted fg_id). It is the embryo of the
event ledger's projection function and the scope-finder for the planned zero-day
reset.

PURELY READ-ONLY: it loads JSON and never writes. Follows the established blueprint
pattern (verbatim local login_required; foundation IO + sku helpers from helpers;
app.-qualified shared path constants via `import app`).
"""
import os
from datetime import datetime
from functools import wraps

from flask import (Blueprint, request, jsonify, session, redirect, url_for,
                   render_template)

from helpers import _load_json, ADJUSTMENTS_PATH, INVENTORY_DIR, _sku_key, _sku_display

import app

ledger_bp = Blueprint("ledger", __name__)


def login_required(f):
    """Local copy of app.py's decorator (verbatim) — keeps the blueprint free of
    an import-time dependency on app.py. Behaviour is identical."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def _int(v):
    """Defensive int coercion matching the codebase style (None/'' -> 0)."""
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def compute_fg_reconciliation():
    """Per-fg_id reconciliation of expected vs actual finished-goods stock.

    Returns a dict: {generated_at, entries[], skus[], orphans[], pending[],
    sale_mismatches[], totals{}}. Read-only — never writes.
    """
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    adjustments = _load_json(ADJUSTMENTS_PATH, [])

    fg_ids = {f.get("id") for f in fg}

    # fg_id -> recorded outflows that actually touched FG stock
    outflow = {}

    def out(fid):
        if fid not in outflow:
            outflow[fid] = {"sales": 0, "manual_sub": 0}
        return outflow[fid]

    orphans = []          # ledger references to a fg_id that no longer exists
    pending = []          # RIPE sales committed but not yet deducted
    sale_mismatches = []  # sale.quantity != sum(breakdown) — edit_organic_sale fingerprint

    # ---- Sales pass: attribute each sale's units to the fg_id(s) it drew from ----
    for s in sales:
        # Not-yet-due RIPE commitment: stock not drawn yet, so no fg outflow.
        if s.get("deducted") is False:
            pending.append({
                "sale_id": s.get("id"),
                "label": _sku_display(s.get("brand", ""), s.get("recipe", ""), s.get("format", "")),
                "quantity": _int(s.get("quantity")),
                "deduction_date": s.get("deduction_date") or s.get("sale_date") or "",
            })
            continue

        sid = s.get("id")
        squant = _int(s.get("quantity"))
        label = _sku_display(s.get("brand", ""), s.get("recipe", ""), s.get("format", ""))

        if s.get("lots"):
            bsum = 0
            for lot in s["lots"]:
                breakdown = lot.get("breakdown") or []
                if breakdown:
                    for b in breakdown:
                        fid = b.get("fg_id")
                        q = _int(b.get("quantity"))
                        bsum += q
                        if fid in fg_ids:
                            out(fid)["sales"] += q
                        else:
                            orphans.append({"kind": "sale_orphan_fg", "ref": sid,
                                            "fg_id": fid, "quantity": q})
                else:
                    # Legacy multi-lot without a per-fg breakdown: best-effort
                    # attribute the whole lot to the first fg_id (mirrors how
                    # delete_organic_sale itself restores these).
                    fids = lot.get("fg_ids") or []
                    lq = _int(lot.get("quantity"))
                    bsum += lq
                    if fids and fids[0] in fg_ids:
                        out(fids[0])["sales"] += lq
                    elif fids:
                        orphans.append({"kind": "sale_orphan_fg", "ref": sid,
                                        "fg_id": fids[0], "quantity": lq, "approx": True})
                    else:
                        orphans.append({"kind": "sale_no_target", "ref": sid,
                                        "quantity": lq, "label": label})
            if bsum != squant:
                sale_mismatches.append({"sale_id": sid, "label": label,
                                        "sale_quantity": squant, "breakdown_sum": bsum,
                                        "delta": squant - bsum})
        elif s.get("fg_id"):
            fid = s.get("fg_id")
            if fid in fg_ids:
                out(fid)["sales"] += squant
            else:
                orphans.append({"kind": "sale_orphan_fg", "ref": sid,
                                "fg_id": fid, "quantity": squant})
        else:
            orphans.append({"kind": "sale_no_target", "ref": sid,
                            "quantity": squant, "label": label})

    # ---- Adjustments pass: only manual subtracts reduce a specific fg_id ----
    # (manual 'add' created its own fg entry; 'audit_fg' is SKU-level and its
    #  per-fg effect, if any, is captured by last_adjusted_at on the entry.)
    for a in adjustments:
        if a.get("kind") == "subtract":
            for d in (a.get("drained") or []):
                fid = d.get("fg_id")
                q = _int(d.get("quantity"))
                if fid in fg_ids:
                    out(fid)["manual_sub"] += q
                else:
                    orphans.append({"kind": "adj_orphan_fg", "ref": a.get("id"),
                                    "fg_id": fid, "quantity": q})

    # ---- Per-fg reconciliation ----
    entries = []
    sku_agg = {}
    for f in fg:
        fid = f.get("id")
        o = outflow.get(fid, {"sales": 0, "manual_sub": 0})
        actual = _int(f.get("quantity_remaining"))

        produced_raw = f.get("quantity_produced")
        if produced_raw is None:
            # Never manufacture false drift on a legacy entry with no produced field.
            produced = actual + o["sales"] + o["manual_sub"]
            produced_inferred = True
        else:
            produced = _int(produced_raw)
            produced_inferred = False

        expected = produced - o["sales"] - o["manual_sub"]
        drift = actual - expected

        is_baseline = bool(f.get("migration_baseline") or f.get("manual_addition")
                           or f.get("source") == "audit_baseline")
        if drift == 0:
            cause = "ok"
        elif f.get("last_adjusted_at"):
            cause = "manual_adjust"   # update_finished_good / adjust_lot_remaining / audit drain
        elif is_baseline:
            cause = "baseline_drift"
        else:
            cause = "unexplained"     # unrecorded sale, non-restoring delete, edit-sale delta

        sku = _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        label = _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        cert = (f.get("certification") or "")
        entries.append({
            "fg_id": fid, "sku_key": sku, "label": label, "cert": cert,
            "lot": f.get("lot", ""), "vessel": f.get("vessel", ""),
            "produced": produced, "out_sales": o["sales"], "out_sub": o["manual_sub"],
            "expected": expected, "actual": actual, "drift": drift, "cause": cause,
            "flags": {
                "baseline": bool(f.get("migration_baseline")),
                "manual_addition": bool(f.get("manual_addition")),
                "audit_baseline": f.get("source") == "audit_baseline",
                "last_adjusted_at": f.get("last_adjusted_at") or None,
                "negative_stock": actual < 0,
                "produced_inferred": produced_inferred,
            },
        })

        g = sku_agg.get(sku)
        if g is None:
            g = sku_agg[sku] = {"sku_key": sku, "label": label, "cert": cert,
                                "produced": 0, "out_sales": 0, "out_sub": 0,
                                "expected": 0, "actual": 0, "drift": 0,
                                "attributed": 0, "unexplained": 0,
                                "entries": 0, "entries_with_drift": 0}
        g["produced"] += produced
        g["out_sales"] += o["sales"]
        g["out_sub"] += o["manual_sub"]
        g["expected"] += expected
        g["actual"] += actual
        g["drift"] += drift
        g["entries"] += 1
        if drift != 0:
            g["entries_with_drift"] += 1
            if cause == "unexplained":
                g["unexplained"] += drift
            else:
                g["attributed"] += drift

    skus = list(sku_agg.values())
    for g in skus:
        g["offsetting_internal_drift"] = (g["drift"] == 0 and g["entries_with_drift"] > 0)
    skus.sort(key=lambda x: (abs(x["unexplained"]), abs(x["drift"])), reverse=True)
    entries.sort(key=lambda x: (abs(x["drift"]), x["label"]), reverse=True)

    totals = {
        "entries": len(entries),
        "entries_with_drift": sum(1 for e in entries if e["drift"] != 0),
        "total_abs_drift": sum(abs(e["drift"]) for e in entries),
        "unexplained_entries": sum(1 for e in entries if e["cause"] == "unexplained"),
        "unexplained_units": sum(e["drift"] for e in entries if e["cause"] == "unexplained"),
        "orphans": len(orphans),
        "pending_units": sum(p["quantity"] for p in pending),
        "sale_mismatches": len(sale_mismatches),
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries, "skus": skus, "orphans": orphans,
        "pending": pending, "sale_mismatches": sale_mismatches, "totals": totals,
    }


# ── Increment 2: append-only event model + projection (read-only so far) ──
#
# The event log is the future source of truth: inventory becomes a *projection*
# over an immutable, append-only stream of movements, and destructive edits become
# reversal events instead of in-place mutations. Increment 2 lays the foundation —
# the schema, a pure projection fold, and a backfill that expresses the CURRENT
# records as that event stream — plus a verify harness proving the projection
# reproduces what the reconciliation independently computes. Nothing writes the log
# yet (it stays empty until the Increment 3 zero-day reset emits opening events).
#
# Event shape: {type, fg_id, sku_key, qty_delta (signed), source, ref, ts}.
EVENTS_PATH = os.path.join(INVENTORY_DIR, "inventory_events.json")

EV_OPENING = "opening"        # baseline / manual-add / audit-baseline inflow
EV_PRODUCTION = "production"  # completed-run inflow
EV_SALE = "sale"              # recorded sale outflow
EV_ADJUST_SUB = "adjust_sub"  # manual-subtract outflow


def _load_events():
    """Load the append-only inventory event log (empty until Increment 3)."""
    return _load_json(EVENTS_PATH, [])


def backfill_fg_events():
    """Express the CURRENT finished-goods records as the canonical event stream
    (in-memory; no writes). Each FG entry is an inflow — `opening` for baseline /
    manual-add / audit-baseline lots, else `production`; each recorded sale and
    manual-subtract is an outflow tied to the fg_id it drew from. This is the same
    movement set the reconciliation replays, expressed as first-class events — the
    model the ledger will persist once writes are flipped on (Increment 4)."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    adjustments = _load_json(ADJUSTMENTS_PATH, [])

    fg_sku = {f.get("id"): _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
              for f in fg}
    fg_ids = set(fg_sku)
    events = []

    def ev(typ, fid, delta, sku, src, ref, ts):
        events.append({"type": typ, "fg_id": fid, "qty_delta": delta,
                       "sku_key": sku, "source": src, "ref": ref, "ts": ts or ""})

    for f in fg:
        fid = f.get("id")
        sku = fg_sku[fid]
        produced = _int(f.get("quantity_produced"))
        if f.get("migration_baseline"):
            ev(EV_OPENING, fid, produced, sku, "baseline", f.get("lot", ""), f.get("created_at"))
        elif f.get("manual_addition"):
            ev(EV_OPENING, fid, produced, sku, "manual_add", f.get("lot", ""), f.get("created_at"))
        elif f.get("source") == "audit_baseline":
            ev(EV_OPENING, fid, produced, sku, "audit_baseline", f.get("lot", ""), f.get("created_at"))
        else:
            ev(EV_PRODUCTION, fid, produced, sku, "run", f.get("run_id") or "", f.get("created_at"))

    for s in sales:
        if s.get("deducted") is False:
            continue  # not-yet-due RIPE commitment — no stock moved yet
        sku = s.get("sku_key") or _sku_key(s.get("brand", ""), s.get("recipe", ""), s.get("format", ""))
        sid = s.get("id")
        ts = s.get("sale_date") or s.get("created_at")
        if s.get("lots"):
            for lot in s["lots"]:
                bd = lot.get("breakdown") or []
                if bd:
                    for b in bd:
                        if b.get("fg_id") in fg_ids:
                            ev(EV_SALE, b.get("fg_id"), -_int(b.get("quantity")), sku, "sale", sid, ts)
                else:
                    fids = lot.get("fg_ids") or []
                    if fids and fids[0] in fg_ids:
                        ev(EV_SALE, fids[0], -_int(lot.get("quantity")), sku, "sale", sid, ts)
        elif s.get("fg_id") in fg_ids:
            ev(EV_SALE, s.get("fg_id"), -_int(s.get("quantity")), sku, "sale", sid, ts)

    for a in adjustments:
        if a.get("kind") == "subtract":
            for d in (a.get("drained") or []):
                fid = d.get("fg_id")
                if fid in fg_ids:
                    ev(EV_ADJUST_SUB, fid, -_int(d.get("quantity")), fg_sku.get(fid),
                       "manual_subtract", a.get("id"), a.get("created_at"))

    events.sort(key=lambda e: e["ts"])
    return events


def project_fg(events):
    """Fold an event stream into balances. Pure. Returns (by_fg_id, by_sku)."""
    by_fg, by_sku = {}, {}
    for e in events:
        d = _int(e.get("qty_delta"))
        fid = e.get("fg_id")
        if fid is not None:
            by_fg[fid] = by_fg.get(fid, 0) + d
        sku = e.get("sku_key")
        if sku:
            by_sku[sku] = by_sku.get(sku, 0) + d
    return by_fg, by_sku


def verify_fg_projection():
    """Prove the event model is faithful: project the backfilled event stream and
    confirm it reproduces, per fg_id, exactly what compute_fg_reconciliation derives
    independently (`projection_matches_reconciliation`). Then report how the
    projected balances compare to actual current stock — `lots_drifting` must equal
    the reconciliation's drift count. Read-only."""
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    events = backfill_fg_events()
    proj_fg, _ = project_fg(events)
    recon = compute_fg_reconciliation()
    expected_by_fg = {e["fg_id"]: e["expected"] for e in recon["entries"]}

    model_ok = True
    model_mismatches = []
    actual_match = 0
    drifting = 0
    for f in fg:
        fid = f.get("id")
        projected = proj_fg.get(fid, 0)
        expected = expected_by_fg.get(fid, 0)
        if projected != expected:
            model_ok = False
            model_mismatches.append({"fg_id": fid, "projected": projected,
                                     "reconciliation_expected": expected})
        if projected == _int(f.get("quantity_remaining")):
            actual_match += 1
        else:
            drifting += 1

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "on_disk_events": len(_load_events()),
        "backfill_events": len(events),
        "fg_lots": len(fg),
        "projection_matches_reconciliation": model_ok,
        "model_mismatches": model_mismatches[:20],
        "lots_matching_actual": actual_match,
        "lots_drifting": drifting,
    }


@ledger_bp.route("/admin/fg-reconcile")
@login_required
def fg_reconcile_page():
    """Render the read-only finished-goods reconciliation / drift-detector page."""
    return render_template("fg_reconcile.html")


@ledger_bp.route("/api/organic/fg-reconcile", methods=["GET"])
@login_required
def fg_reconcile_api():
    """Read-only per-fg_id reconciliation of expected vs actual FG stock."""
    return jsonify(compute_fg_reconciliation())


@ledger_bp.route("/api/organic/ledger/verify", methods=["GET"])
@login_required
def ledger_verify_api():
    """Read-only self-check that the append-only event projection reproduces the
    reconciliation (and reports projected-vs-actual drift)."""
    return jsonify(verify_fg_projection())

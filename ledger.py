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
pattern (verbatim local manager_required; foundation IO + sku helpers from helpers;
app.-qualified shared path constants via `import app`).
"""
import os
from datetime import datetime
from functools import wraps

from flask import (Blueprint, request, jsonify, session, redirect, url_for,
                   render_template)

from helpers import (_load_json, _save_json, ADJUSTMENTS_PATH, INVENTORY_DIR,
                     _sku_key, _sku_display, _prod_date)

import app

ledger_bp = Blueprint("ledger", __name__)


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

    # After a zero-day reset, the books are "closed" before the cutover date:
    # the reset baselines ARE the opening, so only movements on/after the cutover
    # are reconciled against them. None (no reset yet) => no filtering, so Inc 1/2
    # behaviour is unchanged.
    cutover = _reset_cutover_date()

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
        # Pre-reset sale: its effect is already baked into the counted opening.
        if cutover and (s.get("sale_date") or s.get("created_at") or "")[:10] < cutover:
            continue
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
        if cutover and (a.get("created_at") or "")[:10] < cutover:
            continue  # pre-reset adjustment — baked into the counted opening
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
        # Two-tier reading: organic SKUs are lot-controlled (lots captured at sale),
        # so per-lot drift is real. Everything else is SKU-level — its per-lot split
        # is a FIFO estimate, so judge those at the SKU total, not per lot.
        g["tier"] = "organic" if _is_org(g.get("cert")) else "sku"
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
EV_RESET = "reset"            # zero-day reset marker (carries the cutover)

RESET_ARCHIVE_DIR = os.path.join(INVENTORY_DIR, "ledger_archive")


def _load_events():
    """Load the append-only inventory event log (empty until the first reset)."""
    return _load_json(EVENTS_PATH, [])


def _reset_cutover_date():
    """Date (YYYY-MM-DD) of the most recent zero-day reset, or None if none yet.
    Before any reset this returns None, so reconciliation/backfill apply no
    period filtering and Increment 1/2 behaviour is unchanged."""
    latest = None
    for e in _load_events():
        if e.get("type") == EV_RESET:
            ts = (e.get("ts") or "")[:10]
            if ts and (latest is None or ts > latest):
                latest = ts
    return latest


def _reset_frozen_run_ids():
    """Run ids that were COMPLETED as of the most recent reset's cutover. Their
    output is already embodied in the reset baselines, so the production-completion
    chain must NOT regenerate their FG (that's what doubled inventory). Returns the
    set from the latest RESET event's meta (by full ts), or set() if there's no
    reset — or an older reset that predates this snapshot (no retroactive freeze)."""
    latest_ts = None
    latest_ev = None
    for e in _load_events():
        if e.get("type") == EV_RESET:
            ts = e.get("ts") or ""
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts, latest_ev = ts, e
    if not latest_ev:
        return set()
    return set((latest_ev.get("meta") or {}).get("frozen_run_ids") or [])


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

    cutover = _reset_cutover_date()
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
        if cutover and (s.get("sale_date") or s.get("created_at") or "")[:10] < cutover:
            continue  # pre-reset sale — baked into the counted opening
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
        if cutover and (a.get("created_at") or "")[:10] < cutover:
            continue  # pre-reset adjustment — baked into the counted opening
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


# ── Increment 3: TWO-TIER zero-day reset (preview read-only; apply WRITES) ──
#
# Traceability rigor is matched to requirement (the two-tier decision):
#   • Organic-certified SKUs (cert == "Organic") are counted/reset PER LOT — each
#     physical lot becomes one clean reset_baseline entry, preserving the lot code
#     and production date. Forward traceability is kept (organic is wholesale-only
#     and lot is captured at pack-out in a later increment).
#   • Everything else collapses to ONE total per SKU, tagged with the newest lot —
#     SKU-level books, backward-trace only.
# Old lots are archived, a RESET marker + opening events written, and the cutover
# filter closes everything before the reset. Apply archives first (recoverable),
# gated behind a confirmation token.


def _is_org(cert):
    """A SKU is in the lot-tracked tier iff it is organic-certified."""
    return (cert or "").strip().lower() == "organic"


def _current_stock():
    """One pass over the live FG file. Returns (by_lot, by_sku):
      by_lot[(sku, lot)] = {sku_key, lot, label, brand, recipe, format, cert,
                            current, prod_date}
      by_sku[sku]        = {sku_key, label, brand, recipe, format, cert,
                            current, lots, latest_lot, latest_date}
    Certs default to the recipe's certification when the entry omits one."""
    by_lot = {}
    for f in _load_json(app.ORGANIC_FG_PATH, []):
        sku = _sku_key(f.get("brand", ""), f.get("recipe", ""), f.get("format", ""))
        lot = (f.get("lot") or "")
        prod = _prod_date(f) or (f.get("created_at") or "")
        cert = (f.get("certification") or "").strip()
        r = by_lot.get((sku, lot))
        if r is None:
            r = by_lot[(sku, lot)] = {
                "sku_key": sku, "lot": lot,
                "label": _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")),
                "brand": f.get("brand", ""), "recipe": f.get("recipe", ""),
                "format": f.get("format", ""), "cert": cert, "current": 0, "prod_date": prod}
        r["current"] += _int(f.get("quantity_remaining"))
        if prod and (not r["prod_date"] or prod < r["prod_date"]):
            r["prod_date"] = prod
        if cert and not r["cert"]:
            r["cert"] = cert

    by_sku = {}
    for (sku, lot), r in by_lot.items():
        s = by_sku.get(sku)
        if s is None:
            s = by_sku[sku] = {"sku_key": sku, "label": r["label"], "brand": r["brand"],
                               "recipe": r["recipe"], "format": r["format"], "cert": r["cert"],
                               "current": 0, "lots": 0, "latest_lot": "", "latest_date": ""}
        s["current"] += r["current"]
        s["lots"] += 1
        if r["cert"] and not s["cert"]:
            s["cert"] = r["cert"]
        if (r["prod_date"] or "") >= (s["latest_date"] or ""):
            s["latest_date"] = r["prod_date"]
            s["latest_lot"] = lot or s["latest_lot"]

    recipes = None
    for s in by_sku.values():
        if not s["cert"]:
            if recipes is None:
                recipes = app.load_recipes()
            s["cert"] = (recipes.get(s["recipe"] or "", {}) or {}).get("certification", "").strip()
    for (sku, lot), r in by_lot.items():
        if not r["cert"]:
            r["cert"] = by_sku[sku]["cert"]
    return by_lot, by_sku


def _norm_counts(counts):
    """Coerce a list of {sku_key, lot, counted} into {(sku, lot): qty}.
    A blank lot ('') marks a SKU-level total (the non-organic tier)."""
    clean = {}
    for item in (counts or []):
        if not isinstance(item, dict):
            continue
        sku = (item.get("sku_key") or "").strip()
        if not sku:
            continue
        lot = (item.get("lot") or "").strip()
        try:
            q = int(item.get("counted"))
        except (ValueError, TypeError):
            continue
        clean[(sku, lot)] = max(0, q)
    return clean


def compute_reset_preview(counts):
    """Read-only preview of the two-tier reset. `counts` is a list of
    {sku_key, lot, counted}: organic SKUs send one entry per lot; non-organic send
    one entry with lot='' (the SKU total). Uncounted rows carry forward at current
    stock (never silently wiped). Returns {rows, summary}; each row carries enough
    metadata (lot, assign_date, brand/recipe/format/cert) for apply to build it."""
    cmap = _norm_counts(counts)
    by_lot, by_sku = _current_stock()

    lots_by_sku = {}
    for (sku, lot), r in by_lot.items():
        lots_by_sku.setdefault(sku, []).append((lot, r))

    recipes = None

    def cert_for(sku):
        if sku in by_sku:
            return by_sku[sku]["cert"]
        nonlocal recipes
        if recipes is None:
            recipes = app.load_recipes()
        recipe = (sku.split("|") + ["", "", ""])[1]
        return (recipes.get(recipe, {}) or {}).get("certification", "").strip()

    rows = []
    carried = 0
    new_lots = 0
    for sku in (set(by_sku) | {s for (s, l) in cmap}):
        cert = cert_for(sku)
        s = by_sku.get(sku)
        parts = (sku.split("|") + ["", "", ""])[:3]
        label = s["label"] if s else (_sku_display(*parts) or sku)
        if _is_org(cert):
            seen = set()
            for lot, r in sorted(lots_by_sku.get(sku, []), key=lambda x: x[0]):
                counted = (sku, lot) in cmap
                target = cmap[(sku, lot)] if counted else r["current"]
                rows.append({"tier": "organic", "sku_key": sku, "count_key": lot,
                             "lot": lot, "label": label, "cert": cert, "lots": 1,
                             "brand": r["brand"], "recipe": r["recipe"], "format": r["format"],
                             "current": r["current"], "counted": counted, "target": target,
                             "delta": target - r["current"], "assign_date": r["prod_date"],
                             "new_lot": False})
                seen.add(lot)
                if not counted and r["current"] != 0:
                    carried += 1
            for (csku, clot), q in cmap.items():
                if csku == sku and clot and clot not in seen:
                    rows.append({"tier": "organic", "sku_key": sku, "count_key": clot,
                                 "lot": clot, "label": label, "cert": cert, "lots": 0,
                                 "brand": parts[0], "recipe": parts[1], "format": parts[2],
                                 "current": 0, "counted": True, "target": q, "delta": q,
                                 "assign_date": "", "new_lot": True})
                    new_lots += 1
        else:
            counted = (sku, "") in cmap
            cur = s["current"] if s else 0
            target = cmap[(sku, "")] if counted else cur
            rows.append({"tier": "sku", "sku_key": sku, "count_key": "",
                         "lot": (s["latest_lot"] if s else ""), "label": label, "cert": cert,
                         "lots": (s["lots"] if s else 0),
                         "brand": (s["brand"] if s else parts[0]),
                         "recipe": (s["recipe"] if s else parts[1]),
                         "format": (s["format"] if s else parts[2]),
                         "current": cur, "counted": counted, "target": target,
                         "delta": target - cur, "assign_date": (s["latest_date"] if s else ""),
                         "new_lot": (s is None)})
            if not counted and cur != 0:
                carried += 1
    rows.sort(key=lambda r: (r["label"].lower(), r["lot"]))
    return {
        "rows": rows,
        "summary": {
            "organic_skus": len({r["sku_key"] for r in rows if r["tier"] == "organic"}),
            "organic_lots": sum(1 for r in rows if r["tier"] == "organic"),
            "other_skus": sum(1 for r in rows if r["tier"] == "sku"),
            "counted": sum(1 for r in rows if r["counted"]),
            "carried": carried,
            "new_lots": new_lots,
            "current_units": sum(r["current"] for r in rows),
            "target_units": sum(r["target"] for r in rows),
            "net_delta": sum(r["delta"] for r in rows),
            "already_reset": _reset_cutover_date(),
        },
    }


def apply_reset(counts, actor=""):
    """Apply the two-tier zero-day reset (WRITES). Archives the current inventory
    files, then replaces finished_goods.json with one reset_baseline entry per
    preview row (organic = per lot, non-organic = per SKU), and appends a RESET
    marker + opening events to the ledger. Each entry carries reset_tier so later
    increments can branch on it."""
    preview = compute_reset_preview(counts)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    cutover = now.isoformat(timespec="seconds")

    # Snapshot the runs that are COMPLETED as of the cutover — their FG output is
    # now embodied in the reset baselines, so the completion chain must freeze them
    # (never regenerate their FG, or it doubles on the next save/boot).
    runs = _load_json(app.ORGANIC_RUNS_PATH, [])
    frozen_run_ids = sorted({r.get("id") for r in runs
                             if r.get("status") == "completed" and r.get("id")})

    # 1. Archive current files (recoverable snapshot).
    os.makedirs(RESET_ARCHIVE_DIR, exist_ok=True)
    archived = []
    for name, path in [("finished_goods", app.ORGANIC_FG_PATH),
                       ("sales", app.ORGANIC_SALES_PATH),
                       ("adjustments", ADJUSTMENTS_PATH),
                       ("production_runs", app.ORGANIC_RUNS_PATH),
                       ("events", EVENTS_PATH)]:
        _save_json(os.path.join(RESET_ARCHIVE_DIR, f"{name}_{stamp}.json"),
                   _load_json(path, []))
        archived.append(f"{name}_{stamp}.json")

    # 2. Build the clean FG file from the preview rows (tier-agnostic at this point).
    new_fg = []
    opening_events = []
    for i, r in enumerate(preview["rows"]):
        target = r["target"]
        if target <= 0:
            continue
        fid = f"fg_reset_{stamp}_{i:03d}"
        lot = r["lot"] or ("RESET-" + now.strftime("%d%m%y"))
        new_fg.append({
            "id": fid, "brand": r["brand"], "recipe": r["recipe"], "format": r["format"],
            "certification": r["cert"], "lot": lot,
            "quantity_produced": target, "quantity_remaining": target,
            "vessel": "Reset baseline", "week_id": None, "day_idx": None,
            "created_at": r["assign_date"] or cutover,  # preserve lot's production date
            "reset_at": cutover, "source": "reset_baseline", "reset_tier": r["tier"],
        })
        opening_events.append({"type": EV_OPENING, "fg_id": fid, "qty_delta": target,
                               "sku_key": r["sku_key"], "lot": lot, "source": "reset",
                               "ref": stamp, "ts": cutover})

    # 3. Append RESET marker + opening events; replace the FG file.
    events = _load_json(EVENTS_PATH, [])
    events.append({"type": EV_RESET, "fg_id": None, "qty_delta": 0, "sku_key": None,
                   "source": "reset", "ref": stamp, "ts": cutover,
                   "meta": {"entries": len(opening_events), "actor": actor,
                            "frozen_run_ids": frozen_run_ids}})
    events.extend(opening_events)
    _save_json(app.ORGANIC_FG_PATH, new_fg)
    _save_json(EVENTS_PATH, events)

    return {"applied_at": cutover, "cutover": cutover[:10], "archive_stamp": stamp,
            "archived_files": archived, "entries_reset": len(opening_events),
            "organic_lots": sum(1 for r in preview["rows"]
                                if r["tier"] == "organic" and r["target"] > 0),
            "total_units": sum(e["qty_delta"] for e in opening_events),
            "fg_entries": len(new_fg)}


def list_reset_archives():
    """List archive snapshots (newest first): stamp + the FG entry count / units in
    each snapshot, so a restore shows exactly what it would bring back. Read-only."""
    out = []
    if not os.path.isdir(RESET_ARCHIVE_DIR):
        return out
    stamps = set()
    for name in os.listdir(RESET_ARCHIVE_DIR):
        if name.startswith("finished_goods_") and name.endswith(".json"):
            stamps.add(name[len("finished_goods_"):-len(".json")])
    for stamp in sorted(stamps, reverse=True):
        fg = _load_json(os.path.join(RESET_ARCHIVE_DIR, "finished_goods_" + stamp + ".json"), [])
        out.append({"stamp": stamp, "fg_entries": len(fg),
                    "units": sum(_int(e.get("quantity_remaining")) for e in fg)})
    return out


def restore_reset_archive(stamp):
    """Undo a reset: restore finished_goods + events from the archive snapshot
    `stamp` (the pre-reset state the reset saved). Re-archives the CURRENT files
    first (so a restore is itself reversible), then writes the snapshot back.
    sales/adjustments are not touched (the reset never modified them). WRITES."""
    fgpath = os.path.join(RESET_ARCHIVE_DIR, "finished_goods_" + stamp + ".json")
    if not os.path.exists(fgpath):
        raise ValueError("No archive snapshot " + stamp)

    # Safety: snapshot the current (corrupt) state before overwriting it.
    now = datetime.now()
    pre = now.strftime("pre_restore_%Y%m%d_%H%M%S")
    os.makedirs(RESET_ARCHIVE_DIR, exist_ok=True)
    _save_json(os.path.join(RESET_ARCHIVE_DIR, "finished_goods_" + pre + ".json"),
               _load_json(app.ORGANIC_FG_PATH, []))
    _save_json(os.path.join(RESET_ARCHIVE_DIR, "events_" + pre + ".json"),
               _load_json(EVENTS_PATH, []))

    fg = _load_json(fgpath, [])
    _save_json(app.ORGANIC_FG_PATH, fg)
    evpath = os.path.join(RESET_ARCHIVE_DIR, "events_" + stamp + ".json")
    if os.path.exists(evpath):
        _save_json(EVENTS_PATH, _load_json(evpath, []))
    return {"restored_stamp": stamp, "fg_entries": len(fg),
            "units": sum(_int(e.get("quantity_remaining")) for e in fg),
            "current_backed_up_as": pre}


@ledger_bp.route("/admin/fg-reset/archives", methods=["GET"])
@manager_required
def fg_reset_archives_api():
    """List restorable pre-reset snapshots."""
    return jsonify({"archives": list_reset_archives()})


@ledger_bp.route("/admin/fg-reset/restore", methods=["POST"])
@manager_required
def fg_reset_restore_api():
    """Restore finished goods from an archive snapshot (undo a reset). Gated."""
    data = request.json or {}
    if data.get("confirm") != "RESTORE":
        return jsonify({"error": "Confirmation token required"}), 400
    stamp = (data.get("stamp") or "").strip()
    if not stamp:
        return jsonify({"error": "stamp required"}), 400
    try:
        return jsonify(restore_reset_archive(stamp))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@ledger_bp.route("/admin/fg-reconcile")
@manager_required
def fg_reconcile_page():
    """Render the read-only finished-goods reconciliation / drift-detector page."""
    return render_template("fg_reconcile.html")


@ledger_bp.route("/api/organic/fg-reconcile", methods=["GET"])
@manager_required
def fg_reconcile_api():
    """Read-only per-fg_id reconciliation of expected vs actual FG stock."""
    return jsonify(compute_fg_reconciliation())


@ledger_bp.route("/api/organic/ledger/verify", methods=["GET"])
@manager_required
def ledger_verify_api():
    """Read-only self-check that the append-only event projection reproduces the
    reconciliation (and reports projected-vs-actual drift)."""
    return jsonify(verify_fg_projection())


@ledger_bp.route("/admin/fg-reset")
@manager_required
def fg_reset_page():
    """Render the zero-day reset tool page (preview-then-apply)."""
    return render_template("fg_reset.html")


@ledger_bp.route("/admin/fg-reset/preview", methods=["POST"])
@manager_required
def fg_reset_preview_api():
    """Read-only preview of a SKU-level zero-day reset for the submitted counts."""
    data = request.json or {}
    return jsonify(compute_reset_preview(data.get("counts") or []))


@ledger_bp.route("/admin/fg-reset/apply", methods=["POST"])
@manager_required
def fg_reset_apply_api():
    """Apply the zero-day reset. Gated behind an explicit confirmation token."""
    data = request.json or {}
    if data.get("confirm") != "RESET":
        return jsonify({"error": "Confirmation token required"}), 400
    return jsonify(apply_reset(data.get("counts") or [], actor=session.get("user", "")))

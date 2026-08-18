"""audit_tools.py — Inventory audit/traceability blueprint extracted from app.py.

Eighth step of the app.py split (CLAUDE.md "Pending architectural work"), third
inventory slice. Scope: the 6 read-mostly audit/traceability routes —
reconcile-raw (page + run preview/apply), organic trace, stock-exceptions, and
mass-balance (api + page).

Pattern (matches buyers/recipes/sales/finished_goods/raw_materials): PURE
routes-move — every helper and constant stays in app.py, reached via `import app`
+ app.-qualification. This deliberately includes the AUDIT-CRITICAL engines
_rebuild_raw_material_consumption (the reconcile replay), _compute_mass_balance,
and _sale_touches_fg: these are now only called by routes in this blueprint, but
they carry the consumption-chain invariants, so they stay in app.py rather than
being relocated (safest; a silent move could corrupt audit output). _run_start_date_str
and load_recipes are genuinely shared. The path constants ORGANIC_FG_PATH /
ORGANIC_RAW_PATH / ORGANIC_SALES_PATH live in app.py; ORGANIC_RUNS_PATH and the IO
primitives import directly from helpers.

These routes are read-only reporting EXCEPT reconcile-raw/run POST=apply, which
delegates entirely to app._rebuild_raw_material_consumption — the write logic is
unchanged and stays in app.py.

Defines its own manager_required (verbatim copy) so it has no import-time
dependency on app.py.
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

from helpers import ORGANIC_RUNS_PATH, _load_json, _save_json

import app

audit_tools_bp = Blueprint("audit_tools", __name__)


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


@audit_tools_bp.route("/admin/reconcile-raw")
@manager_required
def reconcile_raw_page():
    """Render the raw-material reconciliation tool page."""
    return render_template("reconcile_raw.html")


@audit_tools_bp.route("/admin/reconcile-raw/run", methods=["GET", "POST"])
@manager_required
def reconcile_raw_run():
    """GET = preview (no writes). POST = apply (writes recomputed materials + runs).
    Preview returns a per-lot before/after diff, the resulting shortfalls, and any
    lots that carry a manual 'Edit Qty' adjustment (those overrides are recomputed
    away on Apply — re-enter them afterward if they still apply)."""
    import copy
    do_apply = request.method == "POST"
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
    recipes = app.load_recipes()

    before = {m.get("id"): m.get("remaining") for m in materials}
    manually_adjusted = [
        {"item": m.get("item"), "supplier_lot": m.get("supplier_lot"),
         "date_received": m.get("date_received"), "remaining": m.get("remaining")}
        for m in materials if m.get("last_adjusted_at")
    ]

    work_materials = copy.deepcopy(materials)
    work_runs = copy.deepcopy(runs)
    summary = app._rebuild_raw_material_consumption(work_materials, work_runs, recipes)

    changes = []
    for m in work_materials:
        old, new = before.get(m.get("id")), m.get("remaining")
        if old != new:
            changes.append({
                "item": m.get("item"), "supplier_lot": m.get("supplier_lot"),
                "date_received": m.get("date_received"), "unit": m.get("unit"),
                "before": old, "after": new,
            })
    changes.sort(key=lambda c: ((c["item"] or "").lower(), c["date_received"] or ""))

    if do_apply:
        for m in work_materials:
            m.pop("last_adjusted_at", None)
        _save_json(app.ORGANIC_RAW_PATH, work_materials)
        _save_json(ORGANIC_RUNS_PATH, work_runs)

    return jsonify({
        "applied": do_apply,
        "runs_replayed": summary["runs_replayed"],
        "lots_changed": len(changes),
        "changes": changes,
        "shortfalls": summary["warnings"],
        "manually_adjusted_lots": manually_adjusted,
    })


@audit_tools_bp.route("/api/organic/trace", methods=["GET"])
@manager_required
def organic_trace():
    """GET /api/organic/trace - one-step traceability by raw lot or FG lot."""
    search_type = request.args.get("type", "")  # "raw_lot" or "fg_lot"
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})

    runs = _load_json(ORGANIC_RUNS_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sales = _load_json(app.ORGANIC_SALES_PATH, [])

    if search_type == "raw_lot":
        # A supplier lot string is NOT unique — every day-zero baseline ingredient
        # shares one BL-DDMMYY string, so matching runs by the string alone
        # conflates distinct physical lots. Resolve the typed lot to the actual
        # raw-material entries (each has a unique id) and trace each entry
        # separately by raw_material_id, so results never get commingled.
        materials = _load_json(app.ORGANIC_RAW_PATH, [])
        ql = query.lower()
        matched_entries = [m for m in materials
                           if (m.get("supplier_lot") or "").strip().lower() == ql]

        def _chain_for_run_ids(run_id_set):
            efg = [f for f in fg if f.get("run_id") in run_id_set]
            fg_id_set = {f["id"] for f in efg}
            esales = [s for s in sales if app._sale_touches_fg(s, fg_id_set)]
            return efg, esales

        lots_out = []
        all_runs, all_fg, all_sales = [], [], []
        seen_run, seen_fg, seen_sale = set(), set(), set()

        for entry in matched_entries:
            eid = entry.get("id")
            eruns = [r for r in runs
                     if any(ing.get("raw_material_id") == eid
                            for ing in r.get("ingredients_used", []))]
            efg, esales = _chain_for_run_ids({r["id"] for r in eruns})
            lots_out.append({
                "raw_material_id": eid,
                "item": entry.get("item", ""),
                "supplier": entry.get("supplier", ""),
                "date_received": entry.get("date_received", ""),
                "supplier_lot": entry.get("supplier_lot", ""),
                "unit": entry.get("unit", ""),
                "quantity": entry.get("quantity"),
                "remaining": entry.get("remaining"),
                "runs": eruns, "finished_goods": efg, "sales": esales,
            })
            for r in eruns:
                if r["id"] not in seen_run:
                    seen_run.add(r["id"]); all_runs.append(r)
            for f in efg:
                if f["id"] not in seen_fg:
                    seen_fg.add(f["id"]); all_fg.append(f)
            for s in esales:
                if s.get("id") not in seen_sale:
                    seen_sale.add(s.get("id")); all_sales.append(s)

        # Fallback: a run references this lot string but no current inventory entry
        # carries it (e.g. a pre-Change-B historical orphan). Still surface it so the
        # chain is never silently lost — flagged as not resolvable to a lot on file.
        if not matched_entries:
            legacy_runs = [r for r in runs
                           if any((ing.get("supplier_lot") or "").strip().lower() == ql
                                  for ing in r.get("ingredients_used", []))]
            if legacy_runs:
                efg, esales = _chain_for_run_ids({r["id"] for r in legacy_runs})
                lots_out.append({
                    "raw_material_id": None,
                    "item": "(lot not found in current inventory)",
                    "supplier": "", "date_received": "",
                    "supplier_lot": query, "unit": "",
                    "quantity": None, "remaining": None,
                    "runs": legacy_runs, "finished_goods": efg, "sales": esales,
                })
                all_runs, all_fg, all_sales = legacy_runs, efg, esales

        return jsonify({
            "search_type": "raw_lot",
            "query": query,
            "lots": lots_out,
            # Flattened unions kept for backward compatibility with any caller
            # that doesn't read the per-lot grouping.
            "runs": all_runs,
            "finished_goods": all_fg,
            "sales": all_sales,
        })

    elif search_type == "fg_lot":
        matched_fg = [f for f in fg if f.get("lot", "").lower() == query.lower()]
        run_ids = {f.get("run_id") for f in matched_fg}
        matched_runs = [r for r in runs if r.get("id") in run_ids]
        # Find sales (handle both new lots[] and legacy fg_id)
        fg_ids = {f["id"] for f in matched_fg}
        matched_sales = [s for s in sales if app._sale_touches_fg(s, fg_ids)]
        return jsonify({
            "search_type": "fg_lot",
            "query": query,
            "runs": matched_runs,
            "finished_goods": matched_fg,
            "sales": matched_sales,
        })

    return jsonify({"results": []})


@audit_tools_bp.route("/api/organic/stock-exceptions", methods=["GET"])
@manager_required
def organic_stock_exceptions():
    """Completed batches recorded as produced with LESS raw material on file than
    the recipe required — the INSUFFICIENT_STOCK markers written during deduction.
    Surfaced as an explicit, explainable list for audit rather than left buried in
    the run data. One row per short ingredient per batch. Read-only."""
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    out = []
    for run in runs:
        if run.get("status") != "completed":
            continue
        for used in (run.get("ingredients_used") or []):
            if not used.get("negative"):
                continue
            out.append({
                "run_id": run.get("id"),
                "week_id": run.get("week_id"),
                "day_idx": run.get("day_idx"),
                "day_name": run.get("day_name", ""),
                "production_date": app._run_start_date_str(run.get("week_id"), run.get("day_idx")),
                "vessel": run.get("vessel", ""),
                "recipe": run.get("recipe", ""),
                "brand": run.get("brand", ""),
                "batch_lot": run.get("lot", ""),
                "ingredient": used.get("item", ""),
                "shortfall": used.get("quantity_used"),
                "unit": used.get("unit", ""),
            })
    out.sort(key=lambda r: (r.get("production_date") or ""), reverse=True)
    return jsonify(out)


@audit_tools_bp.route("/api/organic/mass-balance", methods=["GET"])
@manager_required
def organic_mass_balance():
    """GET /api/organic/mass-balance - opening+received-consumed vs current (raw + FG)."""
    to = (request.args.get("to") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    frm = (request.args.get("from") or "").strip() or (datetime.now().strftime("%Y") + "-01-01")
    organic_only = (request.args.get("organic_only") or "").lower() in ("1", "true", "yes", "on")
    return jsonify(app._compute_mass_balance(frm, to, organic_only))


@audit_tools_bp.route("/mass-balance")
@manager_required
def mass_balance_page():
    """Render the mass-balance report page."""
    return render_template("mass_balance.html")

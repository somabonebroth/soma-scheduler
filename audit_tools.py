"""audit_tools.py — Inventory audit/traceability blueprint extracted from app.py.

Eighth step of the app.py split (CLAUDE.md "Pending architectural work"), third
inventory slice. Scope: the read-mostly audit/traceability routes —
reconcile-raw (page + run preview/apply), organic trace, the organic recipe
check, and mass-balance (api + page). (stock-exceptions was removed 2026-09-22:
the Daily Summary raises each exception on its own day with its own read, and
the all-time list had no other reader.)

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
import os
from datetime import datetime, timedelta
from io import BytesIO
from functools import wraps

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template, send_file

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


# Ingredient words that are never organic-certified and are permitted in an
# organic recipe without an "organic" name (Canada Organic Regime treats salt
# and water as outside the organic-percentage calculation). Water is already an
# untracked ingredient; salt is listed here.
ORGANIC_EXEMPT_WORDS = ("salt",)


def organic_recipe_issues(recipes):
    """Read-only check: every ACTIVE recipe certified "Organic" should draw only
    from organic-named ingredients.

    Why a name check: the whole raw-side organic guarantee is naming —
    production deducts a lot only when its item name EXACTLY matches the
    recipe's ingredient name (app.ingredients_match), so "Organic Chicken
    Bones" can never pull a "Chicken Bones" lot. That guard is silent about a
    recipe that simply names a non-organic ingredient. This surfaces those.

    Same ingredient filter as deduction (structured, not needs_review, tracked,
    amount > 0) so a flagged line is one that WOULD consume a lot. Returns
    {checked, issues: [{recipe, brand, format, ingredients[]}]}.
    """
    issues = []
    checked = 0
    for name, r in (recipes or {}).items():
        if not isinstance(r, dict) or r.get("archived"):
            continue
        if (r.get("certification") or "").strip().lower() != "organic":
            continue
        checked += 1
        flagged = []
        for section in app.INGREDIENT_SECTIONS:
            for item in (r.get(section) or []):
                if not app.is_structured_ingredient(item) or item.get("needs_review"):
                    continue
                ing = (item.get("name") or "").strip()
                if not ing or app.is_untracked_ingredient(ing):
                    continue
                try:
                    amount = float(item.get("amount") or 0)
                except (ValueError, TypeError):
                    amount = 0
                if amount <= 0:
                    continue
                low = ing.lower()
                if "organic" in low or any(w in low for w in ORGANIC_EXEMPT_WORDS):
                    continue
                if ing not in flagged:
                    flagged.append(ing)
        if flagged:
            issues.append({
                "recipe": name,
                "brand": (r.get("brand") or "").strip(),
                "format": (r.get("format") or "").strip(),
                "ingredients": flagged,
            })
    issues.sort(key=lambda i: (i["brand"].lower(), i["recipe"].lower()))
    return {"checked": checked, "issues": issues}


@audit_tools_bp.route("/api/organic/recipe-check", methods=["GET"])
@manager_required
def organic_recipe_check():
    """GET /api/organic/recipe-check - organic recipes naming a non-organic
    ingredient (see organic_recipe_issues). Read-only."""
    return jsonify(organic_recipe_issues(app.load_recipes()))


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


# ── Audit pack (2026-09-22) ──────────────────────────────────────────────────
# One PDF an inspector can read cover to cover: the recipes that ran, the CCP
# plan, one completed checklist as the worked example, then every batch in the
# period traced from supplier lot to buyer. It creates NO records — everything is
# re-read from runs / FG / sales / checklists / daily sign-offs, so the pack can
# never disagree with the pages those records live on.

def _checklist_record(week_id, day_idx, ccp_nums):
    """The filing facts for one production day's checklist: who signed, how many
    CCP sections were confirmed, and whether management reviewed the day."""
    date = app._run_start_date_str(week_id, day_idx)
    cl = app.load_checklist(week_id, day_idx) or {}
    checks = cl.get("checks") or {}
    confirmed = sum(1 for n in ccp_nums if checks.get("section-" + n))
    review = app._load_daily_signoffs().get(date) or {}
    return {
        "date": date,
        "filed": bool(cl.get("completed")),
        "signed_by": (cl.get("signoff_kitchen") or "").strip(),
        "ccp_confirmed": confirmed,
        "ccp_total": len(ccp_nums),
        "reviewed_by": review.get("name", ""),
        "reviewed_at": (review.get("signed_at") or "")[:10],
    }


def _build_audit_pack(date_from, date_to, organic_only=False):
    """Every completed batch whose START (production) date is in
    [date_from, date_to], inclusive — the start date is what the LOT# is made
    from, so the period and the lots on the jars line up. Each batch carries its
    raw lots (the frozen ingredients_used snapshot), both checklists it spans
    (started day N, counted day N+1), its FG record, and every sale that drew on
    it. organic_only keeps batches whose jars were certified Organic.
    """
    runs = _load_json(ORGANIC_RUNS_PATH, [])
    fg_by_id = {f.get("id"): f for f in _load_json(app.ORGANIC_FG_PATH, [])}
    sales = _load_json(app.ORGANIC_SALES_PATH, [])
    recipes = app.load_recipes()
    ccp = app.load_ccp_master() or []
    ccp_nums = [str(s.get("num") or "").strip() for s in ccp if isinstance(s, dict)]

    try:
        import ledger
        frozen = ledger._reset_frozen_run_ids()
    except Exception:
        frozen = set()

    batches = []
    for run in runs:
        if run.get("status") != "completed":
            continue
        start = app._run_start_date_str(run.get("week_id"), run.get("day_idx"))
        if not start or start < date_from or start > date_to:
            continue
        vessel = run.get("vessel", "")
        fin_w, fin_d = run.get("finish_week_id"), run.get("finish_day_idx")
        if fin_w is None or fin_d is None:
            # Never happens for a run completed by _complete_organic_run; kept so
            # a hand-edited record degrades to "no counting day" instead of a 500.
            fin_w, fin_d = None, None
        fg_id = f"fg_{fin_w}_{fin_d}_{vessel}" if fin_w is not None else None
        fg = fg_by_id.get(fg_id) or {}
        recipe_name = run.get("recipe", "")
        cert = (fg.get("certification")
                or (recipes.get(recipe_name) or {}).get("certification") or "").strip()
        if organic_only and cert != "Organic":
            continue

        sold = []
        if fg_id:
            for s in sales:
                qty = 0
                if s.get("fg_id") == fg_id:
                    qty += int(s.get("quantity") or 0)
                for lot in (s.get("lots") or []):
                    for b in (lot.get("breakdown") or []):
                        if b.get("fg_id") == fg_id:
                            qty += int(b.get("quantity") or 0)
                if qty:
                    sold.append({
                        "buyer": s.get("buyer", ""),
                        "date": (s.get("deducted_at") or s.get("sale_date")
                                 or s.get("created_at") or "")[:10],
                        "quantity": qty,
                        "order": s.get("order_id") or s.get("ripe_order_id")
                                 or s.get("retail_order_id") or "",
                    })
            sold.sort(key=lambda x: x["date"])

        batches.append({
            "run_id": run.get("id"),
            "start_date": start,
            "vessel": vessel,
            "recipe": recipe_name,
            "brand": run.get("brand") or fg.get("brand", ""),
            "format": fg.get("format") or (recipes.get(recipe_name) or {}).get("format", ""),
            "certification": cert,
            "lot": app._run_lot(run),
            "jars": int(run.get("amount_produced") or 0),
            "fg_on_record": bool(fg),
            # A zero-day reset replaced this batch's FG row with a counted baseline.
            "in_reset": run.get("id") in frozen,
            "jars_remaining": fg.get("quantity_remaining") if fg else None,
            "ingredients": [{
                "item": u.get("item", ""),
                "supplier": u.get("supplier", ""),
                "supplier_lot": u.get("supplier_lot", ""),
                "date_received": u.get("date_received", ""),
                "quantity": u.get("quantity_used", 0),
                "unit": u.get("unit", ""),
                "short": bool(u.get("negative")),
            } for u in (run.get("ingredients_used") or [])],
            "started": _checklist_record(run["week_id"], run["day_idx"], ccp_nums),
            "counted": (_checklist_record(fin_w, fin_d, ccp_nums)
                        if fin_w is not None else None),
            "sold": sold,
        })
    batches.sort(key=lambda b: (b["start_date"], b["vessel"]))

    # The worked-example checklist: the latest day in the period whose every CCP
    # section was confirmed (falling back to the latest filed day at all).
    days = {}
    for b in batches:
        for rec in (b["started"], b["counted"]):
            if rec and rec["filed"]:
                days[rec["date"]] = rec
    ordered = sorted(days.values(), key=lambda r: r["date"], reverse=True)
    example = next((r for r in ordered if r["ccp_total"]
                    and r["ccp_confirmed"] == r["ccp_total"]), None) \
        or (ordered[0] if ordered else None)

    used = sorted({b["recipe"] for b in batches if b["recipe"]})
    return {
        "from": date_from,
        "to": date_to,
        "organic_only": organic_only,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": (app._load_company_info() or {}).get("name", ""),
        "recipes": [(n, recipes[n]) for n in used if n in recipes],
        "recipes_missing": [n for n in used if n not in recipes],
        "ccp": ccp,
        "example_date": example["date"] if example else None,
        "batches": batches,
        "days": ordered,
    }


@audit_tools_bp.route("/api/organic/audit-pack.pdf", methods=["GET"])
@manager_required
def audit_pack_pdf():
    """GET /api/organic/audit-pack.pdf?from=&to=&organic_only= - the audit pack.

    Defaults to the last 90 days. Batches are selected by START date (the date
    the LOT# is made from)."""
    from pdf_engine import generate_audit_pack_pdf
    today = datetime.now()
    to = (request.args.get("to") or "").strip() or today.strftime("%Y-%m-%d")
    frm = (request.args.get("from") or "").strip() or (today - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(frm, "%Y-%m-%d")
        datetime.strptime(to, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "from and to must be YYYY-MM-DD"}), 400
    if frm > to:
        return jsonify({"error": "from must be on or before to"}), 400
    organic_only = (request.args.get("organic_only") or "").lower() in ("1", "true", "yes", "on")

    pack = _build_audit_pack(frm, to, organic_only)
    example = None
    if pack["example_date"]:
        # The worked example is the filed checklist exactly as the signed PDF
        # draws it: that day's schedule for the vessel line, the CCP master for
        # the sections, the stored ticks and sign-off.
        d = datetime.strptime(pack["example_date"], "%Y-%m-%d")
        week_id = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        day_idx = d.weekday()
        sched = ((app.load_schedule(week_id) or {}).get("schedule") or {}).get(str(day_idx), {})
        example = {
            "date": d,
            "active_vessels": [{"vessel": v, "recipe": sched[v]} for v in app.VESSELS if sched.get(v)],
            "filled": app.load_checklist(week_id, day_idx) or {},
        }

    logo_path = os.path.join(app.app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None
    buf = BytesIO()
    generate_audit_pack_pdf(buf, pack, example, logo_path)
    buf.seek(0)
    name = "Soma_Audit_Pack_" + frm + "_to_" + to + ("_Organic" if organic_only else "") + ".pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=name)

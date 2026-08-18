"""production.py — Production-domain blueprint extracted from app.py.

Ninth step of the app.py split (CLAUDE.md "Pending architectural work"). This is
the FIRST slice of the production domain: the low-risk read/schedule clusters —
schedules (create/weekly pages, get/list/delete) and the production tracker
(read-only analytics). The higher-risk production clusters — daily-production +
checklists (which trigger the raw-material consumption chain on save) and
traceability/completed-records (the deletion cascade + weekly sign-off) — are
deliberately deferred to later, separate verified steps and will append to this
same production_bp.

Pattern (matches every prior slice): PURE routes-move — all helpers and constants
stay in app.py, reached via `import app` + app.-qualification. Foundation names
(_load_json, _save_json, _classify_format, ORGANIC_RUNS_PATH) import directly from
helpers.

Decorators applied at import time can't be app.-qualified (app.py hasn't defined
them yet when it imports this blueprint at the top), so login_required AND
require_valid_week are LOCAL verbatim copies. require_valid_week calls
app.validate_week_id at REQUEST time (resolves fine — the validators stay in
app.py).
"""
import os
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint, request, jsonify, session, redirect, url_for, render_template,
)

from helpers import ORGANIC_RUNS_PATH, _load_json, _save_json, _classify_format

from pdf_engine import generate_filled_checklist_pdf

import app

production_bp = Blueprint("production", __name__)


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

def manager_required(f):
    """Local copy of app.py's manager_required (verbatim). The production tablet
    keeps the weekly schedule, daily production and checklists; scheduling,
    the tracker and the completed-production record are manager-only
    (2026-08-18 two-role split). Sessions from before roles existed count as
    manager (see app.current_role)."""
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


def require_valid_week(f):
    """Local copy of app.py's decorator. Validation logic is unchanged; the
    actual check delegates to app.validate_week_id at REQUEST time (the
    validator stays in app.py), so this resolves safely despite being applied
    at blueprint-import time."""
    @wraps(f)
    def decorated(*args, **kwargs):
        week_id = kwargs.get("week_id") or (args[0] if args else None)
        if week_id and not app.validate_week_id(week_id):
            return jsonify({"error": "Invalid week ID"}), 400
        return f(*args, **kwargs)
    return decorated


def require_valid_day(f):
    """Local copy of app.py's decorator (same import-time reason as
    require_valid_week). Delegates to app.validate_day_idx at REQUEST time."""
    @wraps(f)
    def decorated(*args, **kwargs):
        day_idx = kwargs.get("day_idx")
        if day_idx is not None and not app.validate_day_idx(day_idx):
            return jsonify({"error": "Invalid day index"}), 400
        return f(*args, **kwargs)
    return decorated


@production_bp.route("/create-schedule")
@manager_required
def create_schedule_page():
    """Render the create-schedule page."""
    return render_template("create_schedule.html")


@production_bp.route("/weekly-schedule")
@login_required
def weekly_schedule_page():
    """Render the weekly schedule page."""
    return render_template("weekly_view.html")


@production_bp.route("/api/schedule/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def get_schedule(week_id):
    """GET /api/schedule/<week_id> - return a week's schedule (or empty)."""
    data = app.load_schedule(week_id)
    if data:
        return jsonify(data)
    return jsonify({"schedule": None, "notes": ""})


@production_bp.route("/api/schedules", methods=["GET"])
@manager_required
def get_schedules():
    """GET /api/schedules - list all saved schedules."""
    return jsonify(app.list_schedules())


@production_bp.route("/api/schedule/<week_id>", methods=["DELETE"])
@manager_required
@require_valid_week
def delete_schedule(week_id):
    """DELETE /api/schedule/<week_id> - delete a week's schedule and its uncompleted organic runs."""
    path = os.path.join(app.SCHEDULES_DIR, week_id + ".json")
    if os.path.exists(path):
        os.unlink(path)
    # Remove scheduled (uncompleted) organic runs for this week.
    # Completed runs are preserved — they're traceability data.
    try:
        runs = _load_json(ORGANIC_RUNS_PATH, [])
        before = len(runs)
        runs = [r for r in runs
                if not (r.get("week_id") == week_id and r.get("status") != "completed")]
        if len(runs) != before:
            _save_json(ORGANIC_RUNS_PATH, runs)
    except Exception:
        pass
    return jsonify({"success": True})


@production_bp.route("/production-tracker")
@manager_required
def production_tracker_page():
    """Render the production tracker page.
    ?embed=1 hides the page header (used when embedded in the Analytics iframe,
    which already shows a header — avoids a stacked duplicate)."""
    return render_template("production_tracker.html",
                           embed=bool(request.args.get("embed")))


@production_bp.route("/api/production-tracker/<week_id>", methods=["GET"])
@manager_required
@require_valid_week
def get_production_tracker(week_id):
    """Return per-day bucketed totals for a week. Each day contains per-bucket
    counts plus legacy flat keys (produced/bb/kettles_end) for backward compat."""
    schedule_data = app.load_schedule(week_id) or {}
    recipes = app.load_recipes()

    daily_totals = []
    for d_idx in range(7):
        buckets, has_data = app._day_buckets(
            week_id, d_idx,
            recipes_cache=recipes, schedule_cache=schedule_data,
        )
        entry = {
            "day_idx": d_idx,
            "day_name": app.DAYS[d_idx],
            "buckets": dict(buckets),
            "total": app._bucket_total(buckets),
            "has_data": has_data,
        }
        daily_totals.append(entry)
    return jsonify(daily_totals)


@production_bp.route("/api/production-tracker/<week_id>/other-details", methods=["GET"])
@manager_required
@require_valid_week
def get_tracker_other_details(week_id):
    """Diagnostic: return every production entry in this week that classified
    as 'Other', with the reason. Uses the SAME attribution logic as the tracker:
    looks up the PREVIOUS day's recipe (what finished today), not today's start."""
    recipes = app.load_recipes()
    rows = []
    for d_idx in range(7):
        cl = app.load_checklist(week_id, d_idx)
        if not cl or not cl.get("produced"):
            continue
        prev_day_sched = app._get_previous_day_schedule(week_id, d_idx)
        for vessel_id, amount in (cl.get("produced") or {}).items():
            try:
                amt = int(amount)
            except (ValueError, TypeError):
                continue
            if amt <= 0:
                continue
            recipe_name = prev_day_sched.get(vessel_id, "")
            recipe_data = recipes.get(recipe_name) if recipe_name else None
            fmt = (recipe_data or {}).get("format", "")
            bucket = _classify_format(fmt)
            if bucket != "Other":
                continue
            if not recipe_name:
                reason = "no_schedule_entry"
                detail = f"No recipe scheduled yesterday for {vessel_id} — can't classify today's production"
            elif recipe_data is None:
                reason = "recipe_not_found"
                detail = f"Yesterday's scheduled recipe '{recipe_name}' no longer exists"
            elif not fmt:
                reason = "recipe_missing_format"
                detail = f"Recipe '{recipe_name}' has no format field set"
            else:
                reason = "unrecognized_format"
                detail = f"Format '{fmt}' is not SS-876/750/473ML or FZ-*"
            rows.append({
                "day_idx": d_idx,
                "day_name": app.DAYS[d_idx],
                "vessel": vessel_id,
                "amount": amt,
                "scheduled_recipe": recipe_name or None,
                "recipe_format": fmt or None,
                "reason": reason,
                "detail": detail,
            })
    return jsonify(rows)


@production_bp.route("/api/production-tracker/month/<year_month>", methods=["GET"])
@manager_required
def get_production_tracker_month(year_month):
    """Return weekly totals for a given month (format: YYYY-MM)."""
    if not re.match(r'^\d{4}-\d{2}$', year_month):
        return jsonify({"error": "Invalid month format, use YYYY-MM"}), 400
    try:
        year, month = int(year_month[:4]), int(year_month[5:7])
        from calendar import monthrange
        _, days_in_month = monthrange(year, month)
        first_day = datetime(year, month, 1)
        last_day = datetime(year, month, days_in_month)

        start_monday = first_day - timedelta(days=first_day.weekday())
        weeks = []
        current = start_monday
        while current <= last_day:
            wid = current.strftime("%Y-%m-%d")
            end_date = current + timedelta(days=6)
            totals = app._week_totals(wid)
            totals["buckets"] = {b: totals.get(b, 0) for b in app.TRACKER_BUCKETS}
            totals["week_id"] = wid
            totals["label"] = current.strftime("%b %d") + " - " + end_date.strftime("%b %d")
            for b in app.TRACKER_BUCKETS:
                totals.pop(b, None)
            weeks.append(totals)
            current += timedelta(days=7)
        return jsonify(weeks)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@production_bp.route("/api/production-tracker/year/<int:year>", methods=["GET"])
@manager_required
def get_production_tracker_year(year):
    """Return monthly totals for a given year."""
    if year < 2020 or year > 2099:
        return jsonify({"error": "Invalid year"}), 400
    months = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m in range(1, 13):
        from calendar import monthrange
        _, days_in_month = monthrange(year, m)
        first_day = datetime(year, m, 1)
        last_day = datetime(year, m, days_in_month)
        start_monday = first_day - timedelta(days=first_day.weekday())

        month_buckets = app._empty_buckets()
        current = start_monday
        seen_weeks = set()
        while current <= last_day:
            wid = current.strftime("%Y-%m-%d")
            if wid not in seen_weeks:
                seen_weeks.add(wid)
                wt = app._week_totals(wid)
                for bucket in app.TRACKER_BUCKETS:
                    month_buckets[bucket] += wt.get(bucket, 0)
            current += timedelta(days=7)

        month_total = {
            "buckets": dict(month_buckets),
            "total": app._bucket_total(month_buckets),
            "month": m,
            "label": month_names[m - 1],
        }
        months.append(month_total)
    return jsonify(months)


# ── Production: daily production + checklists ────────────────────────────────

@production_bp.route("/daily-production/<week_id>/<int:day_idx>")
@login_required
@require_valid_week
@require_valid_day
def daily_production_page(week_id, day_idx):
    """Render the daily-production page for a week/day."""
    return render_template("daily_production.html", week_id=week_id, day_idx=day_idx)


@production_bp.route("/api/daily-production/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
@require_valid_week
@require_valid_day
def get_daily_production(week_id, day_idx):
    """GET /api/daily-production/<week>/<day> - the day's production data."""
    schedule_data = app.load_schedule(week_id)
    recipes = app.load_recipes()
    checklist = app.load_checklist(week_id, day_idx)

    today_schedule = {}
    prev_schedule = {}

    if schedule_data and schedule_data.get("schedule"):
        today_key = str(day_idx)
        today_schedule = schedule_data["schedule"].get(today_key, {})

        if day_idx > 0:
            prev_schedule = schedule_data["schedule"].get(str(day_idx - 1), {})

    # Cross-week: Monday FINISH needs previous week's Sunday (day 6)
    if day_idx == 0:
        prev_week_start = datetime.strptime(week_id, "%Y-%m-%d") - timedelta(days=7)
        prev_week_id = prev_week_start.strftime("%Y-%m-%d")
        prev_week_data = app.load_schedule(prev_week_id)
        if prev_week_data and prev_week_data.get("schedule"):
            prev_schedule = prev_week_data["schedule"].get("6", {})

    # FINISH = previous day's assigned recipe (it was started yesterday, finishing today)
    # START = today's assigned recipe (what we're starting/prepping today)
    finish_kettles = {}
    start_kettles = {}

    for vessel in app.VESSELS:
        # FINISH: previous day's recipe (started yesterday, finishing today)
        prev_recipe_name = prev_schedule.get(vessel, "")
        if prev_recipe_name and prev_recipe_name.strip():
            prev_recipe_data = recipes.get(prev_recipe_name, {})
            if prev_recipe_data:
                details = app._halve_for_115L(prev_recipe_data) if vessel == "115L" else prev_recipe_data
                finish_kettles[vessel] = {
                    "recipe": prev_recipe_name,
                    "details": details,
                    "halved": vessel == "115L",
                }

        # START: today's assigned recipe (starting today, will be finished tomorrow)
        today_recipe_name = today_schedule.get(vessel, "")
        if today_recipe_name and today_recipe_name.strip():
            today_recipe_data = recipes.get(today_recipe_name, {})
            if today_recipe_data:
                details = app._halve_for_115L(today_recipe_data) if vessel == "115L" else today_recipe_data
                start_kettles[vessel] = {
                    "recipe": today_recipe_name,
                    "details": details,
                    "halved": vessel == "115L",
                }

    # Annotate 'per L' ingredients with their inferred g/ml dosing unit so the
    # recipe cards show e.g. "3 g per L" for kitchen staff.
    raw_materials = app._load_json(app.ORGANIC_RAW_PATH, [])
    for kettle in list(finish_kettles.values()) + list(start_kettles.values()):
        app._attach_per_l_units(kettle.get("details"), raw_materials)

    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    date = week_start + timedelta(days=day_idx)
    prev_date = date - timedelta(days=1)

    daily_notes = ""
    if schedule_data and schedule_data.get("daily_notes"):
        daily_notes = schedule_data["daily_notes"].get(str(day_idx), "")

    # LOT# = expiry date (production date + 365 days) in ddmmyy format.
    # prev_lot is used when generating labels for the recipe being finished today,
    # which was started yesterday → expiry = yesterday + 365 days.
    prev_expiry = prev_date + timedelta(days=365)
    today_expiry = date + timedelta(days=365)

    return jsonify({
        "date": date.strftime("%A, %d/%m/%Y"),
        "day_name": app.DAYS[day_idx],
        "prev_date": prev_date.strftime("%d/%m/%Y"),
        "prev_lot": prev_expiry.strftime("%d%m%y"),
        "lot": today_expiry.strftime("%d%m%y"),
        "today_lot": today_expiry.strftime("%d%m%y"),
        "finish": finish_kettles,
        "start": start_kettles,
        "checklist": checklist,
        "notes": schedule_data.get("notes", "") if schedule_data else "",
        "daily_notes": daily_notes,
    })


@production_bp.route("/api/daily-production/<week_id>/<int:day_idx>/save", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def save_daily_production(week_id, day_idx):
    """POST .../save - save daily production; finishes the prior day's organic runs (consumption chain)."""
    data = request.json or {}
    data["last_updated"] = datetime.now().isoformat()
    app.save_checklist_data(week_id, day_idx, data)
    # Process any organic runs scheduled on the previous day —
    # the produced amounts entered today are the finish of yesterday's runs.
    warnings = []
    try:
        warnings = app._check_organic_completion(week_id, day_idx, data) or []
    except Exception:
        pass
    return jsonify({"success": True, "warnings": warnings})


@production_bp.route("/api/checklist/<week_id>/<int:day_idx>", methods=["GET"])
@login_required
@require_valid_week
@require_valid_day
def get_checklist_route(week_id, day_idx):
    """GET /api/checklist/<week>/<day> - return the day's checklist."""
    data = app.load_checklist(week_id, day_idx)
    schedule_data = app.load_schedule(week_id)
    day_info = {}
    if schedule_data and schedule_data.get("schedule"):
        day_key = str(day_idx)
        if day_key in schedule_data["schedule"]:
            day_info = schedule_data["schedule"][day_key]
    return jsonify({"checklist": data, "day_info": day_info})


@production_bp.route("/api/checklist/<week_id>/<int:day_idx>", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def save_checklist_route(week_id, day_idx):
    """POST /api/checklist/<week>/<day> - save the day's checklist."""
    data = request.json or {}
    data["last_updated"] = datetime.now().isoformat()
    app.save_checklist_data(week_id, day_idx, data)
    return jsonify({"success": True})


@production_bp.route("/api/checklist/<week_id>/<int:day_idx>/complete", methods=["POST"])
@login_required
@require_valid_week
@require_valid_day
def complete_checklist(week_id, day_idx):
    """POST .../complete - mark the checklist complete and generate its PDF."""
    data = request.json or {}
    data["last_updated"] = datetime.now().isoformat()
    data["completed"] = True
    app.save_checklist_data(week_id, day_idx, data)

    schedule_data = app.load_schedule(week_id)
    day_info = {}
    if schedule_data and schedule_data.get("schedule"):
        day_key = str(day_idx)
        if day_key in schedule_data["schedule"]:
            day_info = schedule_data["schedule"][day_key]

    active_vessels = []
    for vessel in app.VESSELS:
        recipe = day_info.get(vessel, "")
        if recipe:
            active_vessels.append({"vessel": vessel, "recipe": recipe})

    week_start = datetime.strptime(week_id, "%Y-%m-%d")
    date = week_start + timedelta(days=day_idx)

    logo_path = os.path.join(app.app.static_folder, "logo.jpg")
    if not os.path.exists(logo_path):
        logo_path = None

    week_pdf_dir = os.path.join(app.PDF_DIR, week_id)
    os.makedirs(week_pdf_dir, exist_ok=True)
    filename = app.DAYS[day_idx] + "_Completed_Checklist.pdf"
    pdf_path = os.path.join(week_pdf_dir, filename)
    generate_filled_checklist_pdf(pdf_path, date, active_vessels, data, logo_path,
                                  sections=app.load_ccp_master())

    warnings = []
    try:
        warnings = app._check_organic_completion(week_id, day_idx, data) or []
    except Exception:
        pass

    return jsonify({"success": True, "filename": filename, "warnings": warnings})


@production_bp.route("/api/checklist-status/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def checklist_status(week_id):
    """GET /api/checklist-status/<week> - per-day completion flags for a week."""
    statuses = {}
    for d_idx in range(7):
        data = app.load_checklist(week_id, d_idx)
        if data and data.get("completed"):
            statuses[str(d_idx)] = "completed"
        elif data and app._has_meaningful_data(data):
            statuses[str(d_idx)] = "in_progress"
        else:
            statuses[str(d_idx)] = "not_started"
    return jsonify(statuses)


# ── Production: traceability / completed records ─────────────────────────────

@production_bp.route("/traceability")
@manager_required
def traceability_page():
    """Render the Completed Production page."""
    return render_template("traceability.html")


def _ccp_sort_key(check_key):
    """Order 'section-<num>' keys numerically, non-numeric ones last."""
    tail = check_key.split("-", 1)[1] if "-" in check_key else check_key
    try:
        return (0, int(tail), "")
    except ValueError:
        return (1, 0, tail)


@production_bp.route("/api/traceability/<week_id>/summary", methods=["GET"])
@manager_required
@require_valid_week
def get_week_summary(week_id):
    """Return a structured summary of a week's production for the HOO review modal.
    Includes: total production, CCP completion, notes flagged, inconsistencies.
    """
    schedule_data = app.load_schedule(week_id) or {}
    sched = (schedule_data.get("schedule") or {}) if schedule_data else {}
    recipes_data = app.load_recipes()

    ccp_titles = {}
    for sec in (app.load_ccp_master() or []):
        if isinstance(sec, dict) and sec.get("num") is not None:
            ccp_titles["section-" + str(sec["num"])] = (sec.get("title") or "").strip()

    days_summary = []
    total_produced = {}
    all_notes = []
    ccp_flags = []
    missing_signoffs = []

    for d_idx in range(7):
        cl = app.load_checklist(week_id, d_idx)
        if not cl or not cl.get("completed"):
            continue

        day_info = sched.get(str(d_idx), {}) or {}
        day_name = app.DAYS[d_idx]

        scheduled = {v: day_info.get(v, "").strip() for v in app.VESSELS if day_info.get(v, "").strip()}

        produced = cl.get("produced", {}) or {}
        bb_produced = cl.get("bb_produced", {}) or {}

        day_prod = {}
        for v, recipe in scheduled.items():
            if recipe:
                qty = int(produced.get(v) or 0)
                if qty:
                    day_prod[recipe] = day_prod.get(recipe, 0) + qty
                    total_produced[recipe] = total_produced.get(recipe, 0) + qty

        notes = (cl.get("notes") or "").strip()
        if notes:
            all_notes.append({"day": day_name, "note": notes})

        # CCP confirmations are stored under "checks" as {"section-<num>": bool},
        # one tick per CCP master section, written by the tablet. This read used
        # to look for a "sections" key that nothing has ever written, so CCP
        # flags could not fire and "all clear" was only ever checking sign-offs
        # and notes. Keyed off the master so a renamed section reads correctly.
        checks = cl.get("checks", {}) or {}
        sec_checks = {k: v for k, v in checks.items() if str(k).startswith("section-")}
        day_ccp_issues = []
        if scheduled and not sec_checks:
            day_ccp_issues.append("no CCP sections confirmed")
        for key in sorted(sec_checks, key=_ccp_sort_key):
            if not sec_checks[key]:
                num = key.split("-", 1)[1]
                day_ccp_issues.append((num + " " + ccp_titles.get(key, "")).strip())
        if day_ccp_issues:
            ccp_flags.append({"day": day_name, "issues": day_ccp_issues})

        # Check kitchen staff sign-off (the only required sign-off in current workflow)
        kitchen = (cl.get("signoff_kitchen") or "").strip()
        if scheduled and not kitchen:
            missing_signoffs.append(day_name)

        days_summary.append({
            "day":           day_name,
            "day_idx":       d_idx,
            "scheduled":     list(scheduled.values()),
            "produced":      day_prod,
            "notes":         notes,
            "kitchen_signoff": kitchen,
            "has_ccp_flags": len(day_ccp_issues) > 0,
        })

    flags = []
    if missing_signoffs:
        flags.append(f"Missing kitchen sign-off: {', '.join(missing_signoffs)}")
    if ccp_flags:
        for cf in ccp_flags:
            flags.append(f"{cf['day']} CCP issue: {'; '.join(cf['issues'][:3])}")

    return jsonify({
        "week_id": week_id,
        "days": days_summary,
        "total_produced": total_produced,
        "notes": all_notes,
        "flags": flags,
        "ccp_flags": ccp_flags,
        "all_clear": len(flags) == 0 and len(all_notes) == 0,
    })


@production_bp.route("/api/traceability", methods=["GET"])
@manager_required
def get_traceability():
    """GET /api/traceability - completed weekly production records."""
    weeks = app.list_schedules()
    signoffs = app._load_weekly_signoffs()
    recipes = app.load_recipes()
    records = []
    for week_id in weeks:
        week_record = {"week_id": week_id, "days": []}
        schedule_data = app.load_schedule(week_id)
        for d_idx in range(7):
            cl = app.load_checklist(week_id, d_idx)
            if cl and cl.get("completed"):
                day_info = {}
                if schedule_data and schedule_data.get("schedule"):
                    day_info = schedule_data["schedule"].get(str(d_idx), {})
                # Certification PER VESSEL — a single day can run different
                # certifications across kettles (e.g. Organic on K1, Conventional
                # on K2), so we never collapse the day to one label. cert_by_vessel
                # maps each producing vessel to its recipe's certification;
                # certifications is the distinct set present that day.
                cert_by_vessel = {}
                certifications = []
                if day_info:
                    for v in app.VESSELS:
                        rname = day_info.get(v, "")
                        if rname and rname in recipes:
                            cert = (recipes[rname].get("certification") or "").strip()
                            if cert:
                                cert_by_vessel[v] = cert
                                if cert not in certifications:
                                    certifications.append(cert)
                week_record["days"].append({
                    "day_idx": d_idx,
                    "day_name": app.DAYS[d_idx],
                    "completed": True,
                    "last_updated": cl.get("last_updated", ""),
                    # Kept for back-compat: a single value only when the whole day
                    # is one certification; blank when mixed (use certifications).
                    "certification": certifications[0] if len(certifications) == 1 else "",
                    "certifications": certifications,
                    "cert_by_vessel": cert_by_vessel,
                })
        if week_record["days"]:
            state, complete_idxs, incomplete_idxs = app._week_completion_state(week_id)
            week_record["completion_state"] = state
            week_record["scheduled_complete_days"] = complete_idxs
            week_record["scheduled_incomplete_days"] = [
                {"day_idx": di, "day_name": app.DAYS[di]} for di in incomplete_idxs
            ]
            week_record["hoo_signoff"] = signoffs.get(week_id) or None
            records.append(week_record)
    return jsonify(records)


@production_bp.route("/api/traceability/<week_id>/<int:day_idx>", methods=["DELETE"])
@manager_required
@require_valid_week
@require_valid_day
def delete_traceability_record(week_id, day_idx):
    """Delete a completed production record AND properly reverse its production,
    so the traceability chain is never left orphaned.

    A bare checklist delete used to leave the consumed raw materials deducted
    and the finished goods floating with no record behind them. This now:
      1. Refuses if any finished goods from that day were already sold (the one
         case that cannot be safely reversed — the sale is downstream).
      2. Restores the raw materials each finishing run consumed.
      3. Removes the finished goods that day created.
      4. Resets those runs to 'scheduled'.
      5. Deletes the checklist file + completed-checklist PDF.
    """
    path = os.path.join(app.CHECKLISTS_DIR, week_id + "_day" + str(day_idx) + ".json")
    if not os.path.exists(path):
        return jsonify({"error": "Record not found"}), 404

    runs = _load_json(ORGANIC_RUNS_PATH, [])
    materials = _load_json(app.ORGANIC_RAW_PATH, [])
    fg = _load_json(app.ORGANIC_FG_PATH, [])
    sales = _load_json(app.ORGANIC_SALES_PATH, [])

    # Finished goods this production day created (keyed to the finish day).
    day_fg = [f for f in fg if f.get("week_id") == week_id and f.get("day_idx") == day_idx]
    day_fg_ids = {f.get("id") for f in day_fg}

    # Hard stop: refuse if any of that stock has already been sold.
    sold_units = 0
    for s in sales:
        if s.get("fg_id") in day_fg_ids:
            try:
                sold_units += int(s.get("quantity") or 0)
            except (ValueError, TypeError):
                pass
            continue
        for lot in (s.get("lots") or []):
            for b in (lot.get("breakdown") or []):
                if b.get("fg_id") in day_fg_ids:
                    try:
                        sold_units += int(b.get("quantity") or 0)
                    except (ValueError, TypeError):
                        pass
    if sold_units > 0:
        return jsonify({
            "error": (f"Cannot delete: {sold_units} unit(s) produced this day have "
                      f"already been sold. Reverse the sale(s) first, then delete."),
            "sold_units": sold_units,
        }), 409

    # Reverse the runs that FINISHED on this day: restore the raw materials they
    # consumed, then reset them to scheduled.
    mat_by_id = {m.get("id"): m for m in materials}
    restored = 0
    reset_runs = 0
    for run in runs:
        if run.get("finish_week_id") != week_id or run.get("finish_day_idx") != day_idx:
            continue
        if run.get("status") != "completed":
            continue
        for used in (run.get("ingredients_used") or []):
            if used.get("negative"):
                continue  # insufficient-stock markers never reserved real stock
            rm_id = used.get("raw_material_id")
            qty = used.get("quantity_used", 0)
            mat = mat_by_id.get(rm_id)
            if mat and qty:
                mat["remaining"] = round(mat.get("remaining", 0) + qty, 4)
                restored += 1
        run["status"] = "scheduled"
        run["ingredients_used"] = []
        run["amount_produced"] = 0
        run["completed_at"] = None
        run["finish_week_id"] = None
        run["finish_day_idx"] = None
        reset_runs += 1

    # Remove the finished goods this day created.
    fg = [f for f in fg if f.get("id") not in day_fg_ids]

    _save_json(ORGANIC_RUNS_PATH, runs)
    _save_json(app.ORGANIC_RAW_PATH, materials)
    _save_json(app.ORGANIC_FG_PATH, fg)

    os.unlink(path)
    pdf_path = os.path.join(app.PDF_DIR, week_id, app.DAYS[day_idx] + "_Completed_Checklist.pdf")
    if os.path.exists(pdf_path):
        os.unlink(pdf_path)

    return jsonify({
        "success": True,
        "runs_reset": reset_runs,
        "lots_restored": restored,
        "finished_goods_removed": len(day_fg_ids),
    })


@production_bp.route("/api/weekly-signoff/<week_id>", methods=["POST"])
@manager_required
@require_valid_week
def sign_off_week(week_id):
    """Head of Operations confirms a week's production records.

    Requires that all days that had a schedule entry have completed checklists.
    Body: {name, notes (optional)}
    """
    data = request.json or {}
    name = (data.get("name") or "").strip()
    notes = (data.get("notes") or "").strip()
    if not name:
        return jsonify({"error": "Name required to sign off"}), 400

    state, _, incomplete = app._week_completion_state(week_id)
    if state == "none":
        return jsonify({"error": "No production scheduled for this week"}), 400
    if state == "partial":
        labels = ", ".join(app.DAYS[di] for di in incomplete)
        return jsonify({
            "error": f"Cannot sign off — {len(incomplete)} scheduled day(s) still incomplete: {labels}"
        }), 400

    signoffs = app._load_weekly_signoffs()
    signoffs[week_id] = {
        "name": name,
        "notes": notes,
        "signed_at": datetime.now().isoformat(),
    }
    app._save_weekly_signoffs(signoffs)
    return jsonify({"success": True, "signoff": signoffs[week_id]})


@production_bp.route("/api/weekly-signoff/<week_id>", methods=["DELETE"])
@manager_required
@require_valid_week
def unsign_week(week_id):
    """Reverse a weekly sign-off (HOO can undo)."""
    signoffs = app._load_weekly_signoffs()
    if week_id in signoffs:
        del signoffs[week_id]
        app._save_weekly_signoffs(signoffs)
    return jsonify({"success": True})


# ── Production: organic production-runs read ─────────────────────────────────

@production_bp.route("/api/organic/production-runs", methods=["GET"])
@manager_required
def get_organic_runs():
    """GET /api/organic/production-runs - return all organic production runs."""
    return jsonify(_load_json(ORGANIC_RUNS_PATH, []))

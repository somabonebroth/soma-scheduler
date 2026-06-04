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


@production_bp.route("/create-schedule")
@login_required
def create_schedule_page():
    return render_template("create_schedule.html")

@production_bp.route("/weekly-schedule")
@login_required
def weekly_schedule_page():
    return render_template("weekly_view.html")

@production_bp.route("/api/schedule/<week_id>", methods=["GET"])
@login_required
@require_valid_week
def get_schedule(week_id):
    data = app.load_schedule(week_id)
    if data:
        return jsonify(data)
    return jsonify({"schedule": None, "notes": ""})

@production_bp.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    return jsonify(app.list_schedules())

@production_bp.route("/api/schedule/<week_id>", methods=["DELETE"])
@login_required
@require_valid_week
def delete_schedule(week_id):
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
@login_required
def production_tracker_page():
    return render_template("production_tracker.html")

@production_bp.route("/api/production-tracker/<week_id>", methods=["GET"])
@login_required
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
@login_required
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
@login_required
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
@login_required
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

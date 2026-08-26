"""end_of_day.py — the floor's one end-of-shift flow.

One button on the production dashboard opens a step-at-a-time run through
everything a shift owes before anyone leaves:

  1. Production — what was counted today, plus any CCP section still
     unconfirmed (tickable here), signed and time-stamped. This IS the
     checklist sign-off: the tablet no longer files the day, it only
     captures numbers and ticks as they happen.
  2. A note for management, seen on tomorrow's Daily Review.
  3. The closing checklist, one item at a time, in the manager's order,
     signed and time-stamped.
  4. One rotating job, or an explicit decline.
  5. Done.

Pattern: routes-move / helpers-stay (buyers.py) — a bare `import app` at
module top, `import production` / `import cleaning` for the two domains this
joins. It owns NO data of its own: step 1 writes the production checklist via
`production.file_checklist`, steps 2-4 write the cleaning file via cleaning's
own endpoints. Nothing here is a new record type.
"""
import logging
from functools import wraps
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

import app
import cleaning
import production

logger = logging.getLogger(__name__)

end_of_day_bp = Blueprint("end_of_day", __name__)


def boh_required(f):
    """Back-of-house gate (local copy pattern — keeps the blueprint free of an
    import-time dependency on app.py): manager + production may act, the FOH
    role may not (2026-08-26 three-role split). This wizard files the day and
    signs the kitchen's closing list; FOH gets its own flow."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        if (session.get("role") or "manager") == "foh":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not available to the FOH role"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


def _coords(on):
    """(week_id, day_idx) for a date — Monday-start weeks, Monday = 0."""
    monday = on - timedelta(days=on.weekday())
    return monday.strftime("%Y-%m-%d"), on.weekday()


def _parse_date(s):
    """Parse YYYY-MM-DD → date, or None if missing/invalid."""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _production_step(week_id, day_idx):
    """Step 1's payload: what the day counted, and the state of its CCP ticks.

    Reads the SAME summary the weekly review and the daily brief read
    (`production.summarize_day`), just before the day is filed rather than
    after — so what the floor signs is what management later sees. Jars are
    credited to the batch that STARTED the previous day, per the next-day
    counting model.
    """
    cl = app.load_checklist(week_id, day_idx) or {}
    schedule_data = app.load_schedule(week_id) or {}
    sched = (schedule_data.get("schedule") or {}) if schedule_data else {}
    day_info = sched.get(str(day_idx), {}) or {}
    prev_sched = app._get_previous_day_schedule(week_id, day_idx) or {}

    summary = production.summarize_day(week_id, day_idx, require_completed=False) or {}

    # Was there anything to do today at all? A day with no schedule either
    # side and no numbers keyed is a quiet day — step 1 is skipped entirely.
    started = [v for v in app.VESSELS if (day_info.get(v, "") or "").strip()]
    finished = [v for v in app.VESSELS if (prev_sched.get(v, "") or "").strip()]
    ran = bool(started or finished or app._has_meaningful_data(cl))

    ccp_titles = production.ccp_titles_map()
    checks = cl.get("checks", {}) or {}
    sections = []
    for sec in (app.load_ccp_master() or []):
        if not isinstance(sec, dict) or sec.get("num") is None:
            continue
        key = "section-" + str(sec["num"])
        sections.append({
            "key": key,
            "num": str(sec["num"]),
            "title": ccp_titles.get(key, "") or (sec.get("title") or "").strip(),
            "confirmed": bool(checks.get(key)),
        })

    produced = summary.get("produced", {}) or {}
    rows = [{"recipe": r, "jars": q} for r, q in sorted(produced.items())]
    try:
        kettles_end = int(cl.get("kettles_end") or 0)
    except (ValueError, TypeError):
        kettles_end = 0

    return {
        "ran": ran,
        "week_id": week_id,
        "day_idx": day_idx,
        "completed": bool(cl.get("completed")),
        "signed_by": (cl.get("signoff_kitchen") or "").strip(),
        "signed_at": cl.get("completed_at", "") or cl.get("last_updated", ""),
        "rows": rows,
        "total_jars": sum(r["jars"] for r in rows),
        "kettles_end": kettles_end,
        "started_recipes": sorted({(day_info.get(v) or "").strip() for v in started}),
        "sections": sections,
        "unconfirmed": [s for s in sections if not s["confirmed"]],
        "day_notes": (cl.get("notes") or "").strip(),
    }


@end_of_day_bp.route("/end-of-day")
@boh_required
def end_of_day_page():
    """The end-of-shift flow — one step on screen at a time."""
    return render_template("end_of_day.html")


@end_of_day_bp.route("/api/end-of-day", methods=["GET"])
@boh_required
def get_end_of_day():
    """Everything the flow needs in one read: today's production and CCP state,
    the note already left, the closing list in the manager's order with what is
    already ticked, and the rotating pool grouped by slot.

    Also reports which steps are already done so a re-opened flow resumes
    instead of asking twice.
    """
    on = _parse_date(request.args.get("date")) or date.today()
    week_id, day_idx = _coords(on)

    try:
        prod = _production_step(week_id, day_idx)
    except Exception:
        logger.warning("end of day: production step failed", exc_info=True)
        prod = {"ran": False, "week_id": week_id, "day_idx": day_idx,
                "completed": False, "rows": [], "sections": [], "unconfirmed": []}

    data = cleaning._load_cleaning()
    iso = on.isoformat()
    rec = next((r for r in data["closing_records"] if r.get("date") == iso), None)
    closing = cleaning._closing_record_view(
        rec or {"date": iso, "staff": "", "notes": "", "items": []},
        data["closing_items"])

    jobs = [cleaning._job_view(j) for j in data["jobs"]]
    jobs = [j for j in jobs if j["ready"]]
    jobs.sort(key=lambda j: (-j["due_ratio"], 0 if j["last_done"] is None else 1))

    done_today = [c for c in data["completions"] if c.get("date") == iso]
    declined = any(d.get("date") == iso for d in data["declines"])

    return jsonify({
        "date": iso,
        "label": on.strftime("%A %d %B"),
        "production": prod,
        "note": (rec or {}).get("manager_note", ""),
        "note_saved": bool((rec or {}).get("manager_note_ts")),
        "closing": closing,
        "closing_signed": bool((rec or {}).get("staff")),
        "jobs": jobs,
        "job_done": [{"title": c.get("job_title", ""), "staff": c.get("staff", "")}
                     for c in done_today],
        "declined": declined,
        "staff": ((rec or {}).get("staff") or prod.get("signed_by") or ""),
    })


@end_of_day_bp.route("/api/end-of-day/production", methods=["POST"])
@boh_required
def sign_production():
    """Step 1's sign-off — files the day. Body: {staff, checks:{key: bool}, date?}.

    Merges the wizard's CCP ticks into the checklist the tablet has been
    autosaving, then files it through `production.file_checklist`, the same
    path the tablet's own button used: PDF, consumption chain, warnings.
    Sections left unticked stay unticked and surface as CCP flags on the brief.
    """
    body = request.get_json(force=True) or {}
    staff = (body.get("staff") or "").strip()
    if not staff:
        return jsonify({"error": "Name required to sign"}), 400
    on = _parse_date(body.get("date")) or date.today()
    week_id, day_idx = _coords(on)

    cl = app.load_checklist(week_id, day_idx) or {}
    checks = dict(cl.get("checks") or {})
    for key, val in (body.get("checks") or {}).items():
        if str(key).startswith("section-"):
            checks[str(key)] = bool(val)
    cl["checks"] = checks
    cl["signoff_kitchen"] = staff
    cl["completed_at"] = datetime.now().isoformat(timespec="seconds")

    try:
        filename, warnings = production.file_checklist(week_id, day_idx, cl)
    except Exception as e:
        logger.exception("end of day: filing the checklist failed")
        return jsonify({"error": "Could not file the day: %s" % e}), 500
    return jsonify({"ok": True, "filename": filename, "warnings": warnings,
                    "signed_at": cl["completed_at"]})


# ── Front of house (2026-08-26) — FOH's own flow: note → closing → done.
#    No production step (that's BOH's) and no rotating jobs. Data lives on
#    cleaning.py's foh_closing_* keys; this blueprint still owns NO data. ────

def foh_required(f):
    """Front-of-house gate (local copy pattern): manager + foh may act,
    production may not — this wizard signs the FOH closing list."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect(url_for("login_page"))
        if (session.get("role") or "manager") not in ("manager", "foh"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "FOH access required"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


@end_of_day_bp.route("/foh-end-of-day")
@foh_required
def foh_end_of_day_page():
    """The FOH end-of-shift flow — one step on screen at a time."""
    return render_template("foh_end_of_day.html")


@end_of_day_bp.route("/api/foh-end-of-day", methods=["GET"])
@foh_required
def get_foh_end_of_day():
    """Everything the FOH flow needs in one read: the note already left and
    the FOH closing list in the manager's order with what is already ticked.

    Also reports which steps are done so a re-opened flow resumes rather than
    asking twice — the same resume contract as /api/end-of-day, minus the
    production and rotation halves FOH doesn't have.
    """
    on = _parse_date(request.args.get("date")) or date.today()
    data = cleaning._load_cleaning()
    iso = on.isoformat()
    rec = next((r for r in data["foh_closing_records"] if r.get("date") == iso), None)
    closing = cleaning._closing_record_view(
        rec or {"date": iso, "staff": "", "notes": "", "items": []},
        data["foh_closing_items"])

    return jsonify({
        "date": iso,
        "label": on.strftime("%A %d %B"),
        "note": (rec or {}).get("manager_note", ""),
        "note_saved": bool((rec or {}).get("manager_note_ts")),
        "closing": closing,
        "closing_signed": bool((rec or {}).get("staff")),
        "staff": (rec or {}).get("staff", ""),
    })

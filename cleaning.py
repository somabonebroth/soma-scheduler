"""cleaning.py — End-of-shift rotating cleaning jobs blueprint.

Self-contained blueprint (suppliers.py pattern): owns its data file and
helpers, defines a local login_required, pulls IO from helpers.py.

Concept: a pool of ~1hr micro cleaning jobs, each with a rest interval
(1 / 3 / 6 month presets). Completing a job takes it OUT of the active
list; it re-enters interval_days after completion. Never-done jobs are
active immediately. Active jobs are ordered most-overdue first (days
since last done ÷ interval); skipped jobs simply stay near the front.
Staff pick from the top, do the job, and sign off with their name. No
manager sign-off, no penalty for skipped days.
"""
import os
from functools import wraps
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

from helpers import _load_json, _save_json, DATA_DIR

cleaning_bp = Blueprint("cleaning", __name__)

CLEANING_PATH = os.path.join(DATA_DIR, "cleaning_jobs.json")

# UI presets are 30 / 90 / 180 days; interval_days is stored per job so
# custom values are also accepted.
DEFAULT_INTERVAL_DAYS = 30


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


def _load_cleaning():
    """Load the cleaning data file ({jobs: [], completions: []})."""
    data = _load_json(CLEANING_PATH, {})
    data.setdefault("jobs", [])
    data.setdefault("completions", [])
    return data


def _save_cleaning(data):
    """Persist the cleaning data file to disk."""
    _save_json(CLEANING_PATH, data)


def _parse_date(s):
    """Parse YYYY-MM-DD → date, or None if missing/invalid."""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _job_view(job):
    """Return the job with computed rotation fields. `ready` means the job
    is in the active list: never done yet, or its rest interval has elapsed
    since the last completion. Resting jobs carry `returns_on`."""
    today = date.today()
    last_done = _parse_date(job.get("last_done"))
    anchor = last_done or _parse_date(job.get("created_at")) or today
    days_since = max(0, (today - anchor).days)
    interval = max(1, int(job.get("interval_days") or DEFAULT_INTERVAL_DAYS))
    view = dict(job)
    view["days_since"] = days_since
    view["due_ratio"] = round(days_since / interval, 3)
    view["ready"] = last_done is None or days_since >= interval
    if not view["ready"]:
        view["returns_on"] = (last_done + timedelta(days=interval)).isoformat()
    return view


@cleaning_bp.route("/cleaning")
@login_required
def cleaning_page():
    """The End-of-Shift Cleaning page."""
    return render_template("cleaning.html")


@cleaning_bp.route("/api/cleaning/jobs", methods=["GET"])
@login_required
def get_cleaning_jobs():
    """Return all jobs with computed due info, most-overdue first."""
    data = _load_cleaning()
    jobs = [_job_view(j) for j in data["jobs"]]
    # Active (ready) jobs first, most-overdue at the top; among ties,
    # never-done jobs outrank previously-done ones. Resting jobs follow,
    # which the same ratio ordering puts soonest-to-return first.
    jobs.sort(key=lambda j: (0 if j["ready"] else 1, -j["due_ratio"],
                             0 if j["last_done"] is None else 1))
    return jsonify(jobs)


@cleaning_bp.route("/api/cleaning/jobs", methods=["POST"])
@login_required
def create_cleaning_job():
    """Add a job. Body: {title, interval_days, notes?}."""
    body = request.get_json(force=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400
    try:
        interval = int(body.get("interval_days", DEFAULT_INTERVAL_DAYS))
    except (ValueError, TypeError):
        return jsonify({"error": "interval_days must be a number"}), 400
    if interval < 1:
        return jsonify({"error": "interval_days must be at least 1"}), 400
    data = _load_cleaning()
    if any(j["title"].lower() == title.lower() for j in data["jobs"]):
        return jsonify({"error": "A job with that name already exists"}), 409
    job = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "title": title,
        "notes": (body.get("notes") or "").strip(),
        "interval_days": interval,
        "created_at": date.today().isoformat(),
        "last_done": None,
        "last_done_by": None,
    }
    data["jobs"].append(job)
    _save_cleaning(data)
    return jsonify(_job_view(job)), 201


@cleaning_bp.route("/api/cleaning/jobs/<jid>", methods=["PATCH"])
@login_required
def update_cleaning_job(jid):
    """Edit a job's title, notes, or interval_days."""
    body = request.get_json(force=True) or {}
    data = _load_cleaning()
    job = next((j for j in data["jobs"] if j["id"] == jid), None)
    if job is None:
        return jsonify({"error": "Not found"}), 404
    if "title" in body:
        title = (body["title"] or "").strip()
        if not title:
            return jsonify({"error": "Title required"}), 400
        if any(j["title"].lower() == title.lower() and j["id"] != jid for j in data["jobs"]):
            return jsonify({"error": "A job with that name already exists"}), 409
        job["title"] = title
    if "notes" in body:
        job["notes"] = (body["notes"] or "").strip()
    if "interval_days" in body:
        try:
            interval = int(body["interval_days"])
        except (ValueError, TypeError):
            return jsonify({"error": "interval_days must be a number"}), 400
        if interval < 1:
            return jsonify({"error": "interval_days must be at least 1"}), 400
        job["interval_days"] = interval
    _save_cleaning(data)
    return jsonify(_job_view(job))


@cleaning_bp.route("/api/cleaning/jobs/<jid>", methods=["DELETE"])
@login_required
def delete_cleaning_job(jid):
    """Remove a job from the rotation. Its past sign-offs are kept."""
    data = _load_cleaning()
    before = len(data["jobs"])
    data["jobs"] = [j for j in data["jobs"] if j["id"] != jid]
    if len(data["jobs"]) == before:
        return jsonify({"error": "Not found"}), 404
    _save_cleaning(data)
    return jsonify({"ok": True})


@cleaning_bp.route("/api/cleaning/jobs/<jid>/complete", methods=["POST"])
@login_required
def complete_cleaning_job(jid):
    """Sign off a job. Body: {staff, date?, notes?}. Resets the job's clock,
    which sends it to the back of the rotation."""
    body = request.get_json(force=True) or {}
    staff = (body.get("staff") or "").strip()
    if not staff:
        return jsonify({"error": "Staff name required"}), 400
    done_date = _parse_date(body.get("date")) or date.today()
    data = _load_cleaning()
    job = next((j for j in data["jobs"] if j["id"] == jid), None)
    if job is None:
        return jsonify({"error": "Not found"}), 404
    completion = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "job_id": job["id"],
        "job_title": job["title"],
        "staff": staff,
        "date": done_date.isoformat(),
        "notes": (body.get("notes") or "").strip(),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    data["completions"].append(completion)
    _sync_job_last_done(job, data["completions"])
    _save_cleaning(data)
    return jsonify({"ok": True, "completion": completion, "job": _job_view(job)})


@cleaning_bp.route("/api/cleaning/completions", methods=["GET"])
@login_required
def get_cleaning_completions():
    """Recent sign-offs, newest first. ?limit= (default 30)."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 30))))
    except ValueError:
        limit = 30
    data = _load_cleaning()
    comps = sorted(data["completions"], key=lambda c: c.get("ts", ""), reverse=True)
    return jsonify(comps[:limit])


@cleaning_bp.route("/api/cleaning/completions/<cid>", methods=["DELETE"])
@login_required
def delete_cleaning_completion(cid):
    """Undo a sign-off (mis-click). Recomputes the job's last-done from its
    remaining sign-offs so the job returns to its previous queue position."""
    data = _load_cleaning()
    comp = next((c for c in data["completions"] if c["id"] == cid), None)
    if comp is None:
        return jsonify({"error": "Not found"}), 404
    data["completions"] = [c for c in data["completions"] if c["id"] != cid]
    job = next((j for j in data["jobs"] if j["id"] == comp["job_id"]), None)
    if job is not None:
        _sync_job_last_done(job, data["completions"])
    _save_cleaning(data)
    return jsonify({"ok": True})


def _sync_job_last_done(job, completions):
    """Set job.last_done / last_done_by from its most recent completion
    (by done-date, then entry time), or clear them if none remain."""
    mine = [c for c in completions if c["job_id"] == job["id"]]
    if mine:
        latest = max(mine, key=lambda c: (c.get("date", ""), c.get("ts", "")))
        job["last_done"] = latest["date"]
        job["last_done_by"] = latest["staff"]
    else:
        job["last_done"] = None
        job["last_done_by"] = None

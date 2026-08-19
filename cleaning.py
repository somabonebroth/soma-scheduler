"""cleaning.py — Cleaning & Upkeep: the daily closing gate + the rotating job pool.

Self-contained blueprint (suppliers.py pattern): owns its data file and
helpers, defines a local login_required, pulls IO from helpers.py.

Two DIFFERENT contracts live here, deliberately kept apart in the UI:

  * Closing checklist — a daily GATE. Same fixed items every day, all of them
    required before anyone leaves, ticked per item and signed once. One record
    per date. This is housekeeping and security, NOT food safety: CCP/HACCP
    lives in the daily production checklist (ccp_master.json) and the two must
    not be merged — mixing housekeeping into a controlled CCP document dilutes
    it, and an unticked "mop the floor" would surface as a CCP alarm.
  * Rotating jobs — a BACKLOG. Skipping is expected and fine (see below).

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

# Seed for the daily closing gate. Housekeeping, waste and security only —
# anything food-safety critical belongs in the CCP checklist, not here.
DEFAULT_CLOSING_ITEMS = [
    {"id": "c1", "label": "All product moved to fridge, freezer or storage"},
    {"id": "c2", "label": "Work surfaces cleaned and sanitised"},
    {"id": "c3", "label": "Kettles and vessels emptied, washed, sanitised"},
    {"id": "c4", "label": "Sinks emptied; dishes done and put away"},
    {"id": "c5", "label": "Floors swept and mopped"},
    {"id": "c6", "label": "Drains cleared and sanitised"},
    {"id": "c7", "label": "Waste and compost taken out; bins relined"},
    {"id": "c8", "label": "Fridge and freezer doors closed and sealed"},
    {"id": "c9", "label": "Gas and burners off; equipment powered down"},
    {"id": "c10", "label": "Lights off; doors and windows locked"},
]


def slot_for(interval_days):
    """Group an interval into its rotation slot. The floor reads the list by
    slot, so Weekly work never sits visually mixed in with six-month work."""
    days = max(1, int(interval_days or DEFAULT_INTERVAL_DAYS))
    if days <= 7:
        return "weekly"
    if days <= 30:
        return "monthly"
    if days <= 90:
        return "quarterly"
    return "semiannual"


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
    """Local copy of app.py's manager_required (verbatim). The floor DOES the
    closing list; only the manager defines what's on it."""
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


def _load_cleaning():
    """Load the cleaning data file
    ({jobs, completions, closing_items, closing_records, declines})."""
    data = _load_json(CLEANING_PATH, {})
    data.setdefault("jobs", [])
    data.setdefault("completions", [])
    data.setdefault("closing_items", [dict(i) for i in DEFAULT_CLOSING_ITEMS])
    data.setdefault("closing_records", [])
    data.setdefault("declines", [])
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
    view["slot"] = slot_for(interval)
    if not view["ready"]:
        view["returns_on"] = (last_done + timedelta(days=interval)).isoformat()
    return view


@cleaning_bp.route("/cleaning")
@login_required
def cleaning_page():
    """Cleaning & Upkeep: tonight's closing gate + the rotating job pool."""
    role = (session.get("role") or "manager") if session.get("authenticated") else None
    return render_template("cleaning.html", role=role)


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


def day_summary(on):
    """What happened on the cleaning side on one date, for the daily brief.

    Returns the closing gate's state (signed / complete / which items were
    missed), the note the floor left for management, the rotation sign-offs
    recorded that day (or that a job was declined), and how many rotating jobs
    are currently overdue. `on` is a date object.
    """
    data = _load_cleaning()
    iso = on.isoformat()

    rec = next((r for r in data["closing_records"] if r.get("date") == iso), None)
    if rec is None:
        closing = {"signed": False, "complete": False, "staff": "", "notes": "",
                   "missed": [], "done_count": 0, "total": len(data["closing_items"])}
    else:
        items = rec.get("items") or []
        missed = [i["label"] for i in items if not i.get("done")]
        closing = {
            "signed": bool(rec.get("staff")),
            "complete": bool(rec.get("complete")),
            "staff": rec.get("staff", ""),
            "notes": rec.get("notes", ""),
            "missed": missed,
            "done_count": len(items) - len(missed),
            "total": len(items),
        }

    jobs_done = [{"title": c.get("job_title", ""), "staff": c.get("staff", ""),
                  "notes": c.get("notes", "")}
                 for c in data["completions"] if c.get("date") == iso]

    declined = next((d for d in data["declines"] if d.get("date") == iso), None)

    overdue = 0
    for job in data["jobs"]:
        view = _job_view(job)
        if view["ready"] and view.get("last_done") and view["due_ratio"] >= 1:
            overdue += 1

    return {"closing": closing, "jobs_done": jobs_done, "jobs_overdue": overdue,
            "declined": bool(declined),
            "declined_by": (declined or {}).get("staff", ""),
            "manager_note": (rec or {}).get("manager_note", "")}


# ── Closing checklist (the daily gate) ────────────────────────────────────────

def _closing_record_view(rec, items):
    """Return the record merged against the CURRENT item list for display.

    A saved record snapshots the labels it was signed against, so editing the
    closing list never rewrites what someone signed last month. For today's
    in-progress record we still surface newly added items as unticked.
    """
    done = {i.get("id"): bool(i.get("done")) for i in (rec.get("items") or [])}
    merged = [{"id": i["id"], "label": i["label"], "done": done.get(i["id"], False)}
              for i in items]
    view = dict(rec)
    view["items"] = merged
    view["complete"] = bool(merged) and all(i["done"] for i in merged)
    return view


@cleaning_bp.route("/api/cleaning/closing/items", methods=["GET"])
@login_required
def get_closing_items():
    """The closing checklist definition. The floor needs it to render the list."""
    return jsonify(_load_cleaning()["closing_items"])


@cleaning_bp.route("/api/cleaning/closing/items", methods=["PUT"])
@manager_required
def update_closing_items():
    """Replace the closing checklist definition. Body: [{id?, label}, ...].
    Ids are kept where given so history still lines up; new rows get one."""
    body = request.get_json(force=True)
    if not isinstance(body, list):
        return jsonify({"error": "Expected a list of items"}), 400
    items, seen = [], set()
    for idx, raw in enumerate(body):
        label = (raw.get("label") or "").strip() if isinstance(raw, dict) else str(raw).strip()
        if not label:
            continue
        iid = (raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        if not iid or iid in seen:
            iid = datetime.now().strftime("%Y%m%d%H%M%S%f") + "-" + str(idx)
        seen.add(iid)
        items.append({"id": iid, "label": label})
    if not items:
        return jsonify({"error": "At least one item required"}), 400
    data = _load_cleaning()
    data["closing_items"] = items
    _save_cleaning(data)
    return jsonify({"ok": True, "items": items})


@cleaning_bp.route("/api/cleaning/closing", methods=["GET"])
@login_required
def get_closing_record():
    """Today's closing record (?date= for another day), merged against the
    current item list. Returns an empty unticked shell when nothing is saved."""
    data = _load_cleaning()
    on = _parse_date(request.args.get("date")) or date.today()
    rec = next((r for r in data["closing_records"] if r.get("date") == on.isoformat()), None)
    if rec is None:
        rec = {"date": on.isoformat(), "staff": "", "notes": "", "items": []}
    return jsonify(_closing_record_view(rec, data["closing_items"]))


@cleaning_bp.route("/api/cleaning/closing", methods=["POST"])
@login_required
def save_closing_record():
    """Sign off the closing list. Body: {staff, done: {item_id: bool}, notes?, date?}.

    One record per date — saving again updates it (someone finishes the last
    few items later) while keeping the original created_at.
    """
    body = request.get_json(force=True) or {}
    staff = (body.get("staff") or "").strip()
    if not staff:
        return jsonify({"error": "Staff name required"}), 400
    on = (_parse_date(body.get("date")) or date.today()).isoformat()
    done = body.get("done") or {}

    data = _load_cleaning()
    # Snapshot the labels signed against, so a later edit to the list
    # cannot rewrite history.
    items = [{"id": i["id"], "label": i["label"], "done": bool(done.get(i["id"]))}
             for i in data["closing_items"]]
    rec = next((r for r in data["closing_records"] if r.get("date") == on), None)
    now = datetime.now().isoformat(timespec="seconds")
    if rec is None:
        rec = {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "date": on, "created_at": now}
        data["closing_records"].append(rec)
    rec["items"] = items
    rec["staff"] = staff
    rec["notes"] = (body.get("notes") or "").strip()
    rec["complete"] = bool(items) and all(i["done"] for i in items)
    rec["ts"] = now
    _save_cleaning(data)
    return jsonify({"ok": True, "record": rec})


@cleaning_bp.route("/api/cleaning/closing/records", methods=["GET"])
@manager_required
def get_closing_records():
    """Closing history, newest first. ?limit= (default 30), ?from=&to= (dates)."""
    try:
        limit = max(1, min(500, int(request.args.get("limit", 30))))
    except ValueError:
        limit = 30
    frm = _parse_date(request.args.get("from"))
    to = _parse_date(request.args.get("to"))
    recs = _load_cleaning()["closing_records"]
    if frm:
        recs = [r for r in recs if (r.get("date") or "") >= frm.isoformat()]
    if to:
        recs = [r for r in recs if (r.get("date") or "") <= to.isoformat()]
    recs = sorted(recs, key=lambda r: r.get("date", ""), reverse=True)
    return jsonify(recs[:limit])


@cleaning_bp.route("/api/cleaning/closing/note", methods=["POST"])
@login_required
def save_closing_note():
    """Leave the floor's note for management. Body: {note, staff?, date?}.

    Lives on the day's closing record rather than the production checklist so a
    non-production day can still carry a note, and so there is exactly one place
    tomorrow's brief has to look. Written before the closing list is signed —
    the End of Day wizard asks for it early — so this upserts the record.
    """
    body = request.get_json(force=True) or {}
    on = (_parse_date(body.get("date")) or date.today()).isoformat()
    data = _load_cleaning()
    rec = next((r for r in data["closing_records"] if r.get("date") == on), None)
    now = datetime.now().isoformat(timespec="seconds")
    if rec is None:
        rec = {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "date": on,
               "created_at": now, "staff": "", "notes": "", "items": []}
        data["closing_records"].append(rec)
    rec["manager_note"] = (body.get("note") or "").strip()
    if body.get("staff"):
        rec["manager_note_by"] = (body.get("staff") or "").strip()
    rec["manager_note_ts"] = now
    _save_cleaning(data)
    return jsonify({"ok": True, "note": rec["manager_note"]})


@cleaning_bp.route("/api/cleaning/rotation/decline", methods=["POST"])
@login_required
def decline_rotation():
    """Record that no rotating job was taken tonight. Body: {staff?, date?}.

    No reason is asked for — skipping is expected and carries no penalty (see
    the module docstring). It surfaces on tomorrow's brief as a plain note, not
    an issue, so management can see the frequency without it reading as a fault.
    One record per date; declining again just refreshes it.
    """
    body = request.get_json(force=True) or {}
    on = (_parse_date(body.get("date")) or date.today()).isoformat()
    data = _load_cleaning()
    rec = next((d for d in data["declines"] if d.get("date") == on), None)
    if rec is None:
        rec = {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"), "date": on}
        data["declines"].append(rec)
    rec["staff"] = (body.get("staff") or "").strip()
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
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

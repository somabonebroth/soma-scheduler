"""daily_brief.py — the manager's morning brief: what happened yesterday.

Why this exists: a 2026-08-18 audit of everything the system writes each day
found that the capture is good but almost nothing has a DAILY reader. The
floor's day notes reached the HOO up to a week later (only via the weekly
sign-off modal), and the per-vessel `finish_notes` typed after every batch had
no reader anywhere in the codebase at all. This blueprint gives that record a
reader the next morning.

It creates NO new artifacts. Every number here is read back from records that
already existed — production checklists, the cleaning file, sales, raw
materials, production runs — aggregated for one date.

Composition, deliberately: the per-domain summaries live with their domain
(`production.summarize_day`, `cleaning.day_summary`) and this module only
joins them. The weekly HOO review calls the same `summarize_day`, so the two
views of a day can never drift apart.

Pattern: routes-move / helpers-stay (buyers.py) — bare `import app` at module
top, `app.X(...)` resolved at request time; foundation IO from helpers.
Manager-only throughout: this is the HOO's desk, not the production tablet.
"""
import logging
from functools import wraps
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify, session, redirect, url_for

from helpers import _load_json
import app
import production
import cleaning

logger = logging.getLogger(__name__)

daily_brief_bp = Blueprint("daily_brief", __name__)


def manager_required(f):
    """Local copy of app.py's manager_required (verbatim) — the brief is the
    HOO's morning read, never the production tablet's."""
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


def _parse_date(s):
    """Parse YYYY-MM-DD → date, or None if missing/invalid."""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _week_coords(on):
    """(week_id, day_idx) for a date. week_id is the Monday of its week and
    day_idx is 0=Monday, matching app.DAYS and the schedule/checklist files."""
    monday = on - timedelta(days=on.weekday())
    return monday.isoformat(), on.weekday()


def _production_section(on):
    """Yesterday's production day, or an empty shell when nothing was completed."""
    week_id, day_idx = _week_coords(on)
    day = production.summarize_day(week_id, day_idx)
    if day is None:
        return {"completed": False, "week_id": week_id, "day_idx": day_idx,
                "produced": {}, "notes": "", "finish_notes": [],
                "kitchen_signoff": "", "ccp_issues": [], "scheduled": []}
    day["completed"] = True
    day["week_id"] = week_id
    return day


def _sales_section(on):
    """Sales recorded for that date, grouped into a total and a per-buyer split.

    Keys off sale_date (the business date on the record), falling back to the
    created_at stamp for records saved without one.
    """
    iso = on.isoformat()
    rows = []
    for s in _load_json(app.ORGANIC_SALES_PATH, []):
        when = (s.get("sale_date") or "")[:10] or (s.get("created_at") or "")[:10]
        if when != iso:
            continue
        rows.append(s)
    by_buyer = {}
    units = 0
    for s in rows:
        try:
            qty = int(s.get("quantity") or 0)
        except (ValueError, TypeError):
            qty = 0
        units += qty
        buyer = (s.get("buyer") or "").strip() or "Unspecified"
        by_buyer[buyer] = by_buyer.get(buyer, 0) + qty
    return {"count": len(rows), "units": units,
            "by_buyer": sorted(by_buyer.items(), key=lambda kv: -kv[1])}


def _receiving_section(on):
    """Raw material logged as received on that date, one row per lot."""
    iso = on.isoformat()
    out = []
    for m in _load_json(app.ORGANIC_RAW_PATH, []):
        if (m.get("date_received") or "")[:10] != iso:
            continue
        out.append({
            "item": m.get("item", ""),
            "supplier": m.get("supplier", ""),
            "quantity": m.get("quantity"),
            "unit": m.get("unit", ""),
            "supplier_lot": m.get("supplier_lot", ""),
            "no_supplier_lot": bool(m.get("no_supplier_lot")),
        })
    return out


def _stock_exceptions_section(on):
    """Batches produced on that date with LESS raw material on file than the
    recipe required (the INSUFFICIENT_STOCK markers). Same source as the
    stock-exceptions panel on Completed Production, narrowed to one day."""
    iso = on.isoformat()
    out = []
    for run in _load_json(app.ORGANIC_RUNS_PATH, []):
        if run.get("status") != "completed":
            continue
        if app._run_start_date_str(run.get("week_id"), run.get("day_idx")) != iso:
            continue
        for used in (run.get("ingredients_used") or []):
            if not used.get("negative"):
                continue
            out.append({
                "recipe": run.get("recipe", ""),
                "vessel": run.get("vessel", ""),
                "ingredient": used.get("item", ""),
                "shortfall": used.get("quantity_used"),
                "unit": used.get("unit", ""),
            })
    return out


def _kitchen_ran(prod, clean):
    """Did the kitchen run that day? Completed production, or any cleaning
    sign-off (closing or a rotation job).

    This is what makes a day REVIEWABLE. A quiet day needs no HOO review and
    raises no missing-closing flag — the system can't tell a quiet day from a
    forgotten one, and flagging every weekend would bury the real flags.
    """
    if prod.get("completed"):
        return True
    if clean.get("jobs_done"):
        return True
    return bool((clean.get("closing") or {}).get("signed"))


def _build_actions(prod, clean, exceptions, receiving):
    """The 'needs your attention' list — the whole point of the brief.

    Ordered by consequence: food safety first, then traceability gaps, then
    housekeeping. Anything here is something the HOO would otherwise only find
    at the end of the week, or never.
    """
    actions = []
    for issue in prod.get("ccp_issues", []):
        actions.append({"level": "high", "area": "CCP",
                        "text": "CCP section not confirmed: " + issue})
    if prod.get("completed") and prod.get("scheduled") and not prod.get("kitchen_signoff"):
        actions.append({"level": "high", "area": "Production",
                        "text": "No kitchen sign-off on the production record"})
    for ex in exceptions:
        actions.append({"level": "high", "area": "Stock",
                        "text": "%s made with insufficient %s on file" % (ex["recipe"], ex["ingredient"])})
    for r in receiving:
        if r["no_supplier_lot"]:
            actions.append({"level": "medium", "area": "Receiving",
                            "text": "%s from %s received with no supplier lot#" % (r["item"], r["supplier"] or "unknown supplier")})
    closing = clean.get("closing", {})
    # Only chase a missing closing sign-off on a day we KNOW the kitchen ran.
    # The system can't tell a quiet day from a forgotten one, and flagging every
    # weekend and every pre-launch date would bury the flags that matter.
    kitchen_ran = _kitchen_ran(prod, clean)
    if not closing.get("signed"):
        if kitchen_ran:
            actions.append({"level": "medium", "area": "Closing",
                            "text": "Closing checklist was not signed off"})
    elif not closing.get("complete"):
        missed = closing.get("missed", [])
        text = "Closing incomplete: " + " · ".join(missed[:3])
        if len(missed) > 3:
            text += " (+%d more)" % (len(missed) - 3)
        actions.append({"level": "medium", "area": "Closing", "text": text})
    return actions


@daily_brief_bp.route("/api/daily-brief", methods=["GET"])
@manager_required
def get_daily_brief():
    """The morning brief for a date (?date=YYYY-MM-DD, default yesterday).

    Read-only: every field is re-read from existing records, nothing is written.
    """
    on = _parse_date(request.args.get("date")) or (date.today() - timedelta(days=1))

    prod = _production_section(on)
    try:
        clean = cleaning.day_summary(on)
    except Exception:
        logger.warning("daily brief: cleaning summary failed", exc_info=True)
        clean = {"closing": {}, "jobs_done": [], "jobs_overdue": 0}
    sales = _sales_section(on)
    receiving = _receiving_section(on)
    exceptions = _stock_exceptions_section(on)
    actions = _build_actions(prod, clean, exceptions, receiving)

    notes = []
    if prod.get("notes"):
        notes.append({"source": "Day note", "text": prod["notes"]})
    for fn in prod.get("finish_notes", []):
        notes.append({"source": fn["vessel"], "text": fn["note"]})
    if (clean.get("closing") or {}).get("notes"):
        notes.append({"source": "Closing", "text": clean["closing"]["notes"]})
    for j in clean.get("jobs_done", []):
        if j.get("notes"):
            notes.append({"source": j["title"], "text": j["notes"]})

    signoffs = app._load_daily_signoffs()
    return jsonify({
        "date": on.isoformat(),
        "day_name": on.strftime("%A"),
        "label": on.strftime("%A %d %B"),
        "signoff": signoffs.get(on.isoformat()),
        "needs_review": _kitchen_ran(prod, clean),
        "production": prod,
        "cleaning": clean,
        "sales": sales,
        "receiving": receiving,
        "stock_exceptions": exceptions,
        "notes": notes,
        "actions": actions,
        "all_clear": not actions,
    })


@daily_brief_bp.route("/api/daily-signoff/<on_date>", methods=["POST"])
@manager_required
def sign_off_day(on_date):
    """HOO confirms they have read and actioned a day's brief. Body: {name, notes?}.

    Deliberately NOT gated on the day being problem-free: the brief exists to be
    read and acted on, and blocking the signature would just leave days unsigned
    while the real work happened elsewhere. What was flagged at signing time is
    snapshotted so the record shows what was reviewed.
    """
    on = _parse_date(on_date)
    if on is None:
        return jsonify({"error": "Invalid date"}), 400
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required to sign off"}), 400

    prod = _production_section(on)
    try:
        clean = cleaning.day_summary(on)
    except Exception:
        logger.warning("daily sign-off: cleaning summary failed", exc_info=True)
        clean = {"closing": {}, "jobs_done": [], "jobs_overdue": 0}
    actions = _build_actions(prod, clean, _stock_exceptions_section(on), _receiving_section(on))

    signoffs = app._load_daily_signoffs()
    signoffs[on.isoformat()] = {
        "name": name,
        "notes": (body.get("notes") or "").strip(),
        "signed_at": datetime.now().isoformat(timespec="seconds"),
        "open_actions": len(actions),
    }
    app._save_daily_signoffs(signoffs)
    return jsonify({"success": True, "signoff": signoffs[on.isoformat()]})


@daily_brief_bp.route("/api/daily-signoff/<on_date>", methods=["DELETE"])
@manager_required
def unsign_day(on_date):
    """Reverse a daily review (mis-click); the HOO can re-sign at any time."""
    on = _parse_date(on_date)
    if on is None:
        return jsonify({"error": "Invalid date"}), 400
    signoffs = app._load_daily_signoffs()
    if on.isoformat() in signoffs:
        del signoffs[on.isoformat()]
        app._save_daily_signoffs(signoffs)
    return jsonify({"success": True})


@daily_brief_bp.route("/api/daily-signoffs/pending", methods=["GET"])
@manager_required
def pending_reviews():
    """Days the kitchen ran that have no HOO review yet, oldest first.

    Looks back ?days= (default 14, max 60) and never past today. Drives the
    catch-up line on the brief and the dashboard badge, so a missed morning
    surfaces instead of quietly ageing out.
    """
    try:
        window = max(1, min(60, int(request.args.get("days", 14))))
    except ValueError:
        window = 14
    signoffs = app._load_daily_signoffs()
    today = date.today()
    pending = []
    for back in range(1, window + 1):
        on = today - timedelta(days=back)
        if on.isoformat() in signoffs:
            continue
        prod = _production_section(on)
        try:
            clean = cleaning.day_summary(on)
        except Exception:
            clean = {"closing": {}, "jobs_done": [], "jobs_overdue": 0}
        if _kitchen_ran(prod, clean):
            pending.append(on.isoformat())
    pending.sort()
    return jsonify({"pending": pending, "count": len(pending), "days_scanned": window})

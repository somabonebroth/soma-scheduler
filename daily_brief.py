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

from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template

from helpers import _load_json, _sku_display
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


def _production_section(on):
    """SECTION 1 — what was made: jars completed that day, with totals.

    Sourced from FINISHED GOODS, not the checklist's raw `produced` map, and
    deliberately so. Jar counts entered on day N complete the runs STARTED on
    day N-1 (`_check_organic_completion`), so reading `produced` against the
    same day's schedule can attribute jars to the wrong recipe when a vessel
    runs different recipes on consecutive days. FG entries were written by the
    run itself, so their recipe/format/lot/certification are correct by
    construction. Each row carries the batch date when it differs from the day
    the jars were counted.

    Falls back to the checklist's own numbers for older days that predate the
    runs/FG chain, labelled via `source` so the page can say which it is.
    """
    week_id, day_idx = _week_coords(on)
    day = production.summarize_day(week_id, day_idx)

    rows = []
    for f in _load_json(app.ORGANIC_FG_PATH, []):
        if f.get("week_id") != week_id or f.get("day_idx") != day_idx:
            continue
        try:
            qty = int(f.get("quantity_produced") or 0)
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue
        batched_on = ""
        if f.get("start_week_id") is not None and f.get("start_day_idx") is not None:
            batched_on = app._run_start_date_str(f.get("start_week_id"), f.get("start_day_idx")) or ""
        rows.append({
            "item": _sku_display(f.get("brand", ""), f.get("recipe", ""), f.get("format", "")),
            "recipe": f.get("recipe", ""),
            "format": f.get("format", ""),
            "certification": f.get("certification", ""),
            "lot": f.get("lot", ""),
            "vessel": f.get("vessel", ""),
            "quantity": qty,
            "batched_on": batched_on,
        })
    rows.sort(key=lambda r: (-r["quantity"], r["item"]))

    source = "finished_goods"
    if not rows and day and day.get("produced"):
        source = "checklist"
        for recipe, qty in sorted(day["produced"].items(), key=lambda kv: -kv[1]):
            rows.append({"item": recipe, "recipe": recipe, "format": "", "certification": "",
                         "lot": "", "vessel": "", "quantity": qty, "batched_on": ""})

    totals = {}
    for r in rows:
        totals[r["item"]] = totals.get(r["item"], 0) + r["quantity"]

    exceptions = _stock_exceptions_section(on)
    notes, issues = [], []
    if day:
        if day.get("notes"):
            notes.append({"source": "Day note", "text": day["notes"]})
        for fn in day.get("finish_notes", []):
            notes.append({"source": fn["vessel"], "text": fn["note"]})
        if day.get("scheduled") and not day.get("kitchen_signoff"):
            issues.append({"level": "high", "text": "No kitchen sign-off on the production record"})
    for ex in exceptions:
        issues.append({"level": "high",
                       "text": "%s made with insufficient %s on file (short %s %s)" % (
                           ex["recipe"], ex["ingredient"], ex["shortfall"], ex["unit"])})

    return {
        "completed": bool(day),
        "week_id": week_id,
        "day_idx": day_idx,
        "source": source,
        "rows": rows,
        "totals": sorted(totals.items(), key=lambda kv: -kv[1]),
        "total_jars": sum(r["quantity"] for r in rows),
        "scheduled": (day or {}).get("scheduled", []),
        "kitchen_signoff": (day or {}).get("kitchen_signoff", ""),
        "stock_exceptions": exceptions,
        "notes": notes,
        "issues": issues,
        # kept flat for the checklists section and the review queue
        "ccp_issues": (day or {}).get("ccp_issues", []),
        "ccp_sections": (day or {}).get("ccp_sections", []),
        "ccp_confirmed": (day or {}).get("ccp_confirmed", 0),
        "ccp_total": (day or {}).get("ccp_total", 0),
        "finish_notes": (day or {}).get("finish_notes", []),
        "day_note": (day or {}).get("notes", ""),
    }


def _transactions_section(on):
    """SECTION 2 — what was sold and received: the day's transactions."""
    iso = on.isoformat()

    sales_rows = []
    for s in _load_json(app.ORGANIC_SALES_PATH, []):
        when = (s.get("sale_date") or "")[:10] or (s.get("created_at") or "")[:10]
        if when != iso:
            continue
        try:
            qty = int(s.get("quantity") or 0)
        except (ValueError, TypeError):
            qty = 0
        item = _sku_display(s.get("brand", ""), s.get("recipe", ""), s.get("format", "")) \
            if s.get("recipe") else (s.get("sku_key") or "")
        sales_rows.append({
            "item": item,
            "buyer": (s.get("buyer") or "").strip() or "Unspecified",
            "quantity": qty,
            "certification": s.get("certification", ""),
            "lots": [l.get("lot", "") for l in (s.get("lots") or []) if l.get("lot")],
            "order_id": s.get("order_id", ""),
            "channel": s.get("channel", ""),
        })
    sales_rows.sort(key=lambda r: (r["buyer"], -r["quantity"]))

    by_buyer = {}
    for r in sales_rows:
        by_buyer[r["buyer"]] = by_buyer.get(r["buyer"], 0) + r["quantity"]

    receiving_rows = _receiving_section(on)

    issues = []
    for r in receiving_rows:
        if r["no_supplier_lot"]:
            issues.append({"level": "medium",
                           "text": "%s from %s received with no supplier lot#" % (
                               r["item"], r["supplier"] or "unknown supplier")})
    return {
        "sales": {
            "rows": sales_rows,
            "count": len(sales_rows),
            "units": sum(r["quantity"] for r in sales_rows),
            "by_buyer": sorted(by_buyer.items(), key=lambda kv: -kv[1]),
        },
        "receiving": {"rows": receiving_rows, "count": len(receiving_rows)},
        "notes": [],
        "issues": issues,
    }


def _checklists_section(on, prod):
    """SECTION 3 — the day's checklists: CCP, closing gate, and the rotation."""
    try:
        clean = cleaning.day_summary(on)
    except Exception:
        logger.warning("daily brief: cleaning summary failed", exc_info=True)
        clean = {"closing": {}, "jobs_done": [], "jobs_overdue": 0}
    closing = clean.get("closing") or {}

    notes, issues = [], []
    for issue in prod.get("ccp_issues", []):
        issues.append({"level": "high", "text": "CCP section not confirmed: " + issue})
    if closing.get("notes"):
        notes.append({"source": "Closing", "text": closing["notes"]})
    for j in clean.get("jobs_done", []):
        if j.get("notes"):
            notes.append({"source": j.get("title", "Cleaning job"), "text": j["notes"]})

    kitchen_ran = _kitchen_ran(prod, clean)
    if not closing.get("signed"):
        # Only chase a missing closing sign-off on a day we KNOW the kitchen ran.
        # The system can't tell a quiet day from a forgotten one, and flagging
        # every weekend would bury the flags that matter.
        if kitchen_ran:
            issues.append({"level": "medium", "text": "Closing checklist was not signed off"})
    elif not closing.get("complete"):
        missed = closing.get("missed", [])
        text = "Closing incomplete — missed: " + " · ".join(missed[:3])
        if len(missed) > 3:
            text += " (+%d more)" % (len(missed) - 3)
        issues.append({"level": "medium", "text": text})

    return {
        "ccp": {
            "completed": prod.get("completed", False),
            "sections": prod.get("ccp_sections", []),
            "confirmed": prod.get("ccp_confirmed", 0),
            "total": prod.get("ccp_total", 0),
            "kitchen_signoff": prod.get("kitchen_signoff", ""),
        },
        "closing": closing,
        "rotation": {"jobs_done": clean.get("jobs_done", []),
                     "overdue": clean.get("jobs_overdue", 0)},
        "notes": notes,
        "issues": issues,
        "_kitchen_ran": kitchen_ran,
    }


def _build_brief(on):
    """Assemble the three sections plus the flat action list they imply."""
    prod = _production_section(on)
    txn = _transactions_section(on)
    checks = _checklists_section(on, prod)

    # Flat list drives the dashboard button, the badge and the sign-off
    # snapshot. Ordered by consequence: food safety, then traceability, then
    # housekeeping — which is also the section order.
    actions = []
    for area, sec in (("Checklists", checks), ("Production", prod), ("Sales & Receiving", txn)):
        for i in sec["issues"]:
            actions.append({"level": i["level"], "area": area, "text": i["text"]})
    actions.sort(key=lambda a: 0 if a["level"] == "high" else 1)

    notes = prod["notes"] + txn["notes"] + checks["notes"]
    signoffs = app._load_daily_signoffs()
    return {
        "date": on.isoformat(),
        "day_name": on.strftime("%A"),
        "label": on.strftime("%A %d %B"),
        "production": prod,
        "transactions": txn,
        "checklists": checks,
        "notes": notes,
        "actions": actions,
        "all_clear": not actions,
        "signoff": signoffs.get(on.isoformat()),
        "needs_review": checks["_kitchen_ran"],
    }


@daily_brief_bp.route("/api/daily-brief", methods=["GET"])
@manager_required
def get_daily_brief():
    """The daily review for a date (?date=YYYY-MM-DD, default yesterday).

    Read-only: every field is re-read from existing records, nothing is written.
    """
    on = _parse_date(request.args.get("date")) or (date.today() - timedelta(days=1))
    return jsonify(_build_brief(on))


@daily_brief_bp.route("/daily-review")
@manager_required
def daily_review_page():
    """The full Daily Review page — the three sections, then sign-off."""
    return render_template("daily_review.html")


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

    actions = _build_brief(on)["actions"]

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
        if _build_brief(on)["needs_review"]:
            pending.append(on.isoformat())
    pending.sort()
    return jsonify({"pending": pending, "count": len(pending), "days_scanned": window})

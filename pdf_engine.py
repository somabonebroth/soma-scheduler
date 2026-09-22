"""
Soma Bone Broth - PDF Engine v5
Label generation, simplified section-level checklist.
"""

from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import inch
from datetime import datetime, timedelta

from helpers import _lot_for_batch_date

DARK = HexColor("#1a1a2e")
ACCENT = HexColor("#4a6741")
LIGHT_BG = HexColor("#f5f5f0")
MEDIUM_GRAY = HexColor("#999999")
LIGHT_GRAY = HexColor("#e0e0e0")
ROW_ALT = HexColor("#f0f0ea")
WARNING_BG = HexColor("#fff3cd")
CHECK_GREEN = HexColor("#2d8a4e")
HEADER_TEXT = white
FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
VESSELS = ["K1", "K2", "K3", "115L"]


def draw_header(c, width, height, title, subtitle="", logo_path=None):
    """Draw the standard page header (logo + title) on a reportlab canvas."""
    header_h = 55
    c.setFillColor(DARK)
    c.rect(0, height - header_h, width, header_h, fill=1, stroke=0)
    c.setFillColor(ACCENT)
    c.rect(0, height - header_h - 3, width, 3, fill=1, stroke=0)
    text_x = 30
    if logo_path:
        try:
            logo = ImageReader(logo_path)
            c.drawImage(logo, 20, height - header_h + 7, width=40, height=40, mask='auto')
            text_x = 70
        except Exception:
            pass
    c.setFillColor(HEADER_TEXT)
    c.setFont(FONT_BOLD, 16)
    c.drawString(text_x, height - 24, "SOMA BONE BROTH")
    c.setFont(FONT, 10)
    c.drawString(text_x, height - 42, title)
    if subtitle:
        c.setFillColor(HexColor("#aaaaaa"))
        c.setFont(FONT, 9)
        c.drawRightString(width - 30, height - 42, subtitle)


def _wrap_text(text, size, max_width):
    """Wrap text to a max width; return the list of lines."""
    words = text.split()
    lines = []
    current = ""
    max_chars = int(max_width / (size * 0.45))
    for word in words:
        test = (current + " " + word).strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fmt_ingredient(item):
    """Format a structured ingredient dict or legacy string for PDF rendering."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    amount = item.get("amount", 0)
    unit = item.get("unit", "") or ""
    name = item.get("name", "") or ""
    process = item.get("process", "") or ""
    parts = []
    if amount not in (0, None, ""):
        if isinstance(amount, float) and amount == int(amount):
            parts.append(str(int(amount)))
        else:
            parts.append(str(amount))
    if unit:
        parts.append(unit)
    if name:
        parts.append(name)
    base = " ".join(parts) if parts else name
    if process:
        return base + " \u2014 " + process
    return base


def estimate_card_height(recipe_data, card_w):
    """Estimate the rendered height of a recipe card."""
    n_items = sum(len(recipe_data.get(k, [])) for k in ["kettle_overnight", "after_skim", "finishing", "add_to_jar"])
    n_sections = sum(1 for k in ["kettle_overnight", "after_skim", "finishing", "add_to_jar"] if recipe_data.get(k))
    si_lines = sum(len(_wrap_text(inst, 7, card_w - 20)) for inst in recipe_data.get("special_instructions", []))
    return 22 + si_lines * 10 + 8 + n_items * 11 + n_sections * 14 + 10


def draw_recipe_card(c, x, y, card_w, recipe_name, recipe_data, vessel=""):
    """Draw a single recipe card on the canvas."""
    start_y = y
    margin = 6
    header_h = 22
    c.setFillColor(ACCENT)
    c.rect(x, y - header_h, card_w, header_h, fill=1, stroke=0)
    c.setFillColor(HEADER_TEXT)
    c.setFont(FONT_BOLD, 9)
    brand = (recipe_data.get("brand") or "").strip()
    title_parts = []
    if vessel:
        title_parts.append(vessel)
    if brand:
        title_parts.append(brand)
    title_parts.append(recipe_name)
    c.drawString(x + margin, y - 15, "  |  ".join(title_parts))
    fmt = recipe_data.get("format", "")
    target = recipe_data.get("yield", "")
    c.setFont(FONT, 8)
    c.drawRightString(x + card_w - margin, y - 15, str(fmt) + "  |  Target: " + str(target) + " units")
    y = y - header_h

    special = recipe_data.get("special_instructions", [])
    if special:
        inner_w = card_w - margin * 2
        si_lines = []
        for inst in special:
            si_lines.extend(_wrap_text(inst, 7, inner_w - 10))
        si_h = len(si_lines) * 10 + 8
        c.setFillColor(WARNING_BG)
        c.rect(x, y - si_h, card_w, si_h, fill=1, stroke=0)
        c.setFillColor(black)
        c.setFont(FONT_BOLD, 7)
        ty = y - 10
        for line in si_lines:
            c.drawString(x + margin + 2, ty, line)
            ty = ty - 10
        y = y - si_h

    sections = [
        ("Add to kettle overnight", recipe_data.get("kettle_overnight", [])),
        ("Add directly to kettle after skim", recipe_data.get("after_skim", [])),
        ("Finishing", recipe_data.get("finishing", [])),
        ("Add to jar / container", recipe_data.get("add_to_jar", [])),
    ]
    line_h = 11
    for sec_title, items in sections:
        if not items:
            continue
        c.setFillColor(DARK)
        c.rect(x, y - 14, card_w, 14, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, 7)
        c.drawString(x + margin, y - 10, sec_title)
        y = y - 14
        for i, item in enumerate(items):
            bg = ROW_ALT if i % 2 == 0 else white
            c.setFillColor(bg)
            c.rect(x, y - line_h, card_w, line_h, fill=1, stroke=0)
            c.setStrokeColor(LIGHT_GRAY)
            c.line(x, y - line_h, x + card_w, y - line_h)
            c.setFillColor(black)
            c.setFont(FONT, 7)
            c.drawString(x + margin + 2, y - 8, _fmt_ingredient(item))
            cx = x + card_w - margin - 10
            c.setFillColor(white)
            c.rect(cx, y - line_h + 1, 9, 9, fill=1, stroke=1)
            y = y - line_h

    total_h = start_y - y
    c.setStrokeColor(ACCENT)
    c.setLineWidth(1)
    c.rect(x, y, card_w, total_h, fill=0, stroke=1)
    return y


# -- Label PDF --
def generate_label_pdf(output, brand_name, recipe_format, lot, best_before):
    """Generate a product label PDF."""
    label_w = 2 * inch
    label_h = 1 * inch
    c = canvas.Canvas(output, pagesize=(label_w, label_h))

    y = label_h - 14

    # Brand name bold
    c.setFont(FONT_BOLD, 7)
    c.setFillColor(black)
    brand_lines = _wrap_text(brand_name, 7, label_w - 12)
    for line in brand_lines:
        c.drawCentredString(label_w / 2, y, line)
        y = y - 9

    # Recipe name + format
    c.setFont(FONT, 6)
    rf_lines = _wrap_text(recipe_format, 6, label_w - 12)
    for line in rf_lines:
        c.drawCentredString(label_w / 2, y, line)
        y = y - 8

    # LOT#
    y = y - 2
    c.setFont(FONT_BOLD, 6)
    c.drawCentredString(label_w / 2, y, "LOT#: " + lot)

    # Best before
    y = y - 9
    c.setFont(FONT, 6)
    c.drawCentredString(label_w / 2, y, "Best Before: " + best_before)

    # Border
    c.setStrokeColor(LIGHT_GRAY)
    c.rect(2, 2, label_w - 4, label_h - 4, fill=0, stroke=1)

    c.save()


# -- Checklist sections (default) --
def _sections_from_master(master):
    """Normalise the CCP master document into the checklist's draw shape.

    ccp_master.json is the SINGLE source of truth for what's on the daily
    checklist — the production tablet renders its section ticks from the same
    document. This module used to carry its own hardcoded copy, which drifted
    (5 master sections vs 8 printed), so the signed PDF did not match what the
    kitchen actually confirmed. Never reintroduce a local copy.

    In:  [{"num": "1", "title": "...", "items": ["text", ...]}]
    Out: [("1", "TITLE", [("1.1", "text"), ...])]
    """
    out = []
    for sec in (master or []):
        if not isinstance(sec, dict):
            continue
        num = str(sec.get("num") or "").strip()
        title = (sec.get("title") or "").strip()
        items = []
        for idx, text in enumerate(sec.get("items") or []):
            text = str(text).strip()
            if text:
                items.append((num + "." + str(idx + 1), text))
        if num or title or items:
            out.append((num, title, items))
    return out


def _draw_checklist_content(c, w, h, date, active_vessels, logo_path=None, filled_data=None, sections=None):
    """Draw the checklist body content on the canvas."""
    day_name = date.strftime("%A").upper()
    # Jars filled/counted today come from the batch STARTED yesterday, so a
    # checklist day touches two lots. Name both rather than print one bare LOT#.
    lot_filled = _lot_for_batch_date(date - timedelta(days=1))
    lot_started = _lot_for_batch_date(date)
    checks = filled_data.get("checks", {}) if filled_data else {}
    is_filled = filled_data is not None

    c.showPage()
    title_prefix = "COMPLETED CCP CHECKLIST" if is_filled else "CCP CHECKLIST"
    draw_header(c, w, h, title_prefix + " - " + day_name, date.strftime("%d/%m/%Y"), logo_path)
    y = h - 72

    # Two lines: the two-lot header outgrew the one it shared with the kettle
    # list, which was being drawn on top of it.
    c.setFillColor(LIGHT_BG)
    c.rect(30, y - 32, w - 60, 32, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont(FONT_BOLD, 8)
    c.drawString(40, y - 13, "DATE: " + date.strftime("%d/%m/%Y")
                 + "    LOT# ON JARS FILLED TODAY: " + lot_filled
                 + "    LOT# FOR BATCHES STARTED TODAY: " + lot_started)
    info = "    ".join([v["vessel"] + ": " + v["recipe"] for v in active_vessels])
    c.setFont(FONT, 7)
    c.drawString(40, y - 25, "STARTED TODAY:  " + info if info else "No kettles scheduled today")
    y = y - 40

    c.setFillColor(WARNING_BG)
    c.rect(30, y - 26, w - 60, 26, fill=1, stroke=0)
    c.setStrokeColor(HexColor("#ffc107"))
    c.rect(30, y - 26, w - 60, 26, fill=0, stroke=1)
    c.setFillColor(black)
    c.setFont(FONT_BOLD, 7)
    c.drawString(40, y - 10, "Pressure canning MANDATORY | Never add thickeners before canning | Never skip procedures")
    y = y - 34

    left_margin = 30
    table_w = w - 60
    check_w = 28
    task_w = table_w - check_w
    check_x = left_margin + task_w
    row_h = 14
    sec_h = 20

    for sec_num, sec_title, items in _sections_from_master(sections):
        needed = sec_h + len(items) * row_h + 6
        if y - needed < 70:
            c.showPage()
            draw_header(c, w, h, title_prefix + " - " + day_name + " (cont.)", date.strftime("%d/%m/%Y"), logo_path)
            y = h - 72

        c.setFillColor(ACCENT)
        c.rect(left_margin, y - sec_h, table_w, sec_h, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, 8)
        c.drawString(left_margin + 6, y - 14, sec_num + "  " + sec_title)

        # Section-level checkbox
        cx = check_x + check_w / 2 - 5
        check_key = "section-" + sec_num
        is_checked = checks.get(check_key, False)
        if is_checked and is_filled:
            c.setFillColor(CHECK_GREEN)
            c.rect(cx, y - sec_h + 4, 12, 12, fill=1, stroke=1)
            c.setFillColor(white)
            c.setFont(FONT_BOLD, 8)
            c.drawCentredString(cx + 6, y - sec_h + 6, "Y")
        else:
            c.setFillColor(white)
            c.rect(cx, y - sec_h + 4, 12, 12, fill=1, stroke=1)

        y = y - sec_h

        for idx, (num, text) in enumerate(items):
            bg = ROW_ALT if idx % 2 == 0 else white
            c.setFillColor(bg)
            c.rect(left_margin, y - row_h, table_w + check_w, row_h, fill=1, stroke=0)
            c.setStrokeColor(LIGHT_GRAY)
            c.line(left_margin, y - row_h, left_margin + table_w + check_w, y - row_h)
            c.setFillColor(black)
            c.setFont(FONT, 6.5)
            c.drawString(left_margin + 8, y - 10, num + "  " + text)
            y = y - row_h

        y = y - 4

    # Sign-off
    if y < 90:
        c.showPage()
        draw_header(c, w, h, title_prefix + " - " + day_name + " (cont.)", date.strftime("%d/%m/%Y"), logo_path)
        y = h - 72

    if is_filled and filled_data.get("notes"):
        c.setFillColor(DARK)
        c.rect(30, y - 16, w - 60, 16, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, 8)
        c.drawString(40, y - 12, "DAILY NOTES")
        y = y - 16
        note_lines = filled_data["notes"].split("\n")[:4]
        nh = max(len(note_lines) * 11 + 8, 25)
        c.setFillColor(white)
        c.rect(30, y - nh, w - 60, nh, fill=1, stroke=0)
        c.setStrokeColor(LIGHT_GRAY)
        c.rect(30, y - nh, w - 60, nh, fill=0, stroke=1)
        c.setFillColor(black)
        c.setFont(FONT, 7)
        ny = y - 10
        for nl in note_lines:
            c.drawString(38, ny, nl[:100])
            ny = ny - 11
        y = y - nh - 6

    c.setFillColor(DARK)
    c.rect(30, y - 18, w - 60, 18, fill=1, stroke=0)
    c.setFillColor(HEADER_TEXT)
    c.setFont(FONT_BOLD, 9)
    c.drawString(40, y - 13, "KITCHEN LEAD SIGN-OFF")
    y = y - 18
    c.setFillColor(LIGHT_BG)
    c.rect(30, y - 28, w - 60, 28, fill=1, stroke=0)
    c.setStrokeColor(LIGHT_GRAY)
    c.rect(30, y - 28, w - 60, 28, fill=0, stroke=1)
    c.setFillColor(black)
    c.setFont(FONT, 9)
    kitchen = filled_data.get("signoff_kitchen", "") if is_filled else ""
    if kitchen:
        c.drawString(40, y - 17, "Kitchen Lead: " + kitchen)
    else:
        c.drawString(40, y - 17, "Kitchen Lead: ________________________________")
    if is_filled and filled_data.get("last_updated"):
        c.drawString(310, y - 17, "Completed: " + filled_data["last_updated"][:16])

    c.setFillColor(MEDIUM_GRAY)
    c.setFont(FONT, 6)
    c.drawString(40, 25, "Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    c.drawRightString(w - 40, 25, "Soma Bone Broth - Retain for audit records")


def draw_checklist_pages(c, w, h, date, active_vessels, logo_path=None, sections=None):
    """Render the checklist across one or more pages."""
    _draw_checklist_content(c, w, h, date, active_vessels, logo_path, filled_data=None, sections=sections)


def generate_filled_checklist_pdf(output_path, date, active_vessels, filled_data, logo_path=None, sections=None):
    """Generate a completed daily checklist PDF."""
    w, h = letter
    c = canvas.Canvas(output_path, pagesize=letter)
    _draw_checklist_content(c, w, h, date, active_vessels, logo_path, filled_data, sections=sections)
    c.save()


def generate_weekly_schedule_pdf(output_path, week_start, days_map, recipes, notes="", logo_path=None):
    """Generate the weekly schedule PDF."""
    w, h = letter
    c = canvas.Canvas(output_path, pagesize=letter)
    draw_header(c, w, h, "WEEKLY PRODUCTION SCHEDULE", "Week of " + week_start.strftime("%B %d, %Y"), logo_path)
    y = h - 85

    c.setFillColor(LIGHT_BG)
    c.rect(30, y - 28, w - 60, 28, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont(FONT_BOLD, 9)
    c.drawString(40, y - 18, "WEEK START: " + week_start.strftime("%d/%m/%Y"))
    c.drawString(230, y - 18, "LOT# = START DATE + 365 (DDMMYY)")
    c.drawRightString(w - 40, y - 18, "Prepared by: ____________________")
    y = y - 45

    day_names = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    cols = ["Vessel", "Recipe", "Target Yield", "Production", "LOT#"]
    col_widths = [65, 185, 80, 80, 120]
    col_x = [30]
    for cw in col_widths[:-1]:
        col_x.append(col_x[-1] + cw)
    table_w = sum(col_widths)
    row_h = 18
    hdr_h = 20

    for d_idx, day in enumerate(day_names):
        date = week_start + timedelta(days=d_idx)
        lot = _lot_for_batch_date(date)
        block_h = hdr_h + row_h * len(VESSELS) + 8
        if y - block_h < 55:
            c.showPage()
            draw_header(c, w, h, "WEEKLY PRODUCTION SCHEDULE (cont.)", "Week of " + week_start.strftime("%B %d, %Y"), logo_path)
            y = h - 85

        c.setFillColor(ACCENT)
        c.rect(30, y - hdr_h, table_w, hdr_h, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, 10)
        c.drawString(40, y - 14, day + "  -  " + date.strftime("%d/%m/%Y"))
        y = y - hdr_h

        c.setFillColor(DARK)
        c.rect(30, y - row_h, table_w, row_h, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, 8)
        for i, cn in enumerate(cols):
            c.drawString(col_x[i] + 4, y - 13, cn)
        y = y - row_h

        day_data = days_map.get(d_idx, [])
        for v_idx, vessel in enumerate(VESSELS):
            bg = ROW_ALT if v_idx % 2 == 0 else white
            c.setFillColor(bg)
            c.rect(30, y - row_h, table_w, row_h, fill=1, stroke=0)
            c.setStrokeColor(LIGHT_GRAY)
            c.line(30, y - row_h, 30 + table_w, y - row_h)
            for cx in col_x:
                c.line(cx, y, cx, y - row_h)
            c.line(30 + table_w, y, 30 + table_w, y - row_h)
            c.setFillColor(black)
            c.setFont(FONT_BOLD, 9)
            c.drawString(col_x[0] + 4, y - 13, vessel)
            vd = next((d for d in day_data if d.get("vessel") == vessel), None)
            if vd and vd.get("recipe"):
                rn = vd["recipe"]
                rd = recipes.get(rn, {})
                target = rd.get("yield", "")
                c.setFont(FONT, 9)
                c.drawString(col_x[1] + 4, y - 13, rn)
                if target:
                    c.drawString(col_x[2] + 4, y - 13, str(target) + " units")
                c.setFillColor(MEDIUM_GRAY)
                c.setFont(FONT, 8)
                c.drawString(col_x[4] + 4, y - 13, lot)
            y = y - row_h
        y = y - 10

    if y < 120:
        c.showPage()
        draw_header(c, w, h, "WEEKLY PRODUCTION SCHEDULE (cont.)", "Week of " + week_start.strftime("%B %d, %Y"), logo_path)
        y = h - 85

    notes_h = 80
    c.setFillColor(DARK)
    c.rect(30, y - 18, w - 60, 18, fill=1, stroke=0)
    c.setFillColor(HEADER_TEXT)
    c.setFont(FONT_BOLD, 9)
    c.drawString(40, y - 13, "NOTES")
    y = y - 18
    c.setFillColor(white)
    c.rect(30, y - notes_h, w - 60, notes_h, fill=1, stroke=0)
    c.setStrokeColor(LIGHT_GRAY)
    c.rect(30, y - notes_h, w - 60, notes_h, fill=0, stroke=1)
    if notes:
        c.setFillColor(black)
        c.setFont(FONT, 8)
        ny = y - 12
        for nl in notes.split("\n")[:8]:
            c.drawString(38, ny, nl[:100])
            ny = ny - 10
    c.setFillColor(MEDIUM_GRAY)
    c.setFont(FONT, 7)
    c.drawString(40, 28, "Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    c.drawRightString(w - 40, 28, "Soma Bone Broth - Confidential")
    c.save()


def generate_single_recipe_pdf(output, recipe_name, recipe_data, logo_path=None):
    """One-recipe PDF for the recipe-cards page Download button.

    `output` may be a path or a writable buffer (e.g. BytesIO). No vessel
    context — the header just shows the recipe name plus brand/format/yield
    as a subtitle.
    """
    w, h = letter
    c = canvas.Canvas(output, pagesize=letter)
    brand = recipe_data.get("brand") or ""
    fmt = recipe_data.get("format", "")
    target = recipe_data.get("yield", "")
    subtitle_parts = []
    if brand:
        subtitle_parts.append(str(brand))
    if fmt:
        subtitle_parts.append(str(fmt))
    if target:
        subtitle_parts.append("Target: " + str(target) + " units")
    subtitle = "  |  ".join(subtitle_parts)
    draw_header(c, w, h, "RECIPE CARD", subtitle, logo_path)
    margin = 30
    card_w = w - 2 * margin
    y = h - 68
    draw_recipe_card(c, margin, y, card_w, recipe_name, recipe_data)
    c.save()


def generate_all_recipes_pdf(output, ordered_recipes, logo_path=None):
    """All-recipes PDF. `ordered_recipes` is a list of (name, data) tuples,
    already filtered (e.g. archived excluded) and sorted by the caller —
    pdf_engine renders them in given order, paginating as needed.
    """
    w, h = letter
    c = canvas.Canvas(output, pagesize=letter)
    today = datetime.now().strftime("%d/%m/%Y")
    n = len(ordered_recipes)
    subtitle = today + "  |  " + str(n) + " recipe" + ("" if n == 1 else "s")
    draw_header(c, w, h, "RECIPE CARDS - ALL", subtitle, logo_path)
    margin = 30
    card_w = w - 2 * margin
    y = h - 68
    gap = 12
    for name, data in ordered_recipes:
        est_h = estimate_card_height(data, card_w)
        if y - est_h < 60:
            c.showPage()
            draw_header(c, w, h, "RECIPE CARDS - ALL (cont.)", today, logo_path)
            y = h - 68
        card_bottom = draw_recipe_card(c, margin, y, card_w, name, data)
        y = card_bottom - gap
    c.save()


def generate_daily_package_pdf(output_path, date, vessel_assignments, recipes, logo_path=None, sections=None):
    """Generate the daily production package PDF."""
    w, h = letter
    c = canvas.Canvas(output_path, pagesize=letter)
    day_name = date.strftime("%A").upper()
    lot = _lot_for_batch_date(date)
    active = [v for v in vessel_assignments if v.get("recipe") and v["recipe"] in recipes]
    draw_header(c, w, h, "RECIPE CARDS - " + day_name, date.strftime("%d/%m/%Y") + "  |  LOT#: " + lot, logo_path)
    if not active:
        c.setFillColor(MEDIUM_GRAY)
        c.setFont(FONT, 12)
        c.drawCentredString(w / 2, h / 2, "No active kettles scheduled")
    else:
        margin = 30
        card_w = w - 2 * margin
        y = h - 68
        gap = 12
        for v in active:
            rd = recipes[v["recipe"]]
            est_h = estimate_card_height(rd, card_w)
            if y - est_h < 60:
                c.showPage()
                draw_header(c, w, h, "RECIPE CARDS - " + day_name + " (cont.)", date.strftime("%d/%m/%Y") + "  |  LOT#: " + lot, logo_path)
                y = h - 68
            card_bottom = draw_recipe_card(c, margin, y, card_w, v["recipe"], rd, v["vessel"])
            y = card_bottom - gap
    draw_checklist_pages(c, w, h, date, active, logo_path, sections=sections)
    c.save()


# -- Audit pack (2026-09-22) --
# One document for an inspector: recipes that ran, the CCP plan, one completed
# checklist as the worked example, then every batch in the period traced from
# supplier lot to buyer. Data comes pre-assembled from audit_tools._build_audit_pack;
# this function only lays it out.
RED = HexColor("#c62828")


def _dmy(iso):
    """YYYY-MM-DD[...] -> DD/MM/YYYY; anything else passes through."""
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return str(iso or "")


def _clip(c, text, font, size, max_w):
    """Trim text with an ellipsis so it fits max_w points."""
    text = str(text or "")
    if c.stringWidth(text, font, size) <= max_w:
        return text
    while text and c.stringWidth(text + "...", font, size) > max_w:
        text = text[:-1]
    return text + "..."


def _qty(v):
    """Quantities print without a trailing .0."""
    try:
        f = float(v)
    except (ValueError, TypeError):
        return str(v or "")
    return str(int(f)) if f == int(f) else str(round(f, 3))


def generate_audit_pack_pdf(output, pack, example=None, logo_path=None):
    """The audit pack. `pack` is audit_tools._build_audit_pack's dict; `example`
    is {date, active_vessels, filled} for the worked-example checklist, or None.
    `output` may be a path or a writable buffer."""
    w, h = letter
    c = canvas.Canvas(output, pagesize=letter)
    period = _dmy(pack["from"]) + " to " + _dmy(pack["to"])
    scope = "Organic-certified production only" if pack["organic_only"] else "All production"
    batches = pack["batches"]
    days = pack["days"]
    left, right = 30, w - 30
    width = right - left

    def footer():
        c.setFillColor(MEDIUM_GRAY)
        c.setFont(FONT, 6)
        c.drawString(40, 25, "Audit pack  |  " + period + "  |  Generated " + pack["generated_at"])
        c.drawRightString(w - 40, 25, "Page " + str(c.getPageNumber()))

    def new_page(title, with_footer=True):
        if with_footer:
            footer()
        c.showPage()
        draw_header(c, w, h, title, period, logo_path)
        return h - 72

    def bar(y, text, color=ACCENT, size=8, height=18, right_text=""):
        c.setFillColor(color)
        c.rect(left, y - height, width, height, fill=1, stroke=0)
        c.setFillColor(HEADER_TEXT)
        c.setFont(FONT_BOLD, size)
        c.drawString(left + 6, y - height + 6, _clip(c, text, FONT_BOLD, size, width - 150))
        if right_text:
            c.setFont(FONT, size - 1)
            c.drawRightString(right - 6, y - height + 6, right_text)
        return y - height

    def para(y, text, size=8, font=FONT, color=black, indent=0):
        c.setFillColor(color)
        c.setFont(font, size)
        for line in _wrap_text(text, size, width - indent - 10):
            c.drawString(left + indent + 4, y - size - 2, line)
            y -= size + 4
        return y

    # ── Cover ────────────────────────────────────────────────────────────────
    draw_header(c, w, h, "PRODUCTION AUDIT PACK", period, logo_path)
    y = h - 110
    c.setFillColor(DARK)
    c.setFont(FONT_BOLD, 20)
    c.drawString(left + 4, y, "Production audit pack")
    y -= 22
    c.setFont(FONT, 11)
    c.setFillColor(black)
    c.drawString(left + 4, y, period + "   |   " + scope)
    y -= 14
    c.setFont(FONT, 8)
    c.setFillColor(MEDIUM_GRAY)
    c.drawString(left + 4, y, (pack.get("company") or "Soma Bone Broth")
                 + "   |   generated " + pack["generated_at"] + " from the live production records")
    y -= 26

    full_ccp = sum(1 for d in days if d["ccp_total"] and d["ccp_confirmed"] == d["ccp_total"])
    reviewed = sum(1 for d in days if d["reviewed_by"])
    short = sum(1 for b in batches if any(i["short"] for i in b["ingredients"]))
    sold = sum(1 for b in batches if b["sold"])
    stats = [
        ("Batches", str(len(batches))),
        ("Jars produced", str(sum(b["jars"] for b in batches))),
        ("Recipes", str(len(pack["recipes"]) + len(pack["recipes_missing"]))),
        ("Checklist days", str(len(days))),
    ]
    box_w = width / len(stats)
    for i, (label, value) in enumerate(stats):
        x = left + i * box_w
        c.setFillColor(LIGHT_BG)
        c.rect(x + 2, y - 52, box_w - 4, 52, fill=1, stroke=0)
        c.setFillColor(DARK)
        c.setFont(FONT_BOLD, 20)
        c.drawCentredString(x + box_w / 2, y - 28, value)
        c.setFillColor(MEDIUM_GRAY)
        c.setFont(FONT, 8)
        c.drawCentredString(x + box_w / 2, y - 44, label)
    y -= 70

    y = bar(y, "RECORD CHECKS")
    checks = [
        ("Checklist days with every CCP section confirmed", str(full_ccp) + " of " + str(len(days)),
         full_ccp == len(days)),
        ("Checklist days reviewed by management", str(reviewed) + " of " + str(len(days)),
         reviewed == len(days)),
        ("Batches made with a raw-material shortfall", str(short), short == 0),
        ("Batches with at least one sale recorded", str(sold) + " of " + str(len(batches)), True),
    ]
    for i, (label, value, ok) in enumerate(checks):
        c.setFillColor(ROW_ALT if i % 2 == 0 else white)
        c.rect(left, y - 16, width, 16, fill=1, stroke=0)
        c.setFillColor(black)
        c.setFont(FONT, 8)
        c.drawString(left + 8, y - 11, label)
        c.setFillColor(black if ok else RED)
        c.setFont(FONT_BOLD, 8)
        c.drawRightString(right - 8, y - 11, value)
        y -= 16
    y -= 18

    y = bar(y, "CONTENTS")
    ex = ("completed checklist for " + _dmy(pack["example_date"])) if pack["example_date"] \
        else "none (no filed checklist in this period)"
    contents = [
        "1.  Recipes produced in this period (current recipe cards)",
        "2.  CCP plan (the controlled checklist every production day is signed against)",
        "3.  Worked example: " + ex,
        "4.  Traced production: " + str(len(batches)) + " batches, supplier lot to buyer",
    ]
    for line in contents:
        c.setFillColor(black)
        c.setFont(FONT, 9)
        c.drawString(left + 8, y - 13, line)
        y -= 16
    y -= 14
    y = bar(y, "HOW TO READ THE LOT NUMBERS", color=DARK)
    y = para(y - 2, "The LOT# is the batch's production (start) date plus 365 days, written ddmmyy. "
                    "It is also the Best Before date, and it is the number stamped on every jar and "
                    "printed on every case label. A batch is started on one day and its jars are "
                    "sealed-checked and counted the next, so each batch is covered by two daily "
                    "checklists; section 4 names both. Raw-material quantities are the frozen record "
                    "taken when the batch was completed, not recomputed from today's recipe.")

    # ── 1. Recipes ───────────────────────────────────────────────────────────
    y = new_page("1. RECIPES PRODUCED IN THIS PERIOD")
    y = para(y, "The recipe cards as currently held in the system for every recipe run in this period. "
                "What each batch actually consumed is recorded per batch in section 4.",
             color=MEDIUM_GRAY) - 8
    if not pack["recipes"] and not pack["recipes_missing"]:
        y = para(y, "No batches were produced in this period.")
    card_w = width
    for name, data in pack["recipes"]:
        est = estimate_card_height(data, card_w)
        if y - est < 60:
            y = new_page("1. RECIPES (cont.)")
        cert = (data.get("certification") or "").strip()
        if cert:
            c.setFillColor(MEDIUM_GRAY)
            c.setFont(FONT, 7)
            c.drawString(left, y - 8, "Certification: " + cert)
            y -= 12
        y = draw_recipe_card(c, left, y, card_w, name, data) - 12
    for name in pack["recipes_missing"]:
        y = para(y, name + ": this recipe no longer exists in the system (renamed or deleted). "
                        "Its batches' ingredients are still recorded in section 4.", color=RED)

    # ── 2. CCP plan ──────────────────────────────────────────────────────────
    y = new_page("2. CCP PLAN")
    y = para(y, "The daily production checklist. Each section is confirmed on the production tablet "
                "and the day is signed off by the kitchen lead; management reviews every day the "
                "kitchen ran.", color=MEDIUM_GRAY) - 8
    for sec_num, sec_title, items in _sections_from_master(pack["ccp"]):
        if y - (20 + 14 * len(items)) < 60:
            y = new_page("2. CCP PLAN (cont.)")
        y = bar(y, sec_num + "  " + sec_title, height=20)
        for i, (num, text) in enumerate(items):
            lines = _wrap_text(num + "  " + text, 7.5, width - 20)
            rh = 6 + 10 * len(lines)
            c.setFillColor(ROW_ALT if i % 2 == 0 else white)
            c.rect(left, y - rh, width, rh, fill=1, stroke=0)
            c.setFillColor(black)
            c.setFont(FONT, 7.5)
            ty = y - 11
            for line in lines:
                c.drawString(left + 8, ty, line)
                ty -= 10
            y -= rh
        y -= 8

    # ── 3. Worked example ────────────────────────────────────────────────────
    footer()
    if example:
        _draw_checklist_content(c, w, h, example["date"], example["active_vessels"], logo_path,
                                filled_data=example["filled"], sections=pack["ccp"])
    else:
        c.showPage()
        draw_header(c, w, h, "3. WORKED EXAMPLE", period, logo_path)
        para(h - 72, "No checklist was filed in this period.")

    # ── 4. Traced production ─────────────────────────────────────────────────
    # The example checklist draws its own footer; don't stamp a second over it.
    y = new_page("4. TRACED PRODUCTION", with_footer=not example)
    y = para(y, "Every completed batch started in this period, in date order: the raw lots it "
                "consumed (supplier and supplier LOT#), the two checklists it was made under, and "
                "every sale that drew on its LOT#.", color=MEDIUM_GRAY) - 8
    if not batches:
        y = para(y, "No batches were produced in this period.")

    cols_in = [("Ingredient", 0.30), ("Supplier", 0.24), ("Supplier LOT#", 0.18),
               ("Received", 0.13), ("Used", 0.15)]
    cols_out = [("Date", 0.16), ("Buyer", 0.46), ("Jars", 0.12), ("Order", 0.26)]

    def table_head(y, cols):
        c.setFillColor(LIGHT_GRAY)
        c.rect(left + 10, y - 12, width - 10, 12, fill=1, stroke=0)
        c.setFillColor(black)
        c.setFont(FONT_BOLD, 6.5)
        x = left + 14
        for label, frac in cols:
            c.drawString(x, y - 9, label.upper())
            x += (width - 10) * frac
        return y - 12

    def table_row(y, cols, values, i, color=black):
        c.setFillColor(ROW_ALT if i % 2 == 0 else white)
        c.rect(left + 10, y - 11, width - 10, 11, fill=1, stroke=0)
        c.setFillColor(color)
        c.setFont(FONT, 7)
        x = left + 14
        for (label, frac), v in zip(cols, values):
            cw = (width - 10) * frac
            c.drawString(x, y - 8, _clip(c, v, FONT, 7, cw - 6))
            x += cw
        return y - 11

    def checklist_line(label, rec):
        if not rec:
            return label + ": no counting day recorded", True
        if not rec["filed"]:
            return label + " " + _dmy(rec["date"]) + ": checklist NOT filed", True
        text = (label + " " + _dmy(rec["date"]) + ": signed by " + (rec["signed_by"] or "(no name)")
                + "  |  CCP " + str(rec["ccp_confirmed"]) + "/" + str(rec["ccp_total"]) + " confirmed"
                + "  |  " + ("reviewed by " + rec["reviewed_by"] + " " + _dmy(rec["reviewed_at"])
                            if rec["reviewed_by"] else "not yet reviewed"))
        flag = rec["ccp_confirmed"] < rec["ccp_total"] or not rec["reviewed_by"]
        return text, flag

    for b in batches:
        need = 18 + 30 + 12 + 11 * max(1, len(b["ingredients"])) + 16 + 12 + 11 * max(1, len(b["sold"]))
        if y - min(need, 300) < 60:
            y = new_page("4. TRACED PRODUCTION (cont.)")
        title = "LOT# " + b["lot"] + "   " + b["vessel"] + "   " + " ".join(
            p for p in (b["brand"], b["recipe"], b["format"]) if p)
        y = bar(y, title, height=18,
                right_text=(b["certification"] or "No certification") + "  |  started " + _dmy(b["start_date"]))
        if b["fg_on_record"]:
            stock = str(b["jars_remaining"]) + " still in stock"
        elif b.get("in_reset"):
            stock = "stock since counted into the zero-day reset baseline"
        else:
            stock = "no finished-goods record on file"
        y = para(y, "Jars produced: " + str(b["jars"]) + "   |   " + stock, font=FONT_BOLD, indent=6)
        for label, rec in (("Started", b["started"]), ("Counted", b["counted"])):
            text, flag = checklist_line(label, rec)
            y = para(y, text, size=7, color=RED if flag else black, indent=6)
        y -= 4

        if y - 40 < 60:
            y = new_page("4. TRACED PRODUCTION (cont.)")
        y = table_head(y, cols_in)
        if not b["ingredients"]:
            y = table_row(y, cols_in, ["No raw materials recorded for this batch", "", "", "", ""], 0, RED)
        for i, ing in enumerate(b["ingredients"]):
            if y - 11 < 60:
                y = new_page("4. TRACED PRODUCTION (cont.)")
                y = table_head(y, cols_in)
            used = _qty(ing["quantity"]) + " " + (ing["unit"] or "")
            y = table_row(y, cols_in, [
                ing["item"], ing["supplier"],
                ("SHORT - no stock" if ing["short"] else ing["supplier_lot"]),
                _dmy(ing["date_received"]), used], i, RED if ing["short"] else black)
        y -= 6

        if y - 30 < 60:
            y = new_page("4. TRACED PRODUCTION (cont.)")
        y = table_head(y, cols_out)
        if not b["sold"]:
            y = table_row(y, cols_out, ["", "Not sold yet", "", ""], 0, MEDIUM_GRAY)
        for i, s in enumerate(b["sold"]):
            if y - 11 < 60:
                y = new_page("4. TRACED PRODUCTION (cont.)")
                y = table_head(y, cols_out)
            y = table_row(y, cols_out, [_dmy(s["date"]), s["buyer"], str(s["quantity"]), s["order"]], i)
        y -= 16

    footer()
    c.save()

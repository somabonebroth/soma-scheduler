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
    lot = date.strftime("%d%m%y")
    checks = filled_data.get("checks", {}) if filled_data else {}
    is_filled = filled_data is not None

    c.showPage()
    title_prefix = "COMPLETED CCP CHECKLIST" if is_filled else "CCP CHECKLIST"
    draw_header(c, w, h, title_prefix + " - " + day_name, date.strftime("%d/%m/%Y"), logo_path)
    y = h - 72

    c.setFillColor(LIGHT_BG)
    c.rect(30, y - 22, w - 60, 22, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont(FONT_BOLD, 8)
    c.drawString(40, y - 15, "DATE: " + date.strftime("%d/%m/%Y") + "    LOT#: " + lot)
    info = "    ".join([v["vessel"] + ": " + v["recipe"] for v in active_vessels])
    c.setFont(FONT, 7)
    c.drawString(220, y - 15, info)
    y = y - 30

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
    c.drawString(230, y - 18, "LOT# FORMAT: DDMMYY (auto)")
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
        lot = date.strftime("%d%m%y")
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
    lot = date.strftime("%d%m%y")
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

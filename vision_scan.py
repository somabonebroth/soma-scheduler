"""Camera-scan extraction for invoices and packing slips.

Calls Claude Vision API to parse uploaded images into structured line items
that can be reviewed and saved through the existing bulk endpoints.

Two flows:
  - extract_invoice_lines(): receive-inventory scan. Returns supplier-level
    metadata + per-line ingredient receipts.
  - extract_packing_slip_lines(): record-sale scan. Returns buyer-level
    metadata + per-line SKU sales.

Both return structured dicts the frontend can render in an editable review
grid. Items are returned with raw extracted text and the server's best-effort
canonical match (with confidence) so the user can confirm or override.
"""
import base64
import json
import os
from datetime import datetime
from difflib import SequenceMatcher

# The anthropic SDK is imported lazily so the rest of the app loads even if
# the SDK isn't installed locally during development. Production must have it.
_anthropic_client = None


def _get_client():
    """Lazy-init the Anthropic client. Raises if API key is missing."""
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Add it in your Render dashboard under Environment."
        )
    try:
        import anthropic  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK is not installed. Add 'anthropic>=0.40.0' to "
            "requirements.txt and redeploy."
        ) from e
    _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


# Default model — Sonnet is plenty smart for invoice OCR and ~5x cheaper than Opus.
SCAN_MODEL = os.environ.get("SCAN_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOKENS = 4096


# ── Image helpers ───────────────────────────────────────────────────────────

ALLOWED_IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}


def detect_media_type(filename, fallback="image/jpeg"):
    """Pick a media type from a filename extension."""
    if not filename:
        return fallback
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ALLOWED_IMAGE_TYPES.get(ext, fallback)


def image_to_base64(file_bytes):
    """Encode raw bytes as base64 string for the API."""
    return base64.standard_b64encode(file_bytes).decode("utf-8")


# ── Prompt templates ────────────────────────────────────────────────────────

INVOICE_PROMPT = """You are extracting raw material receipt data from a supplier invoice photo.

Read the image carefully. Return a JSON object with this exact shape (no other text):

{
  "supplier": "supplier company name as printed",
  "invoice_date": "YYYY-MM-DD if visible, else empty string",
  "invoice_number": "invoice/order number if visible, else empty string",
  "line_items": [
    {
      "raw_name": "the item name exactly as printed on the invoice",
      "quantity": numeric value,
      "unit": "unit of measure (kg, g, lb, lbs, L, ml, etc.) — preserve what's printed",
      "supplier_lot": "lot/batch number if shown for this line, else empty string",
      "best_before": "YYYY-MM-DD if visible, else empty string"
    }
  ],
  "notes": "any free-text observations or extraction warnings"
}

Rules:
- Extract every line that has a quantity. Skip subtotal/tax/total rows.
- For ambiguous units, prefer the printed abbreviation over guessing.
- If the same ingredient appears multiple times (different lots or boxes), keep them separate.
- If quantity is given as "20 cases × 2kg", treat as 40 with unit kg.
- If you cannot read a value clearly, return empty string for that field — never invent values.
- Return ONLY the JSON. No prose before or after."""


PACKING_SLIP_PROMPT = """You are extracting product sales data from a packing slip, purchase order, or shipping document photo.

Read the image carefully. Return a JSON object with this exact shape (no other text):

{
  "buyer": "buyer or recipient company name",
  "ship_date": "YYYY-MM-DD if visible, else empty string",
  "po_number": "PO or order number if visible, else empty string",
  "line_items": [
    {
      "raw_name": "the product description exactly as printed",
      "quantity": numeric value (number of units/jars/bottles),
      "format_hint": "size/format if mentioned, e.g. '750ml', '876ml', '473ml SS', else empty string"
    }
  ],
  "notes": "any free-text observations"
}

Rules:
- Extract every product line that has a quantity. Skip subtotal/tax/total rows.
- Quantity is the unit count being shipped (e.g. 12 jars, 24 bottles).
- format_hint helps the system match to a SKU; copy text like "750ml" or "SS-876" verbatim.
- Brand names like "Soma", "Wild Meadows", "RIPE" should appear in raw_name if printed.
- If you cannot read a value clearly, return empty string — never invent values.
- Return ONLY the JSON. No prose before or after."""


# ── Vision API calls ────────────────────────────────────────────────────────


def _call_vision(prompt_text, image_b64, media_type):
    """Send a single image + prompt to Claude Vision; return parsed JSON dict.

    Raises:
      RuntimeError if the API call fails or the response can't be parsed.
    """
    client = _get_client()
    try:
        response = client.messages.create(
            model=SCAN_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }],
        )
    except Exception as e:
        raise RuntimeError(f"Vision API call failed: {e}") from e

    # Concatenate any text content blocks; ignore tool blocks etc.
    text_parts = []
    for block in (response.content or []):
        # SDK returns objects with .type and .text; be defensive
        block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if block_type == "text":
            t = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            if t:
                text_parts.append(t)
    raw_text = "".join(text_parts).strip()
    if not raw_text:
        raise RuntimeError("Vision API returned no text content.")

    # The model sometimes wraps JSON in ```json fences despite instructions;
    # strip them defensively before parsing.
    cleaned = raw_text
    if cleaned.startswith("```"):
        # Remove first fence line and trailing fence
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Surface the first 300 chars of what came back so the user can debug
        snippet = cleaned[:300].replace("\n", " ")
        raise RuntimeError(
            f"Could not parse JSON from Vision response. Got: {snippet}"
        ) from e

    return parsed


# ── Public extraction functions ─────────────────────────────────────────────


def extract_invoice_lines(image_bytes, filename=""):
    """Run invoice extraction. Returns a dict with supplier, invoice_date,
    invoice_number, line_items, notes — see INVOICE_PROMPT for full shape."""
    media_type = detect_media_type(filename)
    image_b64 = image_to_base64(image_bytes)
    parsed = _call_vision(INVOICE_PROMPT, image_b64, media_type)
    # Defensive normalization — don't trust the model output 100%
    return {
        "supplier": str(parsed.get("supplier") or "").strip(),
        "invoice_date": str(parsed.get("invoice_date") or "").strip(),
        "invoice_number": str(parsed.get("invoice_number") or "").strip(),
        "notes": str(parsed.get("notes") or "").strip(),
        "line_items": [_clean_invoice_line(li) for li in (parsed.get("line_items") or []) if isinstance(li, dict)],
    }


def extract_packing_slip_lines(image_bytes, filename=""):
    """Run packing-slip / PO extraction. Returns a dict with buyer, ship_date,
    po_number, line_items, notes."""
    media_type = detect_media_type(filename)
    image_b64 = image_to_base64(image_bytes)
    parsed = _call_vision(PACKING_SLIP_PROMPT, image_b64, media_type)
    return {
        "buyer": str(parsed.get("buyer") or "").strip(),
        "ship_date": str(parsed.get("ship_date") or "").strip(),
        "po_number": str(parsed.get("po_number") or "").strip(),
        "notes": str(parsed.get("notes") or "").strip(),
        "line_items": [_clean_sale_line(li) for li in (parsed.get("line_items") or []) if isinstance(li, dict)],
    }


def _clean_invoice_line(item):
    try:
        qty = float(item.get("quantity") or 0)
    except (ValueError, TypeError):
        qty = 0.0
    return {
        "raw_name": str(item.get("raw_name") or "").strip(),
        "quantity": qty,
        "unit": str(item.get("unit") or "").strip(),
        "supplier_lot": str(item.get("supplier_lot") or "").strip(),
        "best_before": str(item.get("best_before") or "").strip(),
    }


def _clean_sale_line(item):
    try:
        qty = int(float(item.get("quantity") or 0))
    except (ValueError, TypeError):
        qty = 0
    return {
        "raw_name": str(item.get("raw_name") or "").strip(),
        "quantity": qty,
        "format_hint": str(item.get("format_hint") or "").strip(),
    }


# ── Fuzzy matching helpers ─────────────────────────────────────────────────


def _normalize(s):
    """Lowercase + collapse whitespace for matching."""
    return " ".join((s or "").lower().split())


# Common abbreviations the printed text might use vs canonical names.
UNIT_ALIASES = {
    "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
    "lb": "lbs", "pound": "lbs", "pounds": "lbs",
    "grams": "g", "gram": "g", "gm": "g",
    "milliliters": "ml", "milliliter": "ml", "millilitre": "ml", "millilitres": "ml",
    "liter": "L", "liters": "L", "litre": "L", "litres": "L", "l": "L",
    "ea": "Pack", "each": "Pack", "pcs": "Pack", "pieces": "Pack",
    "bunches": "Bunch",
}


def normalize_unit(unit):
    """Map a printed unit to the canonical 9-unit set if possible.
    Returns the canonical form, or the original string if no match."""
    if not unit:
        return ""
    u = unit.strip()
    # Try direct match first (case-sensitive for L, case-insensitive otherwise)
    if u in {"kg", "g", "L", "ml", "lbs", "Bunch", "Pack", "Adjunct", "per L"}:
        return u
    lc = u.lower()
    if lc in UNIT_ALIASES:
        return UNIT_ALIASES[lc]
    # Try lowercasing common variants
    if lc == "l":
        return "L"
    if lc == "ml":
        return "ml"
    if lc == "kg":
        return "kg"
    if lc == "g":
        return "g"
    return u


def match_ingredient(raw_name, raw_unit, catalog):
    """Find the best canonical (name, unit) match for a parsed line.

    Args:
      raw_name: ingredient name as printed on the invoice
      raw_unit: unit as printed
      catalog: list of {name, unit} dicts (the canonical ingredient list)

    Returns:
      {match: {name, unit} | None, confidence: 'high'|'medium'|'low'|'none', score: float}
    """
    if not raw_name or not catalog:
        return {"match": None, "confidence": "none", "score": 0.0}

    norm_unit = normalize_unit(raw_unit)
    target = _normalize(raw_name)

    # First pass: exact normalized match (with matching unit if possible)
    for item in catalog:
        if _normalize(item["name"]) == target:
            unit_ok = (not norm_unit) or (item["unit"] == norm_unit)
            return {
                "match": {"name": item["name"], "unit": item["unit"]},
                "confidence": "high" if unit_ok else "medium",
                "score": 1.0,
            }

    # Second pass: substring containment in either direction (e.g. printed
    # "Organic Chicken Bones" matches catalog "chicken bones, organic" loosely)
    best = None
    best_score = 0.0
    for item in catalog:
        name_norm = _normalize(item["name"])
        # SequenceMatcher gives a similarity ratio 0..1
        score = SequenceMatcher(None, target, name_norm).ratio()
        # Boost score if one contains the other
        if target in name_norm or name_norm in target:
            score = max(score, 0.85)
        # Tiny boost when units agree
        if norm_unit and item["unit"] == norm_unit:
            score += 0.05
        if score > best_score:
            best_score = score
            best = item

    if best is None:
        return {"match": None, "confidence": "none", "score": 0.0}

    if best_score >= 0.85:
        confidence = "high"
    elif best_score >= 0.65:
        confidence = "medium"
    elif best_score >= 0.45:
        confidence = "low"
    else:
        return {"match": None, "confidence": "none", "score": best_score}

    return {
        "match": {"name": best["name"], "unit": best["unit"]},
        "confidence": confidence,
        "score": round(best_score, 3),
    }


def match_sku(raw_name, format_hint, catalog):
    """Find the best canonical SKU match for a parsed sale line.

    Args:
      raw_name: product line as printed on the packing slip
      format_hint: format/size text if extracted (e.g. "750ml", "SS-876ML")
      catalog: list of {brand, recipe, format, sku_key, display} dicts

    Returns:
      {match: sku_dict | None, confidence: 'high'|'medium'|'low'|'none', score: float}
    """
    if not raw_name or not catalog:
        return {"match": None, "confidence": "none", "score": 0.0}

    target = _normalize(raw_name + " " + (format_hint or ""))

    # Build searchable string per SKU
    best = None
    best_score = 0.0
    for sku in catalog:
        searchable = _normalize(
            (sku.get("brand", "") + " "
             + sku.get("recipe", "") + " "
             + sku.get("format", ""))
        )
        score = SequenceMatcher(None, target, searchable).ratio()
        # Boost: brand or recipe name appears in target
        recipe_norm = _normalize(sku.get("recipe", ""))
        brand_norm = _normalize(sku.get("brand", ""))
        if recipe_norm and recipe_norm in target:
            score = max(score, 0.8)
        if brand_norm and brand_norm in target:
            score += 0.05
        # Format match boost
        fmt_norm = _normalize(sku.get("format", "")).replace("-", "").replace(" ", "")
        target_compact = target.replace("-", "").replace(" ", "")
        if fmt_norm and fmt_norm in target_compact:
            score += 0.1
        if score > best_score:
            best_score = score
            best = sku

    if best is None:
        return {"match": None, "confidence": "none", "score": 0.0}

    if best_score >= 0.85:
        confidence = "high"
    elif best_score >= 0.65:
        confidence = "medium"
    elif best_score >= 0.45:
        confidence = "low"
    else:
        return {"match": None, "confidence": "none", "score": best_score}

    return {
        "match": {
            "sku_key": best.get("sku_key"),
            "brand": best.get("brand"),
            "recipe": best.get("recipe"),
            "format": best.get("format"),
            "display": best.get("display"),
        },
        "confidence": confidence,
        "score": round(best_score, 3),
    }

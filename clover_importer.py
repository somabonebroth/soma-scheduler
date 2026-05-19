"""
Clover weekly sales importer.

Mirrors shopify_importer.py — same preview/commit pattern, same SKU parsing,
same per-channel scaffolding — but talks to Clover's REST API instead of
Shopify's GraphQL/Admin API.

Configuration via environment variables:
    CLOVER_API_TOKEN     — Bearer token for merchant API. Either a
                           merchant-specific API token from the Merchant
                           Dashboard (Setup → API Tokens) or an OAuth
                           access_token. Both are sent the same way:
                               Authorization: Bearer {token}
    CLOVER_MERCHANT_ID   — Merchant identifier string (e.g. 'ABC123XYZ').
                           Required for every API URL.
    CLOVER_API_BASE      — Optional override. Defaults to Clover's North
                           American production base. Set this if your
                           merchant is on a different region's endpoint.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger(__name__)

# Default Clover API base. Override via CLOVER_API_BASE env var if needed
# (e.g. Europe uses api.eu.clover.com).
DEFAULT_CLOVER_API_BASE = "https://api.clover.com/v3"

# Week boundaries interpreted in Toronto local time. Sales week is
# Mon 00:00 → Sun 23:59 local.
TIMEZONE = ZoneInfo("America/Toronto") if ZoneInfo else None

# Format suffix regex — identical to the Shopify importer so SKUs stay
# consistent across both channels.
_FORMAT_RE = re.compile(r"^([A-Z]{2})-(\d+)ML$")


def parse_sku(sku):
    """Parse a Clover SKU like 'SOMA-Adaptogenic Mushroom-SS-876ML' into
    (brand, recipe, format). Same logic as shopify_importer.parse_sku —
    duplicated here on purpose to keep each channel module self-contained.

    Raises ValueError if the SKU doesn't match the expected shape.
    """
    if not sku or not isinstance(sku, str):
        raise ValueError("Empty or non-string SKU")

    parts = sku.split("-")
    if len(parts) < 3:
        raise ValueError(
            f"SKU {sku!r} has fewer than 3 hyphen-separated parts; "
            f"expected BRAND-RECIPE-FORMAT (e.g. 'SOMA-Recipe Name-SS-876ML')"
        )

    fmt = f"{parts[-2]}-{parts[-1]}"
    if not _FORMAT_RE.match(fmt):
        raise ValueError(
            f"SKU {sku!r}: format suffix {fmt!r} does not match expected "
            f"pattern (two letters + digits + 'ML', e.g. 'SS-876ML')"
        )

    brand = parts[0]
    recipe = "-".join(parts[1:-2])
    if not recipe:
        raise ValueError(f"SKU {sku!r}: recipe portion is empty")

    return brand, recipe, fmt


def week_range_ms(week_id):
    """Given a week_id ('YYYY-MM-DD' Monday), return (start_ms, end_ms)
    covering Mon 00:00:00 → Sun 23:59:59 in America/Toronto, as Unix
    epoch milliseconds.

    Clover's `filter=createdTime>=X` parameters use epoch ms, not ISO.
    """
    monday = datetime.strptime(week_id, "%Y-%m-%d")
    if TIMEZONE is not None:
        monday = monday.replace(tzinfo=TIMEZONE)
    sunday_end = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    # int(timestamp() * 1000) — UTC epoch ms regardless of tz attached
    return int(monday.timestamp() * 1000), int(sunday_end.timestamp() * 1000)


def _line_item_sku(li):
    """Extract a SKU string from a Clover line item, trying multiple
    locations because Clover's response shape varies by merchant config.

    Order of preference:
      1. line_item.sku           — when Clover denormalizes onto line item
      2. line_item.item.sku      — when ?expand=lineItems.item was used
      3. line_item.itemCode      — alternative SKU field on some merchants
      4. ""                      — couldn't find a SKU
    """
    if not isinstance(li, dict):
        return ""
    direct = (li.get("sku") or "").strip() if isinstance(li.get("sku"), str) else ""
    if direct:
        return direct
    nested_item = li.get("item") or {}
    if isinstance(nested_item, dict):
        ni_sku = (nested_item.get("sku") or "").strip() if isinstance(nested_item.get("sku"), str) else ""
        if ni_sku:
            return ni_sku
    code = (li.get("itemCode") or "").strip() if isinstance(li.get("itemCode"), str) else ""
    return code


def _api_base(override=None):
    """Resolve the Clover API base URL, honoring an explicit override
    (passed by callers reading CLOVER_API_BASE env var)."""
    return (override or DEFAULT_CLOVER_API_BASE).rstrip("/")


def _clover_request(token, merchant_id, path, params=None, api_base=None):
    """GET against Clover's REST API. Returns parsed JSON body.

    Raises RuntimeError on HTTP error with the response body included for
    diagnosis. (Mirrors shopify_importer's helper.)
    """
    if not (token and merchant_id):
        raise RuntimeError(
            "Missing CLOVER_API_TOKEN or CLOVER_MERCHANT_ID — cannot call API"
        )

    base = f"{_api_base(api_base)}/merchants/{urllib.parse.quote(merchant_id, safe='')}{path}"
    if params:
        base = base + "?" + urllib.parse.urlencode(params, doseq=True)

    req = urllib.request.Request(base, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Clover API error {e.code} on {path}: {err_body[:500]}"
        ) from e


def debug_merchant(token, merchant_id, api_base=None):
    """Diagnostic helper: hits Clover's /merchants/{mId} endpoint (the
    simplest read — any valid token can access merchant metadata) and
    reports the result with a credential fingerprint that doesn't leak
    secrets.
    """
    fingerprint = {
        "token_present": bool(token),
        "token_length": len(token) if token else 0,
        "token_prefix": (token[:6] + "…") if token and len(token) > 6 else "",
        "merchant_id_present": bool(merchant_id),
        "merchant_id_value": merchant_id,
        "api_base": _api_base(api_base),
    }

    if not (token and merchant_id):
        return {"fingerprint": fingerprint, "api_result": "skipped (missing config)"}

    try:
        body = _clover_request(token, merchant_id, "", api_base=api_base)
        return {
            "fingerprint": fingerprint,
            "api_result": {
                "status": 200,
                "merchant_name": body.get("name"),
                "merchant_id_echo": body.get("id"),
                "currency": body.get("currency"),
            },
        }
    except Exception as e:
        return {"fingerprint": fingerprint, "api_result": {"error": str(e)}}


def fetch_orders(token, merchant_id, start_ms, end_ms, api_base=None):
    """Pull all orders in [start_ms, end_ms] from Clover. Returns a list
    of order dicts with line items expanded (so line items carry both
    direct fields and their parent item's fields).

    Pagination: Clover uses offset+limit. Max limit per page is 1000 (we
    use 1000 to minimize round-trips). Capped at 100 pages = 100k orders
    as a sanity guard.
    """
    orders = []
    offset = 0
    page_limit = 1000

    pages = 0
    while True:
        params = {
            "filter": [f"createdTime>={start_ms}", f"createdTime<={end_ms}"],
            "expand": "lineItems,lineItems.item",
            "offset": offset,
            "limit": page_limit,
        }
        body = _clover_request(token, merchant_id, "/orders", params=params, api_base=api_base)
        elements = body.get("elements", []) or []
        orders.extend(elements)

        pages += 1
        if pages >= 100:
            logger.warning("Clover pagination cap reached at %d pages", pages)
            break
        # If we got fewer than page_limit, we're done.
        if len(elements) < page_limit:
            break
        offset += page_limit

    return orders


def aggregate_line_items(orders):
    """Walk every line item across all orders, sum quantities per SKU.

    Returns (by_sku, skipped_no_sku) — same shape as Shopify importer:
        by_sku: {sku_string: {'quantity': int, 'order_ids': [str, ...]}}
        skipped_no_sku: [{'order_id', 'name', 'quantity'}]

    Clover line items don't always have an explicit quantity field — on
    retail POS each scanned item is its own line item with implicit
    qty=1. We default to 1 if 'unitQty' is absent or zero.
    """
    by_sku = {}
    skipped_no_sku = []

    for order in orders:
        oid = order.get("id")
        # Clover wraps line items as { lineItems: { elements: [...] } }
        # when expanded — check both shapes defensively.
        line_items_field = order.get("lineItems")
        if isinstance(line_items_field, dict):
            items = line_items_field.get("elements", []) or []
        elif isinstance(line_items_field, list):
            items = line_items_field
        else:
            items = []

        for li in items:
            sku = _line_item_sku(li)
            # Quantity: retail line items typically default to 1.
            # unitQty is in thousandths for weight items; for unit items
            # it's either absent or equal to 1000 per unit. Be defensive.
            raw_qty = li.get("unitQty")
            if raw_qty in (None, 0):
                qty = 1
            else:
                try:
                    # If unitQty looks like thousandths (>=1000), convert.
                    raw_qty = int(raw_qty)
                    qty = raw_qty // 1000 if raw_qty >= 1000 else 1
                    if qty <= 0:
                        qty = 1
                except (TypeError, ValueError):
                    qty = 1

            if not sku:
                skipped_no_sku.append({
                    "order_id": oid,
                    "name": li.get("name", ""),
                    "quantity": qty,
                })
                continue
            entry = by_sku.setdefault(sku, {"quantity": 0, "order_ids": []})
            entry["quantity"] += qty
            if oid not in entry["order_ids"]:
                entry["order_ids"].append(oid)

    return by_sku, skipped_no_sku


def preview_week(week_id, recipes_data, token, merchant_id, api_base=None):
    """Produce a structured preview of what would be imported for `week_id`.

    Does NOT write to Soma data. Same return shape as
    shopify_importer.preview_week so the commit logic in app.py can be
    a thin wrapper that doesn't care which channel it came from.
    """
    start_ms, end_ms = week_range_ms(week_id)
    orders = fetch_orders(token, merchant_id, start_ms, end_ms, api_base=api_base)
    by_sku, skipped = aggregate_line_items(orders)

    # Set of legal Soma SKU keys for cross-reference.
    valid_keys = set()
    for recipe_name, recipe_meta in (recipes_data or {}).items():
        brand = (recipe_meta.get("brand") or "").strip()
        fmt = (recipe_meta.get("format") or "").strip()
        valid_keys.add(f"{brand}|{recipe_name}|{fmt}")

    matched = []
    unparseable = []

    for sku, info in by_sku.items():
        try:
            brand, recipe, fmt = parse_sku(sku)
        except ValueError as e:
            unparseable.append({
                "sku": sku,
                "quantity": info["quantity"],
                "error": str(e),
            })
            continue

        soma_key = f"{brand}|{recipe}|{fmt}"
        matched.append({
            "sku": sku,
            "brand": brand,
            "recipe": recipe,
            "format": fmt,
            "soma_key": soma_key,
            "quantity": info["quantity"],
            "exists_in_soma": soma_key in valid_keys,
            "order_ids": info["order_ids"],
        })

    matched.sort(key=lambda x: x["sku"])
    unparseable.sort(key=lambda x: x["sku"])

    # Toronto-local ISO range for human-friendly display in the response
    # (same shape as Shopify preview so the UI doesn't need to branch).
    monday = datetime.strptime(week_id, "%Y-%m-%d")
    if TIMEZONE is not None:
        monday = monday.replace(tzinfo=TIMEZONE)
    sunday_end = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    range_start_iso = monday.isoformat()
    range_end_iso = sunday_end.isoformat()

    # Total line item count for visibility (defensive in case of weird shapes)
    def _count_items(o):
        f = o.get("lineItems")
        if isinstance(f, dict):
            return len(f.get("elements", []) or [])
        if isinstance(f, list):
            return len(f)
        return 0
    total_line_items = sum(_count_items(o) for o in orders)

    return {
        "week_id": week_id,
        "range_start": range_start_iso,
        "range_end": range_end_iso,
        "order_count": len(orders),
        "line_item_count": total_line_items,
        "matched": matched,
        "unparseable": unparseable,
        "skipped_no_sku": skipped,
    }

"""
Shopify weekly sales importer (preview-only — deploy 1).

Pulls orders from Shopify's Admin API for a given week, aggregates line items
by SKU, parses each SKU into Soma's (brand, recipe, format) identifiers, and
returns a structured preview.

This module is READ-ONLY. It does not write to any Soma data file. A future
deploy will add a 'commit' function that creates sale rows and triggers FIFO
deduction.

Configuration via environment variables:
    SHOPIFY_CLIENT_ID      — Public app identifier (32-char hex).
    SHOPIFY_CLIENT_SECRET  — Secret value starting with 'shpss_'.
    SHOPIFY_STORE          — Store handle only (e.g. 'fat-top'); '.myshopify.com'
                             is appended here, not in config.

Auth model (changed Jan 2026):
    Shopify deprecated permanent custom-app access tokens on Jan 1, 2026.
    We now use the OAuth 2.0 'client_credentials' grant: each invocation
    exchanges client_id + client_secret for a short-lived access token,
    then uses that token in the X-Shopify-Access-Token header for the
    actual API call. See `get_access_token()` below.
"""

import os
import re
import json
import logging
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 safety net
    ZoneInfo = None

logger = logging.getLogger(__name__)

# Matches the API version released on the custom app's configuration.
SHOPIFY_API_VERSION = "2026-04"

# Week boundaries (Mon 00:00 → Sun 23:59) interpreted in Toronto local time.
TIMEZONE = ZoneInfo("America/Toronto") if ZoneInfo else None

# Format suffix regex: matches 'SS-876ML', 'MJ-473ML', 'FZ-750ML', etc.
# Two uppercase letters, hyphen, digits, 'ML'.
_FORMAT_RE = re.compile(r"^([A-Z]{2})-(\d+)ML$")

# Prefix that identifies a SKU as ours (one we should attempt to parse and
# import). Shopify currently only sells SOMA products so this is a no-op
# safety net, but kept in sync with clover_importer for consistency.
OUR_BRAND_PREFIX = "SOMA-"


def parse_sku(sku):
    """Parse a Shopify SKU string like 'SOMA-Adaptogenic Mushroom-SS-876ML'
    into (brand, recipe, format).

    Strategy:
      - Split on '-'.
      - The LAST two parts must form a valid format suffix (e.g. 'SS-876ML').
      - The FIRST part is the brand.
      - Everything between is the recipe (rejoined with '-' to preserve any
        internal hyphens, though none are expected for SOMA SKUs).

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


def week_range_iso(week_id):
    """Given a week_id ('YYYY-MM-DD' Monday), return (start_iso, end_iso)
    covering Mon 00:00:00 → Sun 23:59:59 in America/Toronto, formatted for
    Shopify's created_at_min/created_at_max filters.
    """
    monday = datetime.strptime(week_id, "%Y-%m-%d")
    if TIMEZONE is not None:
        monday = monday.replace(tzinfo=TIMEZONE)
    sunday_end = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return monday.isoformat(), sunday_end.isoformat()


def day_range_iso(day_id):
    """Given a date ('YYYY-MM-DD'), return (start_iso, end_iso) covering
    00:00:00 -> 23:59:59 of that single day in America/Toronto.

    Same shape as week_range_iso — used by the Daily Review's read-only
    next-morning read, which never writes and never commits.
    """
    day = datetime.strptime(day_id, "%Y-%m-%d")
    if TIMEZONE is not None:
        day = day.replace(tzinfo=TIMEZONE)
    return day.isoformat(), (day + timedelta(hours=23, minutes=59, seconds=59)).isoformat()


def get_access_token(client_id, client_secret, store):
    """Exchange client credentials for a short-lived Admin API access token
    via Shopify's OAuth 2.0 client_credentials grant.

    POST https://{store}.myshopify.com/admin/oauth/access_token
        body: {client_id, client_secret, grant_type: 'client_credentials'}
        response: {access_token, scope}

    Returns the access token string. Raises RuntimeError on failure, with
    the response body included for diagnosis.
    """
    if not (client_id and client_secret and store):
        raise RuntimeError(
            "Missing SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, or "
            "SHOPIFY_STORE — cannot exchange for access token"
        )

    url = f"https://{store}.myshopify.com/admin/oauth/access_token"
    body = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            access_token = data.get("access_token")
            if not access_token:
                raise RuntimeError(
                    f"Shopify OAuth returned no access_token: {data}"
                )
            return access_token
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify OAuth token exchange failed {e.code}: {err_body[:500]}"
        ) from e


def _shopify_request(token, store, path, params=None):
    """GET against Shopify's Admin API. Returns (body_dict, link_header_str).

    On HTTP error, raises RuntimeError with response body included (truncated)
    so the caller can surface a useful message rather than a generic 500.
    """
    base = f"https://{store}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}{path}"
    if params:
        base = base + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(base, headers={
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            link = resp.headers.get("Link", "")
            return body, link
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Shopify API error {e.code} on {path}: {err_body[:500]}"
        ) from e


def _parse_next_page_info(link_header):
    """Shopify uses Link headers for cursor pagination. Example value:
        <https://...orders.json?page_info=abc&limit=250>; rel="next"
    Returns the page_info cursor for 'next', or None if no more pages.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' not in part:
            continue
        m = re.search(r"<([^>]+)>", part)
        if not m:
            continue
        parsed = urllib.parse.urlparse(m.group(1))
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("page_info", [None])[0]
    return None


def fetch_orders(token, store, start_iso, end_iso):
    """Pull all orders within [start_iso, end_iso] from Shopify. Returns a
    list of order dicts with 'id', 'created_at', 'line_items'.

    Hard cap of 100 pages (25,000 orders) as a sanity guard — a weekly DTC
    importer should never approach that.
    """
    orders = []
    params = {
        "status": "any",
        "created_at_min": start_iso,
        "created_at_max": end_iso,
        "limit": 250,
        "fields": "id,created_at,line_items",
    }

    pages = 0
    while True:
        body, link = _shopify_request(token, store, "/orders.json", params=params)
        page_orders = body.get("orders", [])
        orders.extend(page_orders)

        pages += 1
        if pages >= 100:
            logger.warning("Shopify pagination cap reached at %d pages", pages)
            break

        page_info = _parse_next_page_info(link)
        if not page_info:
            break

        # Subsequent pages use only page_info + limit per Shopify's cursor
        # pagination rules.
        params = {"page_info": page_info, "limit": 250}

    return orders


def aggregate_line_items(orders):
    """Walk every line item across all orders, sum quantities per SKU.

    Returns (by_sku, skipped_no_sku) where:
        by_sku: {sku_string: {'quantity': int, 'revenue': float,
                              'order_ids': [int, ...]}}
        skipped_no_sku: [{'order_id', 'title', 'variant_title', 'quantity'}]

    `revenue` is gross line value excluding tax and shipping: unit price x
    quantity, less any line-level discount. It exists for the Daily Review's
    read-only report; the weekly commit ignores it (Soma prices sales from the
    buyer catalogue, not from the channel).

    Line items with empty SKU (bundle parents, free gifts without SKU, etc.)
    are reported separately so they're visible in the preview but never
    counted toward a SKU deduction.
    """
    by_sku = {}
    skipped_no_sku = []

    for order in orders:
        oid = order.get("id")
        for item in order.get("line_items", []):
            sku = (item.get("sku") or "").strip()
            qty = int(item.get("quantity") or 0)
            if not sku:
                skipped_no_sku.append({
                    "order_id": oid,
                    "title": item.get("title", ""),
                    "variant_title": item.get("variant_title", ""),
                    "quantity": qty,
                })
                continue
            entry = by_sku.setdefault(sku, {"quantity": 0, "revenue": 0.0, "order_ids": []})
            entry["quantity"] += qty
            try:
                entry["revenue"] += float(item.get("price") or 0) * qty \
                    - float(item.get("total_discount") or 0)
            except (TypeError, ValueError):
                pass
            if oid not in entry["order_ids"]:
                entry["order_ids"].append(oid)

    return by_sku, skipped_no_sku


def debug_shop(client_id, client_secret, store):
    """Diagnostic helper. Tests the full auth chain end-to-end:
      1. Reports fingerprints of the configured credentials (no secrets leaked).
      2. Exchanges client_id+client_secret for an access token.
      3. Calls Shopify's /shop.json with that access token.

    Returns a structured result showing which step (if any) failed and the
    raw Shopify response for diagnosis.
    """
    fingerprint = {
        "client_id_present": bool(client_id),
        "client_id_length": len(client_id) if client_id else 0,
        "client_id_prefix": (client_id[:6] + "…") if client_id and len(client_id) > 6 else "",
        "client_secret_present": bool(client_secret),
        "client_secret_length": len(client_secret) if client_secret else 0,
        "client_secret_prefix": (client_secret[:6] + "…") if client_secret and len(client_secret) > 6 else "",
        "store_present": bool(store),
        "store_value": store,
    }

    if not (client_id and client_secret and store):
        return {
            "fingerprint": fingerprint,
            "exchange_result": "skipped (missing config)",
            "api_result": "skipped",
        }

    # Step 1: OAuth client_credentials exchange.
    try:
        access_token = get_access_token(client_id, client_secret, store)
    except Exception as e:
        return {
            "fingerprint": fingerprint,
            "exchange_result": {"error": str(e)},
            "api_result": "skipped (exchange failed)",
        }

    exchange_result = {
        "ok": True,
        "access_token_length": len(access_token),
        "access_token_prefix": (access_token[:6] + "…") if len(access_token) > 6 else "",
    }

    # Step 2: actual API call with exchanged token.
    url = f"https://{store}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/shop.json"
    req = urllib.request.Request(url, headers={
        "X-Shopify-Access-Token": access_token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "fingerprint": fingerprint,
                "exchange_result": exchange_result,
                "api_result": {
                    "status": resp.status,
                    "body_excerpt": body[:300],
                },
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {
            "fingerprint": fingerprint,
            "exchange_result": exchange_result,
            "api_result": {
                "status": e.code,
                "body_excerpt": body[:500],
            },
        }
    except Exception as e:
        return {
            "fingerprint": fingerprint,
            "exchange_result": exchange_result,
            "api_result": {"error": str(e)},
        }


def _classify_skus(by_sku, recipes_data):
    """Split aggregated SKUs into (matched, unparseable, skipped_other_brands).

    Shared by preview_week and preview_day so the weekly commit and the Daily
    Review can never disagree about what a SKU means.
    """
    valid_keys = set()
    for recipe_name, recipe_meta in (recipes_data or {}).items():
        brand = (recipe_meta.get("brand") or "").strip()
        fmt = (recipe_meta.get("format") or "").strip()
        valid_keys.add(f"{brand}|{recipe_name}|{fmt}")

    matched = []
    unparseable = []
    skipped_other_brands = []

    for sku, info in by_sku.items():
        # Items that aren't ours (non-SOMA products) are tracked separately
        # so they're visible in the preview but don't block the commit.
        # Only SOMA-prefixed SKUs that fail to parse are "unparseable".
        if not sku.startswith(OUR_BRAND_PREFIX):
            skipped_other_brands.append({
                "sku": sku,
                "quantity": info["quantity"],
                "revenue": round(info.get("revenue", 0.0), 2),
                "order_ids": info["order_ids"],
            })
            continue

        try:
            brand, recipe, fmt = parse_sku(sku)
        except ValueError as e:
            unparseable.append({
                "sku": sku,
                "quantity": info["quantity"],
                "revenue": round(info.get("revenue", 0.0), 2),
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
            "revenue": round(info.get("revenue", 0.0), 2),
            "exists_in_soma": soma_key in valid_keys,
            "order_ids": info["order_ids"],
        })

    matched.sort(key=lambda x: x["sku"])
    unparseable.sort(key=lambda x: x["sku"])
    skipped_other_brands.sort(key=lambda x: x["sku"])
    return matched, unparseable, skipped_other_brands


def preview_week(week_id, recipes_data, client_id, client_secret, store):
    """Produce a structured preview of what would be imported for `week_id`.

    Does NOT write to Soma data. Returns a dict containing:
        - week_id, range_start, range_end
        - order_count, line_item_count
        - matched: list of {sku, brand, recipe, format, soma_key, quantity,
                            revenue, exists_in_soma, order_ids}
        - unparseable: list of {sku, quantity, error}
        - skipped_no_sku: list of line items with empty SKU

    `exists_in_soma` flags whether the parsed SKU corresponds to an actual
    recipe in Soma's recipes.json — caught now rather than at sale-write time.
    """
    # Exchange client credentials for a short-lived access token, then use
    # that token for every API call in this preview run.
    access_token = get_access_token(client_id, client_secret, store)
    start_iso, end_iso = week_range_iso(week_id)
    orders = fetch_orders(access_token, store, start_iso, end_iso)
    by_sku, skipped = aggregate_line_items(orders)
    matched, unparseable, skipped_other_brands = _classify_skus(by_sku, recipes_data)

    total_line_items = sum(len(o.get("line_items", [])) for o in orders)

    return {
        "week_id": week_id,
        "range_start": start_iso,
        "range_end": end_iso,
        "order_count": len(orders),
        "line_item_count": total_line_items,
        "matched": matched,
        "unparseable": unparseable,
        "skipped_no_sku": skipped,
        "skipped_other_brands": skipped_other_brands,
    }


def preview_day(day_id, recipes_data, client_id, client_secret, store):
    """One day's orders, same shape as preview_week plus `day_id`.

    Read-only reporting for the Daily Review. There is deliberately no daily
    commit: the weekly import remains the only path that writes sales and
    deducts FG, so this can never double-count against it.
    """
    access_token = get_access_token(client_id, client_secret, store)
    start_iso, end_iso = day_range_iso(day_id)
    orders = fetch_orders(access_token, store, start_iso, end_iso)
    by_sku, skipped = aggregate_line_items(orders)
    matched, unparseable, skipped_other_brands = _classify_skus(by_sku, recipes_data)

    return {
        "day_id": day_id,
        "range_start": start_iso,
        "range_end": end_iso,
        "order_count": len(orders),
        "line_item_count": sum(len(o.get("line_items", [])) for o in orders),
        "matched": matched,
        "unparseable": unparseable,
        "skipped_no_sku": skipped,
        "skipped_other_brands": skipped_other_brands,
    }

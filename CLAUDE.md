# Soma + Ripe — Codebase Context

## What this is

Two Flask apps deployed on Render:

- **Soma** (`soma-scheduler.onrender.com`) — internal production management for a bone broth company. Handles weekly production scheduling, organic inventory (raw materials + finished goods), sales records, recipes, traceability, checklists, equipment maintenance, and buyer/supplier management.
- **Ripe** — wholesale buyer portal. Buyers log in, view their SKU catalogue and pricing, place orders. Connects to Soma via an internal API authenticated with `INTERNAL_API_KEY`.

---

## Repository structure

```
app.py              — ~8470 lines, ~160 routes, ~250 functions. Most code lives here.
ripe_orders.py      — Flask Blueprint (591 lines) handling Ripe order workflow within Soma
shopify_importer.py — Shopify Admin API client. Pulls orders for a week,
                      parses SKUs, returns a structured preview. Auth via
                      OAuth client_credentials (mandatory since Jan 2026).
clover_importer.py  — Clover REST API client. Same preview shape as Shopify
                      so app.py's commit logic is channel-symmetric. Bearer
                      token auth using a Merchant Dashboard API token.
pdf_engine.py       — PDF generation (labels, checklists, schedules)
vision_scan.py      — Receipt photo OCR
default_recipes.py  — Seed data
add_pwa_tags.py     — PWA manifest support
supplier_routes.py  — Placeholder (routes are in app.py)
templates/          — Jinja2 HTML templates (one per page)
static/             — CSS, JS, images
```

No `helpers.py`, `cogs.py`, or `equipment.py` in this baseline — non-importer code is all in `app.py`.

---

## Architecture

**Single `app.py` monolith** — intentional for stability. A previous session attempted to split into blueprints using string replacement, which caused cascading damage. The correct approach when resuming that work is Python's `ast` module for exact function boundary extraction.

**Data storage:** JSON files on Render persistent disk at `DATA_DIR=/opt/render/project/data`

Key data files:
```
recipes.json                        — recipe definitions
schedules/{week_id}.json            — weekly production schedules
checklists/{week_id}_day{N}.json    — daily production checklists
inventory/sales.json                — organic sales records
inventory/finished_goods.json       — finished goods inventory
inventory/raw_materials.json        — raw material inventory
inventory/production_runs.json      — production runs
inventory/buyers.json               — buyer accounts and SKU pricing
inventory/suppliers.json            — supplier records
inventory/sku_meta.json             — PAR levels per SKU key
inventory/company_info.json         — company settings and order rules
equipment.json                      — equipment and service logs
ccp_master.json                     — master CCP document
```

---

## Key patterns

**week_id format:** `YYYY-MM-DD` (Monday of the week) — used in URL paths, schedule filenames, and all data records. Validated by `validate_week_id()`.

**SKU key format:** `BRAND|RECIPE|FORMAT` — normalised via `_sku_key(brand, recipe, fmt)`. Format is normalised (e.g. `ss-750ml` → `SS-750ML`) before building the key.

**JSON persistence:**
```python
_load_json(path, default)   # reads with per-path threading lock
_save_json(path, data)      # atomic: writes .tmp then os.replace()
```
Both use `_FILE_LOCKS` (threading.Lock per path) added in the latest session.

**Auth:** `@login_required` decorator checks `session["authenticated"]`. Unauthenticated API requests get 401 JSON; page requests redirect to `/`.

**Route validation decorators:**
- `@require_valid_week` — validates `week_id` path param as `YYYY-MM-DD`
- `@require_valid_day` — validates `day_idx` as 0–6

**Logging:** `import logging; logger = logging.getLogger(__name__)` at module level. Used for `logger.warning()` in schedule cascade and Ripe notification failures.

**Ripe→Soma:** Ripe calls Soma's `/api/internal/*` endpoints with `X-Internal-Key` header matching `INTERNAL_API_KEY` env var.

**FIFO deduction:** `_run_scheduled_deductions()` runs at startup — auto-deducts Ripe sale records from FG inventory when `deduction_date <= today`.

**Sales channels:** Sale records optionally have a `channel` field (`'shopify'`, `'clover'`, or absent for legacy/manual). Channel-imported sales also carry `week_id`, `source_order_ids` (list of upstream Shopify/Clover order IDs for traceability), and a deterministic `order_id` of the form `ORD-{CHANNEL}-{week_id}` so multi-SKU imports group as one transaction in Soma's UI (matching the existing `add_sale_order` order_id grouping).

**Cron-driven imports:** Two internal endpoints exist for Render Cron Jobs:
- `POST /api/internal/shopify-import-week`
- `POST /api/internal/clover-import-week`

Both auth via `X-Internal-Key` matching `INTERNAL_API_KEY` (timing-safe via `hmac.compare_digest`). Each computes "last fully-completed Mon→Sun week" in `America/Toronto` and runs its channel's commit logic. Idempotent: re-runs skip SKUs already imported for `(channel, week_id, sku_key)`.

**Brand prefix filter:** SKUs that don't start with `SOMA-` route to `skipped_other_brands[]` rather than `unparseable[]`. This matters for Clover (which sells non-SOMA retail items alongside the jars) — those items don't block the commit. Only SOMA-prefixed SKUs that fail to parse are treated as real errors.

---

## Raw-material consumption & organic traceability (hardened 2026-05/06)

This is the audit-critical chain: supplier lot → production run → finished goods → sale. Several invariants are enforced in code; **do not loosen them without understanding the audit impact.**

**Raw-material deduction is date-aware and per-batch.** When daily production is saved, `_complete_organic_run(finish_week, finish_day, produced)` processes runs that STARTED the previous day. The shared helper `_deduct_run_ingredients(...)` deducts each ingredient FIFO from `_eligible_lots_for_date(materials, run_start_date)` — only lots with `date_received <= the run's start date`, oldest-first. The raw charge is **per-batch** (from the recipe, scaled only by vessel: 115L = half); it does NOT scale with jars produced. "Amount produced" only gates deduction (`>0`) and sets the FG quantity.

**Completed batch records are FROZEN.** On an ordinary re-save of an already-completed, still-producing run, `ingredients_used` and the lot balances are left untouched (only FG quantity updates). The deduction is computed only on FIRST completion or on reversal (`amount<=0`, which restores materials + removes FG). This stops a later recipe edit from silently rewriting what a past batch consumed. Each `ingredients_used` line snapshots `item, supplier_lot, quantity_used, unit, raw_material_id, supplier, date_received` so the record is self-contained for one-step-back.

**The ONLY sanctioned "recompute everything" path is the reconcile tool** (`/admin/reconcile-raw`, page + `/admin/reconcile-raw/run` GET=preview/POST=apply; `_rebuild_raw_material_consumption`). It resets each lot to received quantity and replays all completed runs date-aware. **Consequence: re-saving a completed production day no longer "heals" historical inventory — use the reconcile tool.** Apply overwrites manual `Edit Qty` adjustments (preview lists affected lots).

**Deletions cascade or are blocked:**
- `delete_traceability_record` (DELETE a completed production record): refuses (409) if any FG from that day was sold; else restores consumed raw materials, removes that day's FG, resets the runs to `scheduled`, then deletes the checklist + PDF. Never orphans.
- `delete_raw_material`: 409 if any completed run consumed the lot (`_runs_using_raw_material`); unused lots delete, missing → 404.

**Receipts never store a blank lot.** Both add paths (`add_raw_material`, `add_raw_materials_bulk`) stamp `MAN-DDMMYY` when no supplier lot# is given and flag `no_supplier_lot=true` (surfaced as "⚠ no supplier lot"). Baseline counts use `BL-DDMMYY` (unflagged). Note: lot strings are NOT unique (all baseline ingredients share one `BL-` string), so the trace keys on the unique `raw_material_id`, not the lot string.

**Trace & audit endpoints (read-only):**
- `GET /api/organic/trace?type=raw_lot|fg_lot&q=` — `raw_lot` resolves the lot string to raw-material entries and traces each by `raw_material_id` (grouped per physical lot, with a legacy string-match fallback).
- `GET /api/organic/stock-exceptions` — completed batches made with insufficient raw material (the `INSUFFICIENT_STOCK` markers); shown in a "Stock exceptions" panel on Completed Production.
- `GET /api/organic/mass-balance?from=&to=&organic_only=` + page `/mass-balance` (`_compute_mass_balance`): Opening+Received−Consumed=Expected vs current stock (raw), Opening+Produced−Sold=Expected vs current (FG). Discrepancy = adjustments/loss/breakage, exact when `to`=today. Client-side CSV export. Linked from both the Inventory and Completed Production headers.
- `get_traceability` reports certification **per vessel** (`cert_by_vessel`, `certifications[]`) — a day can run mixed certs; never collapse to one label. Known loose end: it ignores the `?filter=` param, so the Organic/Non-Organic buttons on that page are currently cosmetic.

**Receipt photos:** one per delivery, stored as `<entry_id>.<ext>` in `rm_receipt_photos/`, anchored to the first entry of a bulk save. `GET /api/organic/raw-materials/receipt-photos` lists which entry ids have one; the Receiving list shows a "📎 Invoice" button per delivery.

**Two pages, confusingly named:** "Manage Inventory" = `templates/organic.html` (4 tabs: Raw Materials / Production Runs / Finished Goods / Records). "Completed Production" = `templates/traceability.html` (the page formerly called Traceability; week records, HOO sign-off, stock exceptions, per-vessel certs). New templates this session: `mass_balance.html`, `reconcile_raw.html`.

---

## Environment variables

**Soma:**
- `DATA_DIR=/opt/render/project/data`
- `SECRET_KEY`
- `APP_PASSWORD`
- `MANAGER_PASSWORD`
- `INTERNAL_API_KEY` — used by Ripe→Soma calls AND by the Shopify/Clover cron jobs
- `RIPE_PORTAL_URL`
- `SHOPIFY_CLIENT_ID` — public hex from the custom app's API credentials
- `SHOPIFY_CLIENT_SECRET` — `shpss_...` value from the same page
- `SHOPIFY_STORE` — store handle only (e.g. `fat-top`); `.myshopify.com` is appended in code
- `CLOVER_API_TOKEN` — Merchant Dashboard API token with `Orders: read` + `Inventory: read` scopes
- `CLOVER_MERCHANT_ID` — alphanumeric merchant identifier (e.g. `2KC4HPQ71T6W1`), NOT the numerical MID used by card processors
- `CLOVER_API_BASE` — optional; defaults to `https://api.clover.com/v3`

**Ripe:**
- `DATA_DIR`, `SECRET_KEY`, `RIPE_PASSWORD`, `INTERNAL_API_KEY`, `SOMA_APP_URL`
- `SMTP_USER`, `SMTP_PASS`, `RIPE_CONTACT_EMAIL`, `SOMA_NOTIFY_EMAIL`, `SOMA_ETRANSFER_EMAIL`
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RIPE_BILLING_EMAIL`

---

## Sales channel imports

Two automated weekly imports run on Render Cron Jobs (separate services from the web service):

| Cron Job service | Schedule | Hits | Writes |
|---|---|---|---|
| `soma-shopify-weekly` | `0 14 * * 1` (Mon 14:00 UTC) | `/api/internal/shopify-import-week` | buyer `SOMA (Shopify)`, channel `shopify`, `ORD-SHOPIFY-{week}` |
| `soma-clover-weekly`  | `15 14 * * 1` (Mon 14:15 UTC) | `/api/internal/clover-import-week`  | buyer `SOMA (Clover)`, channel `clover`, `ORD-CLOVER-{week}` |

Each channel has a matching set of routes for manual operation (in `app.py`):

```
/admin/{channel}-debug      diagnostic (auth + scope check, no order data)
/admin/{channel}-preview    read-only JSON of what would be imported
/admin/{channel}-commit     POST — writes sales + FIFO-deducts FG
/admin/{channel}-import     browser control page (Preview + Commit buttons)
```

Where `{channel}` is `shopify` or `clover`. The two modules are deliberate near-duplicates rather than an abstracted base — each channel's behavior reads end-to-end in one place. If a third channel is added later, that's the moment to consider extracting.

**Shopify line item snapshot gotcha:** Shopify captures `line_item.sku` at order-creation time as a snapshot of the variant. Adding a SKU to a variant later does not retroactively populate orders placed before. So only orders placed after SKUs were filled in will import. Clover does not have this issue — its line items carry SKUs at creation reliably.

**Clover line item SKU extraction is defensive:** Clover's response shape varies by merchant config. `_line_item_sku()` in `clover_importer.py` checks `line_item.sku`, then `line_item.item.sku` (requires `?expand=lineItems.item`), then `line_item.itemCode` — first non-empty wins.

---

## Current code quality state

**Fixed in last session:**
- `_save_json` is now atomic (`os.replace`)
- Per-path threading locks on all reads and writes
- Duplicate `_prod_date` nested function removed
- `logger` undefined bug fixed (was crashing at lines touching schedule cascade)
- 127 restatement comments removed (6796 lines, down from 7126)

**Known style debt (not bugs):**
- JavaScript in templates uses `var` throughout (1781 uses) — 51 `let`/`const` are the inconsistent ones. Mechanical replacement is risky due to `var` hoisting semantics; use `jscodeshift` or eslint `--fix`.
- 905 hardcoded hex colour values in templates vs 548 CSS variable uses. CSS variable system is partially adopted. Needs palette audit before fixing.
- Only 125/236 functions have docstrings.
- No type annotations (one exception).
- 126 route handlers have 1 blank line before them instead of PEP 8's 2.

**Recommended tools for the style debt:**
- `jscodeshift` for `var` → `let`/`const`
- `eslint --fix` for JS style
- `black` or `autopep8` for Python formatting
- Playwright for visual regression after CSS changes

---

## Deployment workflow

Render is connected to `github.com/somabonebroth/soma-scheduler` and auto-deploys on push to `main`. There is no manual zip upload.

1. Make changes on a worktree/feature branch
2. Commit
3. `git push origin HEAD:main` (fast-forwards `main`; Render builds + deploys automatically)
4. Watch Render logs through startup — confirm no traceback
5. Smoke test in browser before the next change

**One change at a time, deployed and confirmed before the next.**

**Smoke test after every deploy:**
1. Dashboard loads
2. Weekly schedule loads
3. Organic inventory loads
4. Recipes page loads
5. Open a production record

---

## Pending architectural work

**Blueprint split** (do this in Claude Code, not chat):
- Use `ast` module to map exact function line ranges before extracting anything
- Build `helpers.py` first as the dependency-free foundation layer
- Extract one blueprint at a time, verify before next
- Suggested order: `suppliers.py` (smallest, most isolated) → `buyers.py` → `recipes.py` → `sales.py` → `inventory.py` → `production.py`
- `app.py` would reduce to ~2500 lines of core/auth/analytics

**Function domains already mapped** (from earlier AST audit):
```
buyers:     14 functions, ~532 lines — no cross-domain deps after helpers extracted
suppliers:   7 functions, ~65 lines  — cleanest, best to start here
recipes:    17 functions, ~479 lines
sales:      14 functions, ~699 lines
inventory:  32 functions, ~1093 lines
production: 31 functions, ~883 lines
```

Private helpers that must move to `helpers.py` before any blueprint extraction:
`_revenue`, `_cases`, `_sku_display`, `_prod_date`, `_entry_prod_date`,
`_aggregate_lots_for_sku`, `_group_fg_by_sku`, `_infer_section_for_ingredient`,
`_ingredient_section_key`, `_jar_volume_liters`, `_previous_day_coords`,
`_record_adjustment`, `_runs_using_raw_material`, `_section_for_ingredient`,
`_load_rm_sections`, `_add_contact`, `_load_company_info`, `build_display_name`,
`_classify_format`

**COGS model** — was built and worked, then removed due to instability from an unrelated session. Can be re-added once architecture is stable. The feature itself was sound.

---

## What NOT to do

- Do not use string replacement (`src.replace(block, '')`) to extract functions from app.py — this caused major damage in a previous session by silently truncating adjacent functions
- Do not modify `ripe_orders.py` without also checking the Ripe portal app — they share an API contract
- Do not deploy without checking Render logs — startup crashes return 500 on all routes and look like "data missing"
- Do not touch the data directory on Render — all deploys should exclude `data/*`

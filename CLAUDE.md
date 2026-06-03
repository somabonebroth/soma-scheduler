# Soma + Ripe — Codebase Context

## What this is

Two Flask apps deployed on Render:

- **Soma** (`soma-scheduler.onrender.com`) — internal production management for a bone broth company. Handles weekly production scheduling, organic inventory (raw materials + finished goods), sales records, recipes, traceability, checklists, equipment maintenance, and buyer/supplier management.
- **Ripe** — wholesale buyer portal. Buyers log in, view their SKU catalogue and pricing, place orders. Connects to Soma via an internal API authenticated with `INTERNAL_API_KEY`.

---

## Repository structure

```
app.py              — ~7940 lines, 166 routes. Still the core, but the blueprint
                      split is now underway (see "Pending architectural work").
helpers.py          — Foundation layer (extracted 2026-06-03): dependency-free
                      stdlib-only primitives — JSON IO with per-path locks, path/
                      config constants, format/SKU/date helpers. app.py imports
                      these back. Must NOT import app.py (circular).
suppliers.py        — Flask Blueprint: /api/suppliers CRUD (extracted 2026-06-03)
buyers.py           — Flask Blueprint: /api/buyers CRUD only (extracted 2026-06-03).
                      Shared buyer helpers stay in app.py; reached via `import app`.
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

`helpers.py` now exists (foundation layer, 2026-06-03). No `cogs.py` or `equipment.py` — that code is still in `app.py`. `supplier_routes.py` remains a dead placeholder (suppliers are now in `suppliers.py`); it can be deleted.

---

## Architecture

**`app.py` was a single monolith** — intentional for stability. A previous session attempted to split into blueprints using string replacement, which caused cascading damage. The split is now actively underway using the correct method: Python's `ast` module for exact function-boundary (line-range) extraction, one blueprint at a time, each verified and deployed before the next. See "Pending architectural work" for what's done and the established patterns.

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

**Two pages, confusingly named:** "Manage Inventory" = `templates/organic.html` (4 tabs: Raw Materials / Production Runs / Finished Goods / Records). "Completed Production" = `templates/traceability.html` (the page formerly called Traceability; week records, HOO sign-off, stock exceptions, per-vessel certs). Two more standalone tool pages: `mass_balance.html` and `reconcile_raw.html` (both link the shared `static/style.css`).

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
- Hardcoded hex colour values remain in templates alongside CSS variable uses, but every non-standalone template has now had its exact-value hexes swept to tokens (2026-06-03). The literals that remain are token-less by design — JS logic/categorical palettes and the round-2 promotion candidates (see "UI styling convention" below). See that section for the cascade + remaining unification work.
- Only 125/236 functions have docstrings.
- No type annotations (one exception).
- 126 route handlers have 1 blank line before them instead of PEP 8's 2.

**Recommended tools for the style debt:**
- `jscodeshift` for `var` → `let`/`const`
- `eslint --fix` for JS style
- `black` or `autopep8` for Python formatting
- Playwright for visual regression after CSS changes

### UI styling convention (the cascade — follow for ALL new pages)

There IS a design system: [`static/style.css`](static/style.css). It defines the
token palette (`:root` — colours, `--r-*` radius scale), and the component layer
(`.card`, `.btn`/`.btn-*`, `.status`, `.modal`, `.header`, `.nav-arrow-btn`).
Think in three layers, top-down: **tokens → components → page layout.** Pages
should add *layout only*; they must not re-implement colours or components.

Rules for any new or edited template:
1. **Link the shared sheet** in `<head>`: `<link rel="stylesheet" href="/static/style.css">`. Every page except `login.html` (intentional standalone) does this.
2. **Never redeclare `:root`** in a template. Adding a second `:root` forks the palette (this is how `mass_balance.html` ended up with a different `--accent` green). Need a new colour? Add a token to `static/style.css`, then reference it.
3. **Never hardcode a hex that already has a token.** Use `var(--accent)`, `var(--bg)`, `var(--action-green)`, etc. Common offenders that are already tokens: `#2e7d32`=`--action-green`, `#c62828`=`--action-red`, `#f5f5f0`=`--bg`, `#e8f5e9`=`--action-green-bg`.
4. **Reuse components** (`.btn-primary`, `.card`, `.status`…) instead of restyling buttons/cards inline.
5. Inline `<style>` is for **page-specific layout** (this grid, this table width) — not a place to re-derive the global look.

Unification is essentially complete (the history below is kept as the method record). The hex→token sweep was provably no-op (each swap value-identical), so small templates were batched per deploy; anything visual (de-forking a `:root`, converging near-dupe colours) stayed its own eyes-on deploy. Status:
- **Done:** deleted dead `cogs.html`; linked `mass_balance.html` + `reconcile_raw.html` to the shared sheet.
- **Done:** extended the token set with the most-duplicated orphan literals — `--white`/`--black`/`--text-muted`, `--info`/`--indigo`, the material-amber family (`--amber-bg/-border/-text/-text-dark`, `--orange`), `--green-light`/`--green-tint`. These match values already in the templates, so collapsing to them is a no-op visually.
- **Done (hex→token swept):** `dashboard`, `weekly_view`, `production_tracker`, plus a batch of 12 small templates (`recipes`, `ripe_sku_audit`, `certifications`, `analytics`, `reconcile_raw`, `ripe_packing_slip`, `create_schedule`, `master_ccp`, `important_documents`, `company_settings`, `ripe_analytics`, `organic_certification`); then `organic` (730 lines, audit-critical — own deploy, 122 swaps); then a batch of the 9 remaining small templates (`audit`, `contacts`, `buyer_analytics`, `ripe_products`, `audits`, `daily_production`, `buyer_edit`, `ripe_orders`, `traceability` — 190 swaps). Method: replace exact-value hex matches in CSS position **only outside `<script>`**, leaving JS logic palettes and arbitrary categorical palettes (chart bucket colours, packing-slip group borders) literal.
- **Not yet swept:** none — all non-standalone templates are swept. `login.html` is intentionally standalone — leave it.
- **mass_balance.html** — DONE: de-forked onto the shared palette (removed the local `:root` that overrode `--accent`/#3d5a3d, `--border`, `--text`, `--text-light`); now inherits canonical tokens. (Its remaining page-local literals — `#fff`, `#2e7d32`, `#c62828`, off-whites — were left for a later no-op hex→token pass.)
- **Token round-2 — DONE (hybrid).** Promoted 8 token-less literals at exact value (no-op): `--info-dark` #1565c0, `--info-border` #1976d2, `--warning-text` #856404, `--success-text` #155724, and a cool neutral grey ramp `--grey-dark` #666 / `--grey` #888 / `--grey-light` #ccc / `--grey-border` #ddd (distinct from the warm `--text`/`--border` family). Then converged only the two *imperceptible* near-dupes (own eyes-on deploy): `--action-blue` now aliases `var(--info)` (#0277bd→#0288d1); `#333`→`var(--text)`. **Deliberately left exact:** the amber (`#8a6900`/`#856404`) and orange (`#e67e22`/`#e65100`) merges — they touch audit-critical warning banners, so a visible shift there wasn't worth it. `#aaa` (1 use) left literal.
- **Style unification is effectively complete.** Every non-standalone template references the shared sheet and uses tokens for all exact-value colours. What remains literal is intentional: JS logic/categorical palettes (chart buckets, packing-slip borders), the deliberately-unmerged amber/orange near-dupes above, and a long tail of true one-off hexes with no token. If a future page introduces a recurring new colour, add a token first (never redeclare `:root`).

---

## Deployment workflow

Render is connected to `github.com/somabonebroth/soma-scheduler` and auto-deploys on push to `main`. There is no manual zip upload.

1. Make changes on a worktree/feature branch
2. Commit
3. `git push origin HEAD:main` (fast-forwards `main`; Render builds + deploys automatically)
4. Watch Render logs through startup — confirm no traceback
5. Smoke test in browser before the next change

**One change at a time, deployed and confirmed before the next.** (Exception: a *provably* no-op mechanical sweep — e.g. value-identical hex→token swaps across several small templates — may be batched into one deploy, since there's nothing per-file to confirm. Anything with a visual or behavioural change, including de-forking a `:root`, stays one-at-a-time.)

**Smoke test after every deploy:**
1. Dashboard loads
2. Weekly schedule loads
3. Organic inventory loads
4. Recipes page loads
5. Open a production record

---

## Pending architectural work

### Blueprint split — IN PROGRESS (started 2026-06-03)

The method (do this in Claude Code, not chat): use the `ast` module to get exact
`(lineno, end_lineno)` for every function/constant, then **slice by line range —
never text-match or `src.replace()`**. One blueprint per step. Each step:
build → verify → commit → `push origin HEAD:main` → poll the live site through the
Render restart → browser smoke test → STOP for the next.

**Per-step verification gate (all must pass before push):**
- both files compile; no duplicate defs left in `app.py`; every moved name still resolves
- **URL + method route map byte-identical** to before (166 routes; only endpoint
  *names* may gain a `blueprint.` prefix — fine as long as nothing uses `url_for` on them)
- a test-client functional smoke of the moved routes (real CRUD, all status codes)

**Done (all on `main`, deployed green, 2026-06-03):**
- ✅ **`helpers.py`** (commit `3751302`) — the dependency-free foundation layer. 34 names:
  IO infra (`_load_json`/`_save_json`/`_get_file_lock`/`_FILE_LOCKS`/`_FILE_LOCKS_LOCK`),
  path/config constants (`DATA_DIR`, `INVENTORY_DIR`, `ORGANIC_RUNS_PATH`,
  `ORGANIC_CONTACTS_PATH`, `COMPANY_INFO_PATH`, `ADJUSTMENTS_PATH`, `RM_SECTIONS_PATH`,
  `_DEFAULT_COMPANY_INFO`, `DEFAULT_RM_SECTIONS`), the format/SKU cluster (`FORMAT_RE`,
  `FORMAT_PREFIX_CANONICAL`, `_FORMAT_SUFFIX_RE`, `_normalize_format`,
  `_strip_format_suffix`, `_sku_key`, `_classify_format`, `build_display_name`,
  `_sku_display`), and date/section/lot helpers (`_prod_date`, `_previous_day_coords`,
  `_jar_volume_liters`, `_ingredient_section_key`, `_section_for_ingredient`,
  `_load_rm_sections`, `_runs_using_raw_material`, `_add_contact`,
  `_aggregate_lots_for_sku`, `_record_adjustment`, `_load_company_info`).
  `app.py` imports all 34 back. `_group_fg_by_sku` was deliberately left in `app.py`
  (recipe-domain coupling via `load_recipes`).
- ✅ **`suppliers.py`** (commit `fdc6985`) — first blueprint. Fully self-contained:
  owns `SUPPLIERS_PATH` + `_load_suppliers`/`_save_suppliers` + 5 `/api/suppliers`
  routes; pulls IO from `helpers`; defines its own `login_required`.
- ✅ **`buyers.py`** (commit `c3b1403`) — **CRUD routes only** (6 `/api/buyers`
  endpoints). The shared buyer helpers (`_load_buyers`, `_save_buyers`,
  `_buyer_resolver`, `_all_sku_catalog`, `BUYERS_PATH`) STAY in `app.py` because
  analytics/sales/Ripe-internal code also calls them; the blueprint reaches them at
  request time via `import app` + qualified calls (`app._load_buyers(...)`). Buyer
  *analytics* routes (`api_buyer_analytics`, `api_sales_by_buyer`,
  `buyer_analytics_page`, `buyer_edit_page`) and `internal_buyer_catalogue` remain in
  `app.py` (future analytics/sales blueprints).

**Two reusable blueprint patterns are now established** (both define a local verbatim
`login_required` to avoid a circular import, matching `ripe_orders.py`):
1. **Self-contained** — when a domain owns its data file and helpers (suppliers).
2. **Routes-move, helpers-stay** — when helpers are shared cross-domain (buyers).
   Blueprint does a bare `import app` at module top (binds the partially-initialised
   module safely; attributes resolve only at request time) and calls `app.X(...)`.
`ripe_orders.py` was untouched and still resolves its lazy `from app import` calls
because `app.py` re-exports the moved names.

**NEXT: `recipes.py`** — analysis complete, not yet built (deferred to a fresh session
as the highest-risk step). Use the buyers pattern + a strengthened safety net (a
free-variable AST audit of the new file asserting every name resolves to {local,
param, import, builtin, flask, or `app`}). Key facts:
- **17 route handlers** to move (per-function slicing — the block is NOT contiguous):
  `get_recipes`, `add_recipe`, `get_recipe`, `recipe_pdf`, `recipe_pdf_all`,
  `update_recipe` (**151-line cross-domain cascade** — rewrites buyers/FG/sales/
  schedules/sku_meta on recipe rename), `delete_recipe`, `duplicate_recipe`,
  `archive_recipe`, `unarchive_recipe`, `migrate_all_recipes`, `upload_recipe_photo`,
  `get_recipes_grouped`, `update_recipe_order`, `upload_recipe`, `upload_recipe_json`,
  plus the page route `recipes_page`.
- `_schedules_using_recipe` is **recipe-private** (only `archive_recipe` calls it) →
  move it INTO `recipes.py`; qualify its own deps (`app.SCHEDULES_DIR`, `os`, `json`).
- **Interleaved NON-recipe code to slice around (do NOT move):** the `PHOTOS_DIR`
  assignment + its `os.makedirs`, and the `serve_photo` route.
- **16 app-internal names to `app.`-qualify** (functions AND bare constants — constants
  can't use a `name(`→`app.name(` trick, need word-boundary edits): funcs
  `load_recipes`, `save_recipes`, `load_recipe_order`, `save_recipe_order`,
  `migrate_recipe_ingredients`, `parse_recipe_pdf_text`, `_load_tracking_modes`,
  `_load_buyers`, `_save_buyers`; constants `ORGANIC_FG_PATH`, `ORGANIC_SALES_PATH`,
  `SCHEDULES_DIR`, `SKU_META_PATH`, `RECIPES_PATH`, `PHOTOS_DIR`, `INGREDIENT_SECTIONS`.
  Direct imports in `recipes.py`: `os`, `re`, `io`, `json`, `datetime`, a module
  `logger`, and `generate_single_recipe_pdf`/`generate_all_recipes_pdf` from `pdf_engine`.
- Smoke must exercise the `update_recipe` cascade specifically.

**Remaining domains after recipes** (one per session given growing entanglement):
`sales` (~699 lines), `inventory` (~1093 lines), `production` (~883 lines). Expect the
same routes-move/helpers-stay pattern. Note the "no cross-domain deps" optimism in the
original audit did NOT hold for buyers or recipes — assume shared helpers stay in `app.py`.

> Correction to the earlier audit: the helper list once named four functions that don't
> exist as movable top-level defs — `_revenue`, `_cases`, `_entry_prod_date` are **nested
> closures**, and `_infer_section_for_ingredient` **doesn't exist**. The real, movable
> foundation is the 34-name set now in `helpers.py` (above).

**COGS model** — was built and worked, then removed due to instability from an unrelated session. Can be re-added once architecture is stable. The feature itself was sound.

---

## What NOT to do

- Do not use string replacement (`src.replace(block, '')`) to extract functions from app.py — this caused major damage in a previous session by silently truncating adjacent functions
- Do not modify `ripe_orders.py` without also checking the Ripe portal app — they share an API contract
- Do not deploy without checking Render logs — startup crashes return 500 on all routes and look like "data missing"
- Do not touch the data directory on Render — all deploys should exclude `data/*`

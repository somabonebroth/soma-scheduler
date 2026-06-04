# Soma + Ripe — Codebase Context

## What this is

Two Flask apps deployed on Render:

- **Soma** (`soma-scheduler.onrender.com`) — internal production management for a bone broth company. Handles weekly production scheduling, organic inventory (raw materials + finished goods), sales records, recipes, traceability, checklists, equipment maintenance, and buyer/supplier management.
- **Ripe** — wholesale buyer portal. Buyers log in, view their SKU catalogue and pricing, place orders. Connects to Soma via an internal API authenticated with `INTERNAL_API_KEY`.

---

## Repository structure

```
app.py              — ~4749 lines, 166 routes. Still the core, but the blueprint
                      split is now underway (see "Pending architectural work").
helpers.py          — Foundation layer (extracted 2026-06-03): dependency-free
                      stdlib-only primitives — JSON IO with per-path locks, path/
                      config constants, format/SKU/date helpers. app.py imports
                      these back. Must NOT import app.py (circular).
suppliers.py        — Flask Blueprint: /api/suppliers CRUD (extracted 2026-06-03)
buyers.py           — Flask Blueprint: /api/buyers CRUD only (extracted 2026-06-03).
                      Shared buyer helpers stay in app.py; reached via `import app`.
recipes.py          — Flask Blueprint (637 lines, extracted 2026-06-03): 17 recipe
                      routes + recipe-private _schedules_using_recipe, incl. the
                      update_recipe rename cascade. Shared recipe/buyer helpers stay
                      in app.py (reached via `import app`); foundation names imported
                      from helpers. PHOTOS_DIR + serve_photo stay in app.py.
sales.py            — Flask Blueprint (731 lines, extracted 2026-06-03): the 7
                      organic-sales routes (/api/organic/sales*: get/add/add-order/
                      edit/delete + packing-slip + qbo-csv). Buyer helpers + the
                      ORGANIC_FG_PATH/ORGANIC_SALES_PATH constants stay in app.py
                      (app.-qualified); foundation IO from helpers. Channel imports,
                      sales analytics, and trace deliberately NOT included.
finished_goods.py   — Flask Blueprint (477 lines, extracted 2026-06-03): the 10
                      finished-goods routes (/api/organic/finished-goods*). First
                      slice of the "inventory" domain. FG grouping helpers
                      (_group_fg_by_sku/_group_fg_with_catalog) + load_recipes +
                      ORGANIC_FG_PATH/SKU_META_PATH stay in app.py (app.-qualified);
                      foundation IO from helpers. No consumption-chain code touched.
raw_materials.py    — Flask Blueprint (771 lines, extracted 2026-06-03): the 21
                      raw-materials routes (RM CRUD incl. bulk, ingredients,
                      sections/assignments, by-ingredient lots, invoices, receipt
                      photos). Second inventory slice. PURE routes-move: ALL helpers
                      + constants stay in app.py (19 app.-qualified, incl. the 3
                      RM-private invoice helpers — invoice cluster kept together);
                      foundation IO from helpers. No consumption-chain code touched
                      (only delete's 409 guard via helpers._runs_using_raw_material).
audit_tools.py      — Flask Blueprint (256 lines, extracted 2026-06-03): the 6
                      audit/traceability routes — reconcile-raw (page + run preview/
                      apply), organic trace, stock-exceptions, mass-balance (api +
                      page). Third inventory slice. PURE routes-move: the audit-critical
                      engines (_rebuild_raw_material_consumption, _compute_mass_balance,
                      _sale_touches_fg) + path consts stay in app.py (8 app.-qualified);
                      ORGANIC_RUNS_PATH + IO from helpers.
production.py       — Flask Blueprint (763 lines, extracted 2026-06-03): the FULL production
                      domain, built up over 3 slices. (1) schedules (create/weekly pages,
                      get/list/delete) + production tracker (page + week/month/year).
                      (2) daily-production (page/GET/save) + checklists (GET/POST/complete/
                      status) — save_daily_production triggers the consumption chain
                      (_check_organic_completion→_complete_organic_run→_deduct_run_
                      ingredients, ALL kept in app.py). (3) traceability/completed-records
                      (page, week-summary, get_traceability, delete_traceability_record
                      cascade, weekly sign-off/unsign). PURE routes-move (app.-qualified).
                      Local verbatim copies of login_required + require_valid_week +
                      require_valid_day (decorators apply at import time → can't be
                      app.-qualified; they call app.validate_week_id/day_idx at request time).
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
- ✅ **`recipes.py`** (commit `721cf23`) — **highest-risk step to date.** 17 recipe
  routes + recipe-private `_schedules_using_recipe` (incl. `update_recipe`'s 151-line
  rename cascade across FG/sales/runs/sku_meta/buyers/schedules). Used the buyers
  routes-move/helpers-stay pattern. **Lesson:** the strengthened free-variable audit
  caught 8 names the original plan misclassified — `_load_json`, `_save_json`,
  `DATA_DIR`, `ORGANIC_RUNS_PATH`, `_normalize_format`, `_sku_key`, `_sku_display`,
  `build_display_name` are imported into `app.py` *from helpers*, so in the blueprint
  they're imported **directly from `helpers`** (suppliers.py pattern), NOT
  `app.`-qualified. Lesson for next steps: classify each external name by its true
  home (helpers → direct import; app.py → `app.`-qualify) — don't trust that a name
  visible in `app.py`'s namespace is defined there. Also `app.static_folder` (Flask
  instance attr) became `app.app.static_folder` (module → instance). `PHOTOS_DIR` +
  `serve_photo` stayed in `app.py`; sliced around them per-function.
- ✅ **`sales.py`** (commit `38a6b1d`) — 7 organic-sales routes (get/add/add-order/
  edit/delete + packing-slip + qbo-csv). Lower-risk than recipes: per-name home
  classification gave only **3** `app.`-qualifications (`ORGANIC_FG_PATH`,
  `ORGANIC_SALES_PATH`, `_load_buyers`) — the FG/FIFO deduction in the add routes is
  inline on the FG JSON, not a shared helper, so no cascade. Sliced around the
  interleaved buyer helpers (`_load_buyers`/`_save_buyers`/`_buyer_resolver`/
  `_all_sku_catalog`/`BUYERS_PATH`) and the startup-called `_migrate_legacy_sales`,
  all of which stay in `app.py`. Smoke verified FIFO deduction across lots, multi-line
  orders, and precise per-fg_id restore on delete.
- ✅ **`finished_goods.py`** (commit `325d17d`) — first slice of the **inventory**
  domain. 10 FG routes (/api/organic/finished-goods*: list/update/delete, lot-adjust,
  baseline +bulk, manual add/subtract, grouped, sku-detail). Only 5 `app.`-qualifications:
  `ORGANIC_FG_PATH`, `SKU_META_PATH`, `load_recipes`, and the grouping helpers
  `_group_fg_by_sku` / `_group_fg_with_catalog` (sku-meta + other routes still call them,
  so they stay in app.py). Touches **no** consumption-chain code. Inventory was scoped
  down from one ~44-route move to per-sub-domain slices (decided with the user); FG was
  the cleanest cut. Confirmed `/grouped` + `/sku/<key>` still resolve alongside the
  generic `/<fg_id>` route — Werkzeug ranks by rule specificity, not definition order, so
  moving routes into the blueprint can't change matching precedence.
- ✅ **`raw_materials.py`** (commit `a1bd754`) — second inventory slice. 21 RM routes
  (CRUD incl. bulk, ingredients, sections/assignments, by-ingredient lots, invoices,
  receipt photos), sliced across three islands. **PURE routes-move** — every helper +
  constant stays in app.py (19 `app.`-qualifications). **Deviated from the earlier
  plan note:** the 3 RM-private invoice helpers (`_parse_lots_field`,
  `_remove_invoice_file`, `_save_invoice_file_bytes`) + `_invoice_mime` were NOT moved
  — the invoice-helper cluster is internally self-referential, so leaving it together
  in app.py was safer and consistent with prior slices. **Risk was lower than feared:**
  the RM routes touch NO consumption-chain helpers; the only audit hook is
  `delete_raw_material`'s 409 guard via `helpers._runs_using_raw_material`. Smoke
  exercised CRUD, MAN-/BL- lot stamping, the bulk catalog-validation guard, and the
  409 delete guard (consumed lot blocked + reported + not deleted).
- ✅ **`audit_tools.py`** (commit `a29c153`) — third inventory slice, the highest-risk
  (audit-critical). 6 routes: reconcile-raw (page + run preview/apply), organic trace,
  stock-exceptions, mass-balance (api + page). Pure routes-move, 8 `app.`-qualifications.
  Left the audit engines `_rebuild_raw_material_consumption`, `_compute_mass_balance`,
  `_sale_touches_fg` in app.py (private now, but they carry consumption-chain invariants
  — safest not to relocate). Smoke exercised reconcile PREVIEW (no writes) + APPLY
  (date-aware replay recomputed remaining 100−10=90, runs_replayed=1, manual-adj lot
  surfaced), mass-balance, raw_lot trace chain, stock-exceptions. **Lesson:** the
  raw_lot trace response keys its chain under `lots`, not `results` (only the empty-query
  early-return uses `results`); the reconcile replay skips runs with `amount_produced<=0`.
- ✅ **`production.py`** (commit `a18c673`) — first production slice (schedules + tracker,
  10 read/schedule routes, ~183 lines). Pure routes-move, 12 `app.`-qualifications.
  **NEW pattern wrinkle (important for the remaining production steps):** decorators are
  applied at blueprint-IMPORT time, before app.py finishes defining them, so
  `require_valid_week` / `require_valid_day` (like `login_required`) must be LOCAL
  verbatim copies in the blueprint — they delegate to `app.validate_week_id` /
  `app.validate_day_idx` at REQUEST time (validators stay in app.py). app.-qualifying a
  decorator (`@app.require_valid_week`) would crash at import. Smoke confirmed the local
  `require_valid_week` returns 400 on a bad week.
- ✅ **daily-production + checklists** (commit `f2595ef`) — second production slice,
  APPENDED to `production_bp` (8 routes, ~191 lines). The HIGH-RISK consumption-chain
  slice: `save_daily_production` → `_check_organic_completion` → `_complete_organic_run`
  → `_deduct_run_ingredients` (all kept in app.py, 10 app.-qualifications). Added a local
  `require_valid_day` copy; `app.static_folder`→`app.app.static_folder` in complete_checklist;
  `generate_filled_checklist_pdf` from pdf_engine. Smoke drove the FULL chain: day-1 save
  finished a day-0 organic run → raw deducted per-batch (50→40kg), FG created (qty 100,
  cert carried), run completed with ingredients_used snapshot. **Lesson:** appending to an
  existing blueprint works cleanly (build script edits the bp file's header + appends route
  chunks, slices routes out of app.py, no new register_blueprint).
- ✅ **traceability / completed-records** (commit `8f1986f`) — third/final production slice,
  APPENDED to `production_bp` (6 routes, ~280 lines). Highest-risk slice:
  `delete_traceability_record`'s 98-line cascade. 14 app.-qualifications (signoff helpers
  `_load_weekly_signoffs`/`_save_weekly_signoffs`/`_week_completion_state`, `list_schedules`,
  `load_schedule/checklist/recipes`, `CHECKLISTS_DIR`/`PDF_DIR`, `ORGANIC_FG/RAW/SALES_PATH`,
  `DAYS`, `VESSELS`) — all stay in app.py. No header changes (production.py already imported
  everything). Smoke drove the cascade BOTH ways: (a) 409 refusal when FG sold (nothing
  mutated), (b) successful delete restoring raw (40→50kg), removing FG, resetting run to
  scheduled + clearing finish coords, deleting checklist, 404 on re-delete; plus sign-off/unsign.
  **The production domain is now fully extracted.**

**Two reusable blueprint patterns are now established** (both define a local verbatim
`login_required` to avoid a circular import, matching `ripe_orders.py`):
1. **Self-contained** — when a domain owns its data file and helpers (suppliers).
2. **Routes-move, helpers-stay** — when helpers are shared cross-domain (buyers).
   Blueprint does a bare `import app` at module top (binds the partially-initialised
   module safely; attributes resolve only at request time) and calls `app.X(...)`.
`ripe_orders.py` was untouched and still resolves its lazy `from app import` calls
because `app.py` re-exports the moved names.

**Inventory is being split per sub-domain, not as one blueprint** (decided 2026-06-03 —
it was ~44 routes / ~1385 lines straddling audit-critical code). Done: `finished_goods.py`
+ `raw_materials.py` + `audit_tools.py` (✅ all above). The audit-critical slice is now
behind us. Only a small low-risk inventory tail remains (sku-meta + inventory pages +
`internal_fg_stock`) — see "Small inventory tail still pending" below.

**Production domain is FULLY extracted** (sliced like inventory, 3 verified steps —
schedules+tracker, daily-production+checklists, traceability/completed-records, ✅ all
above; one `production_bp` in `production.py`). The audit-critical consumption chain and
deletion cascade are now behind a blueprint, with all the audit helpers still in app.py.

**NEXT (recommended): the small inventory/misc tail.** This is the only planned blueprint
work left and it is all LOW-RISK:
- sku-meta (`update_sku_meta`, `get_all_sku_meta`) + inventory pages (`organic_page`,
  `organic_certification_page`) + `internal_fg_stock`.
- production-runs / contacts (`get_organic_runs`, `get_organic_contacts`) — trivial.
Decide with the user whether these are worth their own blueprint(s) or simply left in
app.py as part of the core — they're small, low-churn, and not obviously a cohesive
domain. Apply the same proven method if extracted (re-run line ranges live; classify each
name by TRUE home; pure routes-move; byte-identical 166-route map; free-var audit; smoke).
Assume shared helpers STAY in `app.py`.

Everything beyond that (dashboard, auth, channel imports, analytics, equipment,
company-info, certifications, CCP, audits, important-documents, Ripe internal endpoints,
PWA, and all the shared helpers reached via `import app`) is the INTENDED app.py core —
the split was never meant to dissolve app.py entirely.

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

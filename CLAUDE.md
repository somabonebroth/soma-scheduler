# Soma + Ripe — Codebase Context

## What this is

Two Flask apps deployed on Render:

- **Soma** (`soma-scheduler.onrender.com`) — internal production management for a bone broth company. Handles weekly production scheduling, organic inventory (raw materials + finished goods), sales records, recipes, traceability, checklists, equipment maintenance, and buyer/supplier management.
- **Ripe** — wholesale buyer portal. Buyers log in, view their SKU catalogue and pricing, place orders. Connects to Soma via an internal API authenticated with `INTERNAL_API_KEY`.

---

## Repository structure

```
app.py              — 6796 lines, 145 routes, 236 functions. Everything lives here.
ripe_orders.py      — Flask Blueprint (591 lines) handling Ripe order workflow within Soma
pdf_engine.py       — PDF generation (labels, checklists, schedules)
vision_scan.py      — Receipt photo OCR
default_recipes.py  — Seed data
add_pwa_tags.py     — PWA manifest support
supplier_routes.py  — Placeholder (routes are in app.py)
templates/          — Jinja2 HTML templates (one per page)
static/             — CSS, JS, images
```

No `helpers.py`, `cogs.py`, or `equipment.py` in this baseline — everything is in `app.py`.

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

---

## Environment variables

**Soma:**
- `DATA_DIR=/opt/render/project/data`
- `SECRET_KEY`
- `APP_PASSWORD`
- `MANAGER_PASSWORD`
- `INTERNAL_API_KEY`
- `RIPE_PORTAL_URL`

**Ripe:**
- `DATA_DIR`, `SECRET_KEY`, `RIPE_PASSWORD`, `INTERNAL_API_KEY`, `SOMA_APP_URL`
- `SMTP_USER`, `SMTP_PASS`, `RIPE_CONTACT_EMAIL`, `SOMA_NOTIFY_EMAIL`, `SOMA_ETRANSFER_EMAIL`
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RIPE_BILLING_EMAIL`

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

# Soma + Ripe — Codebase Context

## What this is

Two Flask apps deployed on Render:

- **Soma** (`soma-scheduler.onrender.com`) — internal production management for a bone broth company. Handles weekly production scheduling, organic inventory (raw materials + finished goods), sales records, recipes, traceability, checklists, equipment maintenance, and buyer/supplier management.
- **Ripe** — wholesale buyer portal. Buyers log in, view their SKU catalogue and pricing, place orders. Connects to Soma via an internal API authenticated with `INTERNAL_API_KEY`.

---

## Repository structure

```
app.py              — ~4688 lines, 166 routes. The core. The blueprint split is
                      COMPLETE (see "Pending architectural work") — what remains here
                      is the intended core (dashboard, auth, channel imports, analytics,
                      equipment, company-info, certifications, CCP, audits, Ripe internal
                      endpoints, PWA) + the shared helpers every blueprint reaches via
                      `import app`.
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
                      from helpers. PHOTOS_DIR + serve_photo stay in app.py. **Rename-cascade constraint (audited 2026-08-19):** `update_recipe`'s cascade covers FG, sales, runs, sku_meta, buyers, schedule files, + the Ripe notify. It deliberately does NOT touch `inventory_events.json` or `adjustments.json`, which both store a `sku_key` — that is safe ONLY because every consumer joins on `fg_id`: `compute_fg_reconciliation` recomputes the key live from FG entries (which the cascade does update), and `project_fg`'s `by_sku` half is discarded by its only caller (`proj_fg, _ = project_fg(events)`). Those stored keys DO go stale on a rename. If anything ever starts joining on them, the cascade must grow to match. Checklists are unaffected — `produced` is keyed by VESSEL, and the recipe comes from the schedule, which is cascaded.
sales.py            — Flask Blueprint (731 lines, extracted 2026-06-03): the 7
                      organic-sales routes (/api/organic/sales*: get/add/add-order/
                      edit/delete + packing-slip + qbo-csv). Buyer helpers + the
                      ORGANIC_FG_PATH/ORGANIC_SALES_PATH constants stay in app.py
                      (app.-qualified); foundation IO from helpers. Channel imports,
                      sales analytics, and trace deliberately NOT included.
finished_goods.py   — Flask Blueprint (542 lines, extracted 2026-06-03): the 10
                      finished-goods routes (/api/organic/finished-goods*) + the
                      inventory tail folded in later: sku-meta (update_sku_meta,
                      get_all_sku_meta) and the two inventory page shells (organic_page,
                      organic_certification_page). FG grouping helpers
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
production.py       — Flask Blueprint (771 lines, extracted 2026-06-03): the FULL production
                      domain (+ get_organic_runs production-runs read). Built over 3 slices.
                      (1) schedules (create/weekly pages,
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
ripe_orders.py      — Flask Blueprint handling Ripe order workflow within Soma
                      (wholesale approve/decline/fulfill + ripe_retail_auto_approve for
                      Stripe-Checkout-paid retail pickup orders + monthly service-fee
                      e-transfer confirmation, proxied to the Ripe portal)
retail_orders.py    — Flask Blueprint (added 2026-07-02): SBBC Wholesale Portal order
                      ingestion, mirroring ripe_orders.py (which shares a live contract
                      with Ripe and stays untouched). /retail-orders admin page +
                      PATCH /api/retail-orders/<id>. Orders arrive ALREADY PAID (Stripe
                      Checkout on the portal): approve deducts FG (sales._deduct_fifo,
                      all-or-nothing, idempotency-guarded on retail_order_id, and REFUSES
                      organic-certified SKUs — two-tier boundary enforced for this
                      channel); decline proxies to the portal, which issues a full Stripe
                      refund. NO auto-approve. Env: RETAIL_PORTAL_URL + INTERNAL_API_KEY.
                      Portal repo: github.com/somabonebroth/SBBC-Wholesale-Portal.
end_of_day.py       — Flask Blueprint (added 2026-08-19): the floor's one end-of-shift
                      flow at /end-of-day. Owns NO data — it joins production
                      (file_checklist) and cleaning (closing record, rotation) and is
                      the ONLY place a production day is now signed off. See "End of
                      Day" below.
ledger.py           — Flask Blueprint: inventory event-ledger subsystem (added
                      2026-06-09). Read-only FG reconciliation/drift detector
                      (/admin/fg-reconcile), append-only event model + projection
                      + verify, the two-tier zero-day reset (/admin/fg-reset) with
                      run-freeze + archive restore. See "Inventory ledger & two-tier
                      organic traceability" below. Owns inventory_events.json +
                      ledger_archive/; foundation IO from helpers, app.-qualified
                      shared paths via `import app`.
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
templates/          — Jinja2 HTML templates (one per page)
static/             — CSS, JS, images
```

`helpers.py` now exists (foundation layer, 2026-06-03). No `cogs.py` or `equipment.py` — that code is still in `app.py`. The dead `supplier_routes.py` placeholder was deleted (2026-06-03, commit after the split); suppliers live in `suppliers.py`.

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

**Product photos → portals:** `/api/internal/catalogue` includes each SKU's recipe `photo` filename (from recipes.json, uploaded via the Recipes page); portals fetch the bytes from the key-gated `GET /api/internal/photo/<filename>` and proxy them to their buyers (added 2026-07-02 for the SBBC portal storefront).

**Daily records: two documents, deliberately separate (2026-08-18).** The daily production
checklist is the CCP/HACCP record (`ccp_master.json`, see below). The **closing checklist**
is housekeeping and lives in `cleaning.py` — NOT in the CCP master. Merging them would
dilute a controlled document and would make a forgotten mop raise a CCP alarm now that CCP
flags actually fire. `cleaning.py` therefore holds two contracts, documented in its module
docstring: the closing **gate** (fixed items, all required, per-item ticks, one signature,
one record per date, works on non-production days) and the rotating **backlog** (skipping
explicitly fine). Closing records SNAPSHOT the labels signed against; item ids survive
edits. Data lives in `cleaning_jobs.json` as `closing_items` + `closing_records`.

**Jars are completed the day AFTER they are scheduled — one model, audited 2026-08-18.**
A batch STARTED on day N is counted on day N+1 (seal check is next-day, per the CCP
checklist). The tablet encodes this: `get_daily_production` splits a START side (today's
recipe) from a FINISH side (yesterday's), and "Amount Produced" sits on the FINISH side.
Everything downstream must credit those jars to the PREVIOUS day's recipe —
`_check_organic_completion`/`_complete_organic_run` (`_previous_day_coords`), the production
tracker + its "Other" diagnostic, the boot backfill, mass balance ("raw consumed = the run's
PRODUCTION (start) date; FG produced = finish date"), and the FG LOT# (= production date +
365, matching the case label). `summarize_day` and `get_traceability` used the SAME day's
schedule until 2026-08-18 and were fixed; use `app._get_previous_day_schedule(week_id,
day_idx)` for anything that reads `checklist["produced"]`. `summarize_day` returns BOTH
`scheduled` (started that day) and `finished_recipes` (counted that day) so neither reading
stands in for the other. Completed-production records tag certification for the jars
COUNTED that day (from the FG entries the run wrote; prev-day schedule as fallback), so a
day where a batch started but nothing finished correctly shows no cert tags.

**End of Day (`end_of_day.py` + `templates/end_of_day.html`, 2026-08-19).** The floor's ONE
end-of-shift flow, behind a single full-width dashboard button (it replaced the separate
Closing Checklist / Cleaning & Upkeep buttons). Steps appear one at a time: (1) the day's
production read back — jars counted today, named for the batch that STARTED yesterday —
plus every CCP section, tickable in place, then signed; (2) a note for management; (3) the
closing checklist ONE ITEM AT A TIME in the manager's order, then signed; (4) one rotating
job or an explicit decline; (5) a done card. `GET /api/end-of-day` returns the whole flow
in one read and reports what is already done, so a re-opened flow RESUMES rather than
asking twice.

**Step 1 IS the checklist sign-off — the tablet no longer files the day.** The Sign &
Complete button and the `signoff-kitchen` field are gone from `daily_production.html`; the
tablet only captures numbers and ticks as they happen (still autosaving). Filing moved to
`production.file_checklist(week_id, day_idx, data)`, extracted from `complete_checklist` so
the route and the wizard can only file a day one way (PDF + consumption chain + warnings).
`summarize_day` gained `require_completed=False` so the wizard previews the day it is about
to file with the SAME read the weekly review and daily brief use. `production._preserve_filing`
carries `completed`/`completed_at`/`signoff_kitchen` through a later tablet autosave —
`save_checklist_data` replaces the file wholesale, so going back to fix a jar count would
otherwise silently un-file a signed day.

**The note and the decline live on the cleaning record, not a new file.**
`POST /api/cleaning/closing/note` upserts `manager_note` on the day's closing record (it is
written BEFORE the list is signed, hence the upsert) — one home, and a non-production day
can still carry a note. The brief exposes it as `handover_note` and `daily_review.html`
renders it ABOVE section 1 as "Note from the floor". `POST /api/cleaning/rotation/decline`
records a dateless-reason decline in `declines[]`; it surfaces as a plain NOTE ("Rotating
job declined"), never an issue — skipping carries no penalty, management only gets to see
how often it happens.

**Rotating jobs are grouped by slot** (`cleaning.slot_for`: weekly ≤7 / monthly ≤30 /
quarterly ≤90 / semi-annual beyond; `_job_view` carries `slot`). Weekly was added 2026-08-19.
Both the cleaning page and the wizard render slot headings with colour-coded pills — an
ungrouped list made an hour-a-week job look identical to a six-month one. The closing list
is re-orderable by the manager (↑/↓ in "Manage closing list"); order matters because the
wizard walks the floor through the items in exactly that sequence, so it should read as a
walk around the kitchen. The PUT already stored the list in order — only the UI was missing.

**Management Report (`daily_brief.py` + `templates/daily_review.html`, 2026-08-18;
renamed from "Daily Review" 2026-08-19).** A full page at `/daily-review` (route and API
names unchanged — the rename is titles only) behind the full-width dashboard button
labelled "Start Here → Management Report", fed by `GET /api/daily-brief?date=` (default
yesterday). Every note the floor left is gathered ABOVE the sections (`staffNotesHtml`):
the End of Day handover keeps its amber block, the rest — day note, per-vessel finish
notes, closing/cleaning notes — sit in a "Notes from staff" card beneath it. Then THREE
sections: (1) What's getting labelled today — per-product rows with vessel/LOT#/cert/jars
+ totals; (2) Sales & Receiving from yesterday (the heading says "yesterday" only when the
viewed day IS yesterday, else the day's label — the page navigates back) — sales by buyer
with lots/channel, deliveries with supplier/lot; (3) Completed checklists and cleaning —
CCP sections as ✓/✗ with the confirmed count, the closing gate (who signed, what was
missed), and which rotating job was done. Sections carry their own ISSUES; notes are
top-only, so `notesHtml` is called from `staffNotesHtml` alone.
**Section 1 reads FINISHED GOODS, not the checklist `produced` map** — jar counts entered on
day N complete runs STARTED on day N-1, so `produced` against the same day's schedule can
name the wrong recipe; FG rows were written by the run itself. Falls back to checklist
numbers for pre-runs days (`source` says which). The dashboard button is highlighted until
the day is reviewed AND no earlier day is outstanding. Built because an audit found
the system captures well and reports late — day notes reached the HOO up to a week later and
per-vessel `finish_notes` had **no reader anywhere**. It creates NO artifacts: everything is
re-read from existing records. Per-domain summaries stay with their domain
(`production.summarize_day`, `cleaning.day_summary`) and the brief only joins them.
**`summarize_day` was extracted from `get_week_summary`, which now calls it** — the weekly
review and the daily brief must never drift in how they read a day. A missing closing
sign-off is only flagged on a day the kitchen is known to have run (completed production or
a cleaning sign-off), so quiet days stay quiet.

**Management Report — labelling + channel/portal sales (added 2026-08-19).** Section 1 shows the
day's LOT# prominently plus the **hot-stamp guide**: the stamp is set by hand face-down, so
the type is drawn REVERSED and MIRRORED (CSS `scaleX(-1)` per character) with slot numbers
and a mirror check. `_lot_blocks` reads the lots off the **FG rows**, never recomputed from
the date, so the panel and the inventory record can't disagree; a day whose jars came from
batches started on different dates gets one block per lot. Each product row has a Generate
Label button → `/api/label` with the FG lot + the BATCH (start) date, so printed Best
Before = lot. **Note the tablet disagrees:** `daily_production.html`'s finish-side button
sends `productionData.lot` (finish+365) while FG stamps start+365, and `production.py`'s
`prev_lot`/`prev_date` — computed for exactly this — have no reader. Unresolved; fix on the
tablet, not by bending the review.
Section 2 gained **Portal orders** (Ripe + Wholesale Portal sale rows regrouped by order,
keyed on `deducted_at` — the day stock actually left — NOT `sale_date`, which for wholesale
is a future delivery date) and **Retail channels**: Shopify + Clover read LIVE for the date
via new `preview_day()` in both importers, served by `GET /api/daily-brief/channels?date=`
and loaded AFTER the page renders. Live because the cron import runs weekly, so sales.json
holds nothing for yesterday six mornings in seven; **read-only** — the weekly import is
still the only path that writes sales and deducts FG, so it cannot double-count. Kept out
of `/api/daily-brief` on purpose: `pending_reviews` builds up to 60 briefs and must never
make network calls. An unconfigured/failing channel degrades to a status line. Both
importers now share `_classify_skus` between `preview_week` and `preview_day` (the weekly
commit and the daily report can never disagree about a SKU) and sum gross line revenue
(Shopify nets line discounts; Clover's discount objects are deliberately not netted).

**Review is DAILY, not weekly (changed 2026-08-18).** The HOO signs each day off on the
morning brief: `POST/DELETE /api/daily-signoff/<date>` → `daily_signoffs.json` keyed by
date, snapshotting `open_actions` at signing time. Signing is deliberately NOT gated on the
day being clean. Only days the kitchen RAN are reviewable (`daily_brief._kitchen_ran`:
completed production or a cleaning sign-off), so quiet days need no review.
`GET /api/daily-signoffs/pending` lists unreviewed days oldest-first and drives both the
brief's catch-up line and the dashboard's Completed Production badge.
**The weekly sign-off is RETIRED, not deleted** — `sign_off_week`/`unsign_week` are gone so
nothing writes a new one, but `_load_weekly_signoffs` still reads and pre-cutover weeks
render their historical confirmation. **Watch out:** the weekly sign-off silently gated the
Delete button on day records; the daily sign-off inherited that lock at finer grain
(`get_traceability` returns each day's `signoff`; Delete hides on a reviewed DAY). That was
UI-ONLY until 2026-08-19 — `delete_traceability_record` had NO sign-off check, so a reviewed
day could still be deleted, cascade and all, by calling the endpoint directly. It now refuses
with 409 on either a daily sign-off (keyed by `app._run_start_date_str(week_id, day_idx)`) or
a historical weekly one (keyed by `week_id`) — the SAME keys `get_traceability` uses, so the
guard and the hidden button can never disagree. Un-sign the day on the Management Report to
delete. If review ever changes shape again, re-check BOTH the button and the guard.

**Daily CCP checklist — `ccp_master.json` is the SINGLE source of truth (fixed 2026-08-18).**
The production tablet renders its section ticks from the CCP master (manager-editable at
`/ccp-master`), and since this fix BOTH PDF paths do too — `generate_filled_checklist_pdf`
(completed record) and `generate_daily_package_pdf` → `draw_checklist_pages` (the blank
checklist inside the daily package) take a `sections=` argument, and the two callers
(`production.complete_checklist`, `app.generate_pdfs`) pass `load_ccp_master()`.
`pdf_engine.CHECKLIST_SECTIONS` — a hardcoded second copy that had drifted to 8 sections
against the master's 5, so every completed PDF printed three sections the tablet cannot
tick and mislabelled section 5 — was DELETED. **Never reintroduce a local copy of the
checklist in `pdf_engine`.** Consequence (deliberate): editing the CCP master changes the
signed PDF, so new sections (e.g. a closing checklist) need no deploy. The `"---"` callout
row is gone — the master has no way to express it.
Confirmations are stored per SECTION, not per item: the tablet writes
`checklist["checks"]["section-<num>"] = bool`. `get_week_summary` reads exactly that (it
used to read a `sections` key nothing writes, so **CCP flags could never fire** and the
HOO's "all clear" was only checking sign-offs and notes).

**FIFO deduction:** `_run_scheduled_deductions()` runs at startup — auto-deducts Ripe sale records from FG inventory when `deduction_date <= today`.

**Sales channels:** Sale records optionally have a `channel` field (`'shopify'`, `'clover'`, or absent for legacy/manual). Channel-imported sales also carry `week_id`, `source_order_ids` (list of upstream Shopify/Clover order IDs for traceability), and a deterministic `order_id` of the form `ORD-{CHANNEL}-{week_id}` so multi-SKU imports group as one transaction in Soma's UI (matching the existing `add_sale_order` order_id grouping). Since 2026-08-19 they also carry `line_total` (the revenue the CHANNEL reported, taken from the importer's `matched["revenue"]`) and a derived `unit_price` — read the line from `line_total`, never `unit_price × quantity`. Before that fix neither field was written, so every Shopify/Clover sale counted as $0 revenue in `api_sales_by_buyer`; there is no buyer catalogue for `SOMA (Shopify)`/`SOMA (Clover)` (`_add_contact` only registers the name in organic_contacts.json), so the channel is the sole source of these retail prices. Clover's figure reads HIGH on discounted items — its discount objects are deliberately not netted. Weeks imported before the fix keep their $0 until repaired via `/admin/channel-prices` (price-only: writes unit_price/line_total and NOTHING else — no stock, no new sales; leaves any row the channel can't confirm untouched rather than zeroing it).

**Channel reconciliation (`/admin/channel-prices/reconcile`, read-only, 2026-08-19).** Two-way diff of one channel-week: what the channel sold vs what Soma recorded. Needed because the price repair only walks Soma's EXISTING rows, so it can't see a SKU the channel sold that never became a sale — and four importer paths cause exactly that: a line item whose SKU was empty at order-creation (Shopify snapshots `line_item.sku`), a SKU skipped at commit for insufficient FG (`errors[]`, cron-log only), a week the commit refused wholesale because some SKU was unparseable, and non-`SOMA-` items (by design). Reports per-SKU status (ok / units_differ / missing_in_soma / extra_in_soma), unit and revenue gaps, and the `skipped_no_sku` + `skipped_other_brands` buckets — both importers now carry `revenue` on `skipped_no_sku` entries so the invisible money is quantified. The revenue gap is suppressed (None) while any Soma row is still unpriced, since it would be meaningless. Works on a week Soma has NO rows for, which the price repair 404s on — that is the failed-import case.

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
- `GET /api/organic/stock-exceptions` — completed batches made with insufficient raw material (the `INSUFFICIENT_STOCK` markers). The banner on Completed Production was REMOVED 2026-06-10 (commit `9d60cf3`): these are frozen historical markers, not a live check, so it never cleared and read as a standing error. The all-time list now lives in a collapsed "Stock Exceptions" card on **Organic Certification** (loaded on demand, framed as a record you consult — do NOT reinstate it as a banner). Per-DAY, the same markers surface as an issue on the Management Report via `daily_brief._stock_exceptions_section`.
- `GET /api/organic/mass-balance?from=&to=&organic_only=` + page `/mass-balance` (`_compute_mass_balance`): Opening+Received−Consumed=Expected vs current stock (raw), Opening+Produced−Sold=Expected vs current (FG). Discrepancy = adjustments/loss/breakage, exact when `to`=today. Client-side CSV export. Linked from the Inventory and Organic Certification pages and the dashboard (NOT from Completed Production, despite an earlier note here).
- `get_traceability` reports certification **per vessel** (`cert_by_vessel`, `certifications[]`) — a day can run mixed certs; never collapse to one label. The All/Organic/Non-Organic filter tabs were **removed** (2026-06-03): `get_traceability` never honored the `?filter=` param, and the per-vessel cert tags already convey cert status, so the tabs were dead UI. The route still harmlessly ignores a stray `?filter=`.

**Receipt photos:** one per delivery, stored as `<entry_id>.<ext>` in `rm_receipt_photos/`, anchored to the first entry of a bulk save. `GET /api/organic/raw-materials/receipt-photos` lists which entry ids have one; the Receiving list shows a "📎 Invoice" button per delivery.

**Two pages, confusingly named:** "Manage Inventory" = `templates/organic.html` (3 tabs since 2026-08-18: Raw Materials / Production Runs / Finished Goods; deep-linkable via `?tab=raw|runs|fg`, `showTab()` keeps the URL in step, buttons carry `data-tab`, highlight is by name not position; `?tab=records` redirects to `/sales-receiving`). The former Records tab (Search & Trace / Receiving / Sales) is now its own page, "Sales & Receiving Record" = `templates/sales_receiving.html` at `/sales-receiving` (route in `sales.py`). **Date-windowed since 2026-08-19:** the page defaults to the last 90 days (presets 30d/90d/1y/all + a from/to picker) and passes the window to the SERVER as `?from=`/`?to=` on `/api/organic/sales` and `/api/organic/raw-materials`. Both params are optional and omitting them returns EVERYTHING, so every other caller (organic.html, organic_certification.html) is unaffected; they compose with the existing `?certification=`. Filtering is server-side ON PURPOSE — doing it in the browser would still ship the whole table, and these params are the same predicate SQL takes, so moving these files to a database later changes the function bodies and not the request URLs. Shared predicate is `helpers._in_date_window` (inclusive bounds, lexicographic ISO compare, and it KEEPS undated rows — dropping them would silently hide records). Sales window on `sale_date` (fallback `created_at`), NOT `deducted_at`; raw materials on `date_received`. Search & Trace deliberately ignores the window — a trace must reach any record. Measured at ~2 years of volume: 443.9 KB -> 55.3 KB per page load. The dead Suppliers/Buyers/Settings panes and legacy top-form JS were deleted the same day (organic.html 5908 → 4502 lines). "Completed Production" = `templates/traceability.html` (the page formerly called Traceability; week records, HOO sign-off, stock exceptions, per-vessel certs). Two more standalone tool pages: `mass_balance.html` and `reconcile_raw.html` (both link the shared `static/style.css`).

**Organic Certification (`/organic-certification`) is the audit workspace — keep BOTH sides in it (2026-08-19).** Its "before an audit" flow was raw-only: step 1 Reconcile Raw, step 2 Mass Balance, step 3 `/audit/rm`. The finished-goods counterparts both existed and neither was on the page — `/admin/fg-reconcile` was linked only from the dashboard, and `/audit/fg` (a fully working audit kind; `audit_page` accepts `rm` and `fg`) was linked only from organic.html. Steps 1 and 3 now each carry a raw tile and an FG tile, step 2 spans both. FG is the CERTIFIED product that leaves the building, so a raw-only audit proves half the chain — do not let the FG side drift back off this page. `/audits` is the count HISTORY and is now labelled "Stock Count History" everywhere (it is linked from both the Inventory and Organic Certification dashboard rows — one destination, one label). The zero-day FG reset (`/admin/fg-reset`) is deliberately reachable ONLY from the FG Reconcile page header, one level behind the read-only diagnostic — it is the most destructive tool in the system; keep it off the dashboard.

**Known duplication (NOT fixed):** Search & Trace is implemented twice — independently on `sales_receiving.html` and `organic_certification.html`, each with its own `doTrace`/`doTraceDebounced` and its own copy of `escSku`/`escHtml`. Both hit `/api/organic/trace`. Edit one and the other silently drifts.

---

## Inventory ledger & two-tier organic traceability (`ledger.py`, 2026-06-09)

A subsystem built to fix accumulating FG inventory drift and lock in organic
traceability. Full history in the memory note `project_inventory_event_ledger`.

**Why FG drifts:** sales are entered **SKU-accurate but NOT lot-accurate** (the
warehouse can't pick true-FIFO, so the lot recorded on a sale is a FIFO *guess*; the
real lot is only on the physical case). So per-lot FG *balances* are fiction; only SKU
totals are trustworthy. Plus several paths mutate FG with no ledger record
(`update_finished_good`, `adjust_lot_remaining`, in-place `edit_organic_sale`).

**The two-tier model (keyed on `certification == "Organic"`):**
- **Organic-certified SKUs** → full lot tracking. They are **wholesale-only and never
  flow through Ripe or the retail channels** — only the manual sale entry. The Record
  Sale modal (`organic.html`) shows each organic SKU's available LOTs with +/- steppers;
  the packer allocates the units actually packed; the sale carries `allocated_lots` and
  FG deducts **those exact lots** (`sales._deduct_allocation`), not FIFO. Forward trace is
  real and prints on the packing slip / QBO CSV / Records / trace (all already lot-aware).
- **Everything else** → SKU-level books, backward trace only.

**Read-only tools (`ledger.py`):**
- `compute_fg_reconciliation()` + `/admin/fg-reconcile` — per-fg_id drift detector:
  Expected = produced − recorded sales − recorded manual subtracts, vs actual remaining;
  flags cause (manual_adjust / baseline_drift / unexplained), orphan refs, edited-sale
  gaps, pending Ripe. SKU rollup carries `tier` (organic = judge per-lot; sku = judge at
  SKU total, per-lot split is FIFO noise). Catches SKU-internal offsetting errors the
  mass balance hides.
- Event model: append-only `inventory_events.json`, `backfill_fg_events()`,
  `project_fg()`, `verify_fg_projection()` + a self-check banner.

**Zero-day reset (`/admin/fg-reset`, `apply_reset` — the only WRITE path here):**
counts at two-tier grain (organic per LOT, others one SKU total), archives the 5
inventory files to `inventory/ledger_archive/*_<stamp>.json`, replaces FG with clean
`reset_baseline` entries, writes a RESET event + opening events with a **cutover**.
Reconciliation/backfill are cutover-aware (skip pre-cutover sales/adjustments).
The **mass-balance FG side** is likewise cutover-aware (2026-06-10): it folds
`reset_baseline` entries into Opening (like `migration_baseline`) and skips
pre-cutover sales/adjustments — otherwise, post-reset, every organic SKU read
as baseline + every old sale of bogus unexplained discrepancy. Raw side is
untouched (raw is never reset). Reconcile-raw and the trace endpoints needed no
change: raw lots + runs are never wiped, and reset baselines deliberately carry
no `run_id` (pre-cutover production linkage lives in `ledger_archive/`).
The mass-balance FG "sold" column + cutover skip key off the date stock actually
LEFT FG — `deducted_at` (Ripe deducts at APPROVAL) else `sale_date` else
`created_at` — NOT a Ripe order's `sale_date`, which is its (often future)
DELIVERY date (2026-06-10). Keying on sale_date double-counted a pre-reset order
delivered after the cutover (phantom +discrepancy = the order) and dropped a
just-deducted order delivered beyond `to` (phantom −discrepancy). Manual sales
carry no `deducted_at` → fall back to sale_date, so the manual path is unchanged.
**CRITICAL — run-freeze:** FG is *regenerated from completed production runs* by
`_complete_organic_run` (fires on the daily-production save AND on
`_backfill_organic_finished_goods` at every boot). A naive replace-FG reset let those
runs re-materialise old FG ON TOP of the baselines → inventory doubled (the 2026-06-09
incident). Fix: `apply_reset` snapshots `frozen_run_ids` (run ids `status=="completed"`
as of cutover) into the RESET event meta; `_complete_organic_run` fetches
`ledger._reset_frozen_run_ids()` (lazy `import ledger`, degrades to `set()` on error so a
save/boot never crashes) and **skips any frozen run** — no FG regen, no raw touch. New
runs and runs still `scheduled` at cutover are NOT frozen, so real production flows.
Freeze by run-id, NOT `completed_at` (a first-time post-reset completion has stale/empty
`completed_at` at guard time). **Undo:** the "↩ Undo a reset" panel restores
finished_goods+events from a snapshot (`restore_reset_archive`, re-archives current as
`pre_restore_*` first).

**FG LOT# = production(start) date + 365** (`_complete_organic_run`), matching the case
label which `/api/label` prints as production+365. (Was finish+365, which put the system
one day ahead of the case on any batch produced one day / packaged the next.)

**Known caveat (R3, unfixed):** `delete_traceability_record` on a *pre-cutover* (frozen)
run restores raw against current lots while the baseline stands → raw inflation. Avoid
deleting pre-cutover production days; harden later if needed. RAW materials have no
reset (only the existing reconcile-raw tool).

---

## FG mutation-surface audit (2026-06-09)

Audited every code path that adds/subtracts FG inventory (20 write sites across 8
modules). The ADD side (production completion, reset, backfill, rename cascade) is
sound and the 2026-06-09 doubling vector is genuinely closed (frozen-run guard fires
in `_complete_organic_run` for both live-save and boot-backfill). Three SUBTRACT-side
bugs were found and **fixed**:

- **manual-subtract drained the wrong SKU** (`finished_goods.py` `manual_subtract_finished_good`):
  matched FG on bare `recipe`, so a recipe name shared across formats/brands drained as
  one pool. Now matches on the full `brand|recipe|format` SKU key (recomputed from the
  components the "Adjust Inventory" modal already sends), recipe-only fallback for legacy
  callers; stamps `sku_key` into the adjustments audit line.
- **edit-sale corrupted the lot record + drifted FG** (`sales.py` `edit_organic_sale`):
  adjusted FG by a delta in place — a delta>0 partial deduction wasn't rolled back on the
  422, and neither direction updated `sale.lots[]` (only the scalar qty), so a later delete
  restored the OLD quantity. Now reverses the original deduction in full and re-applies the
  new qty FIFO on a **trial copy** (commit only on success → a shortfall changes nothing),
  rewriting `sale.lots[]` to match. Organic (lot-tracked) sales REFUSE a quantity edit
  (delete + re-record with a real allocation instead). Canonical restore extracted to
  `sales._restore_sale_lots()`, shared by edit and delete.
- **Ripe approve double-deducted on retry** (`ripe_orders.py` `create_ripe_sale_records`):
  FG is deducted + sales written BEFORE the status push to Ripe; if that push failed (502)
  the order stayed `pending` on Ripe, so a retry passed the pending-check and deducted again.
  Now an idempotency guard keyed on `ripe_order_id` skips the deduction if sale records
  already exist for that order (internal-only; no Ripe API contract change).

**Two systemic gaps left UNFIXED (by design — deliberate conversations, not squeeze-ins):**
1. **No FG write path emits an inventory ledger event.** All ~18 mutating sites write
   `finished_goods.json` directly; `ledger.py` is read-only + the reset. The ledger
   reconstructs history by re-projecting from FG/sales records (`backfill_fg_events`), not
   a write-through log. This is the known architecture, not a regression.
2. **The two-tier organic boundary is convention-only, not enforced in code.** No automated
   subtract path (scheduled deductions, Shopify/Clover commit, Ripe approve) excludes
   `certification == "Organic"` SKUs, and Ripe/channels even copy the organic cert onto the
   sale. Organic stays lot-accurate only because it's catalogued wholesale-manual-only. A
   mis-catalogued organic SKU on a retail channel would silently FIFO-deduct an organic lot.
   Likewise both sales *add* paths choose allocation-vs-FIFO on whether `allocated_lots` was
   sent, NOT on the cert flag — no server guard requires an organic sale to carry an allocation.

---

## Environment variables

**Soma:**
- `DATA_DIR=/opt/render/project/data`
- `SECRET_KEY`
- `APP_PASSWORD`
- `MANAGER_PASSWORD`
- `INTERNAL_API_KEY` — used by Ripe→Soma calls AND by the Shopify/Clover cron jobs
- `RIPE_PORTAL_URL`
- `RETAIL_PORTAL_URL` — the SBBC Wholesale Portal (retail_orders.py); set 2026-07-02
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
- **JavaScript `var` — ACCEPTED DEBT (do NOT bulk-convert by hand).** Templates use `var`
  throughout (~1827 uses across 26 templates). This is valid, working JS — purely a style
  modernization, zero functional benefit. Bulk var→let/const is risky (hoisting/TDZ/
  redeclaration/loop-closure semantics) and the breakage only surfaces at browser runtime.
  The sanctioned fix is tooling (`jscodeshift`/`eslint --fix`) **plus** browser verification —
  neither was available in the session that triaged this (no node/eslint, no browser). Decision
  (2026-06-03): leave existing `var` as-is; do the conversion only in an env with the tool + a
  browser, where it's safe and free. **Forward-only policy: write NEW JS in `let`/`const`** so
  the debt stops growing.
- Hardcoded hex colour values remain in templates alongside CSS variable uses, but every non-standalone template has now had its exact-value hexes swept to tokens (2026-06-03). The literals that remain are token-less by design — JS logic/categorical palettes and the round-2 promotion candidates (see "UI styling convention" below). See that section for the cascade + remaining unification work.
- Docstrings: **DONE (2026-06-04).** Concise docstrings on all previously undocumented
  top-level functions/methods across every module — including the final 8 in `ripe_orders.py`
  (init_paths, _configured, _ripe_request, _load, _save, _soma_login_required,
  ripe_orders_page, ripe_pending_count), added 2026-06-04 (docstrings can't change the API
  contract, so they're safe despite the "don't modify without checking the Ripe portal" rule).
  Nested closures (e.g. `decorated`, `_walk`) intentionally left bare.
- Type annotations — **ACCEPTED DEBT / deferred.** Essentially none in the codebase. A blanket
  pass over ~359 functions is high-churn and low-value with no type checker (no `mypy`/CI) to
  verify it, and wrong hints are worse than none. Decision (2026-06-03): skip until a type
  checker is added; if revisited, start with the stable `helpers.py` foundation layer.
- PEP 8 blank lines: **DONE (2026-06-04).** Normalized E302/E303/E305/E306 across all Python
  modules with `autopep8 --select=E301,E302,E303,E305,E306` (whitespace-only, verified no
  non-blank-line changes; all modules compile; 0 violations remain). Forward-only: new
  top-level defs get 2 blank lines.

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

**Production dashboard tiles carry colour (2026-08-19).** The floor finds a tile by its
colour before it reads the label: `.tile-green` = today's work (Today's / Weekly Schedule),
`.tile-amber` = the End of Day flow (clipboard icon), `.tile-blue` = reference you only look
things up in (Recipe Cards, CCP Checklist). All from the shared token palette — the one
addition is `--info-light` (#90caf9), the Material-200 companion to `--green-light` /
`--amber-border` so a tinted card can carry a soft border.

**Dashboard greeting (2026-08-19).** `templates/dashboard.html` opens with one greeting
bar above the tiles, shared by BOTH roles (one template renders both). Five slots by local
hour — 04:00 "Good Morning Soma", 11:00 "Anyone hungry?", 13:00 "Good afternoon Soma",
15:00 "Let's wrap this up", 18:00 "Zzzzzzzzz" — recomputed on every rotation tick so a
tablet left open all day keeps up. The phrase rotates every 30s through English, Nepali,
English, Greek, English, French (English on every other turn); the asleep line is identical
in all four, so the rotation holds rather than fading between identical text. Beside it, a
live Toronto temperature from Open-Meteo (no API key, client-side, refreshed every 15 min)
that stays HIDDEN on any failure — an offline tablet must never see a stale number.
**This is the codebase's only external font dependency:** Caveat (Latin + Greek) + Kalam
(Devanagari) from Google Fonts, falling back to the device cursive. If another page ever
needs handwriting, reuse that pair rather than adding a third family.

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

### UX simplification / two-role split — IN PROGRESS (started 2026-08-18)

Goal: two logins, two dashboards. `APP_PASSWORD` → production role (kitchen tablet:
Today's Schedule, Weekly Schedule with prev/next, Cleaning & Upkeep [= /cleaning,
renamed], Recipes READ-ONLY, + placeholder Opening/Closing checklists);
`MANAGER_PASSWORD` → manager role (the HOO's desktop: today's full-weight Schedule /
Quick Actions / Buyer Portals tiles, then reduced-weight expandable rows whose headers
list their sub-sections — Analytics, Completed Production, Inventory, **Sales & Receiving
Record** (Records tab extracted from organic.html to its own page), Organic
Certification, Buyers & Suppliers, Recipe Cards, Cleaning Records [placeholder],
Settings & Other [company settings incl. Ripe buffer + credits, CCP master, backup,
Shopify/Clover import, Ripe tools — home for every orphan]). The current manager gate
(`/api/verify-manager` + sessionStorage on the dashboard's Administration `<details>`)
is client-side only and gets deleted; recipe write routes become truly manager-only.

Sequence: **Phase 1 invisible cleanups — DONE 2026-08-18** (dead organic.html panes;
`?tab=` deep links; dead `/checklist` route + `ripe_products.html`). **Phase 2 — DONE
2026-08-18:** `/sales-receiving` (`templates/sales_receiving.html`, route in `sales.py`)
holds the former Records tab (Search & Trace / Receiving / Sales); organic.html is now 3
tabs (Raw / Runs / FG, 4,502 lines) and `?tab=records` redirects to the new page.
**Phase 3 — DONE 2026-08-18:** `session["role"]` set at login (`MANAGER_PASSWORD` →
`manager`, `APP_PASSWORD` → `production`; unset `MANAGER_PASSWORD` → `APP_PASSWORD` grants
manager; pre-role sessions count as manager via `current_role()`); `manager_required`
decorator (app.py, verbatim local copy in recipes.py) on all recipe write routes;
`/api/verify-manager` + the dashboard curtain deleted; `dashboard.html` renders per role
(`role=` from `current_role()`); recipes.html `READ_ONLY` for production; weekly_view hides
Edit/Create for production; cleaning page retitled "Cleaning & Upkeep".
**Phase 4 — DONE 2026-08-18:** the role boundary is now enforced server-side on EVERY
management route, not just recipe writes. Whole blueprints became manager-only (their local
`login_required` copy was renamed to `manager_required`): `raw_materials`, `finished_goods`,
`sales`, `audit_tools`, `ledger`, `suppliers`, `buyers`. `production.py` gained a second local
copy alongside `login_required` — scheduling (create/list/delete), the production tracker and
the completed-production record (incl. weekly sign-off + `delete_traceability_record`) are
manager; the tablet keeps `/weekly-schedule`, `GET /api/schedule/<week>`, daily production +
checklists (the consumption chain). `ripe_orders.py`/`retail_orders.py` swapped
`_soma_login_required` → `_soma_manager_required` on all 19 buyer-portal routes (needed a
`redirect` import). In `app.py`, 39 routes flipped (contacts, company settings, CCP write,
certifications, analytics, audits, backup, schedule PDFs, Shopify/Clover import). **The
production role's whole reachable surface is now exactly its dashboard:** `/`,
`/weekly-schedule`, `/daily-production/*` + checklist APIs, `/cleaning` + its APIs,
`/recipes` + recipe GETs/PDFs, plus `/api/photos`, `/api/label`, `GET /api/ccp-master`,
and (2026-08-19) the `/ccp-master` PAGE read-only — the route is `login_required` and
passes `can_edit`, which hides the Edit/Save controls for the floor; `POST /api/ccp-master`
stays `manager_required`, so the page is view-only in the template AND on the server.
Method: AST-located decorator lines flipped in place (never text-replace); gate = 192-route
map byte-identical + a two-role test-client smoke (locked pages 302→`/`, locked APIs 403,
production routes 200, manager blocked nowhere). Completed Production is manager-only by
Jeremy's decision — the floor has no link to it and sign-off is the HOO's.
**Still open:** daily checklists + cleaning records (separate design session), `base.html`
consolidation (deferred). Details in the memory note `project_ux_simplification`.

### Coco Market direct ship — PLANNED (2026-08-17)

> **The authoritative spec is `RETAIL_CONTRACT.md`** at this repo's root, kept
> byte-identical with the copy in the Ripe portal — diff the two to detect drift. Read it
> before touching anything retail. The summary below is orientation only.

Ripe's retail fulfillment for Coco Market moves to Soma. Ripe previously took bulk
delivery at Coco Market and picked/packed retail orders themselves; Soma now packs
each retail order at the factory and hands the parcel to Ripe's third-party carrier
(Trexity / Canpar / Canada Post).

**What Soma does:** pack to the order, affix the label Ripe supplies, hand to carrier.
**What Soma does NOT do:** choose a carrier, buy a label, price shipping, or track
delivery. Ripe owns all of that through its own shipper portal.

Settled decisions (do not relitigate without checking the Ripe side):

- **Manual entry, not a Shopify feed.** Ripe keys each order into the Ripe portal,
  picking products from Soma's catalogue. That picker is the SKU validation boundary —
  it's what stops Ripe's storefront SKU conventions reaching Soma's inventory. An
  automated importer later must feed the same boundary, not bypass it.
- **Labels are always PDFs**, uploaded by Ripe at order-entry time, one per order.
  Ripe's portal generates its own pack sheet, so no Shopify packing slip transits.
- **Auto-approve on payment**, reusing `ripe_retail_auto_approve` — orders reach Soma
  already approved, with sale records written and FG deducted.
- **Soma interacts exactly once**: a single packed/fulfilled action. Resist adding a
  "packed, awaiting pickup" intermediate state — it would double the interaction cost
  of what becomes the highest-volume path in the system.
- **No case minimums** for any mode. The `ss_small_order_threshold` gate in
  `ripe_order_action`'s approve branch gets deleted, along with Ripe's
  `MIN_SS_CASES_DELIVERY` — they currently disagree (20 vs 40) for what reads like the
  same rule, which is exactly the drift this contract is supposed to prevent.
- Likely modelled as `fulfillment_method: "pickup" | "ship"` on the existing
  `order_mode: "retail"`, not a third mode — that keeps every paid-up-front,
  no-wholesale-rules, auto-approve path working untouched.

**Soma-side work:** a pack queue showing item list and shipping label side by side with
Ripe's order number visible on both. That pairing is the only defence against a
mislabelled parcel — because the destination address exists only on Ripe's label, Soma's
data model never holds it, and a label attached to the wrong order cannot be detected.

Note that `retail_orders.py` (SBBC) is the closer structural template than the wholesale
path in `ripe_orders.py`: pre-paid orders, retail units carrying `sku_key` directly, no
wholesale business rules. The one divergence is auto-approve — SBBC reviews by hand,
this does not.

### Blueprint split — COMPLETE (2026-06-03)

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
- ✅ **inventory/misc tail** (commit `f350a44`) — FINAL step. Folded each leftover into
  its rightful home rather than a synthetic blueprint: finished_goods.py += sku-meta
  (update_sku_meta, get_all_sku_meta) + inventory page shells (organic_page,
  organic_certification_page) [added render_template to its flask import; all app-quals
  already present]; production_bp += get_organic_runs. Deliberately LEFT in app.py as
  intended core: `internal_fg_stock` (Ripe-internal, X-Internal-Key/hmac auth, no
  login_required) and `get_organic_contacts` (trivial cross-cutting read). Smoke: sku-meta
  GET/PATCH/null-removal/400, both pages, production-runs, + core contacts still resolves.

**The planned app.py blueprint split is COMPLETE** (2026-06-03). 9 blueprints
(suppliers, buyers, recipes, sales, finished_goods, raw_materials, audit_tools,
production; plus the pre-existing ripe_orders) + the helpers.py foundation layer.
app.py went from 7940 → ~4688 lines. Everything still in app.py is the intended core.
Any future page/domain should follow the established patterns below (and the UI cascade).

**Two reusable blueprint patterns are now established** (both define a local verbatim
`login_required` to avoid a circular import, matching `ripe_orders.py`):
1. **Self-contained** — when a domain owns its data file and helpers (suppliers).
2. **Routes-move, helpers-stay** — when helpers are shared cross-domain (buyers).
   Blueprint does a bare `import app` at module top (binds the partially-initialised
   module safely; attributes resolve only at request time) and calls `app.X(...)`.
`ripe_orders.py` was untouched and still resolves its lazy `from app import` calls
because `app.py` re-exports the moved names.

**Inventory was split per sub-domain** (decided 2026-06-03 — it was ~44 routes / ~1385
lines straddling audit-critical code): `finished_goods.py` + `raw_materials.py` +
`audit_tools.py` (✅ all above), with the sku-meta + inventory-page tail later folded into
finished_goods.py. **Production domain FULLY extracted** in 3 slices (schedules+tracker,
daily-production+checklists, traceability/completed-records) into one `production_bp`.

**NOTHING planned remains.** The split is complete (see the ✅ list above). If a future
need arises to extract more from the core, apply the same proven method: re-run line
ranges live (they drift); classify each external name by TRUE home (helpers → direct
import; app.py → `app.`-qualify; Flask-instance attr → `app.app.X`; decorators applied at
import time → LOCAL verbatim copies); pure routes-move (slice by per-function AST line
range, never `src.replace`); strengthened free-variable AST audit on the new file;
byte-identical 166-route map; test-client smoke. Assume shared helpers STAY in `app.py`.

Everything beyond that (dashboard, auth, channel imports, analytics, equipment,
company-info, certifications, CCP, audits, important-documents, Ripe internal endpoints,
PWA, and all the shared helpers reached via `import app`) is the INTENDED app.py core —
the split was never meant to dissolve app.py entirely.

> Correction to the earlier audit: the helper list once named four functions that don't
> exist as movable top-level defs — `_revenue`, `_cases`, `_entry_prod_date` are **nested
> closures**, and `_infer_section_for_ingredient` **doesn't exist**. The real, movable
> foundation is the 34-name set now in `helpers.py` (above).

**COGS model** — was built, then removed in a prior session; **not planned to return** (confirmed 2026-06-03). The removal was clean: there is no COGS route, page, data file, nav link, or helper left. NOTE: the `cogs` field that still appears in `buyers.py`, `app.py` (buyer catalogue), and the `buyer_edit` / `ripe_products` / `contacts` templates is the **active buyer-pricing field** (the price = cogs × (1 + margin/100) triangle per buyer SKU) — it is unrelated to the removed COGS model; do not strip it. **The triangle is enforced SERVER-side since 2026-08-19** — `buyers._normalize_sku_pricing`, shared by POST and PUT `/api/buyers`: whichever two of price/cogs/margin_pct are present decide the third, price+cogs winning. Before that it was computed only in `buyer_edit.html`'s input listener and the server stored whatever it was handed, so saving a buyer WIPED margin_pct on every SKU the user hadn't touched (the edit page inits each row's margin to null, and `merged.update(sku)` let that null overwrite the stored value). Two dead write endpoints were deleted the same day — `PATCH /api/buyers/<bid>/skus/<sku_key>/pricing` (which held the ONLY server-side copy of the triangle and had no caller — buyer_edit.html saves the whole buyer with one PUT) and `PUT /api/suppliers/<sid>/ingredients` (contacts.html saves ingredients inside the whole-supplier PUT). Note `margin_pct` still has no consumer beyond the edit page's own display.

---

## Hardening / resilience audit (2026-06-04) — findings & backlog

A code-grounded resilience assessment was run on 2026-06-04 (four parallel read-only
probes: deploy/runtime, error-handling, security, data durability). **Nothing here was
fixed** — this is a risk register for future sessions. The app is fundamentally sound
(atomic writes, per-path locks, hardened audit chain, timing-safe internal API that fails
*closed*, no committed secrets, path-traversal defended, no injection vectors). The gaps
cluster into four failure modes; most high-leverage fixes are cheap. Build one at a time,
each with a test + smoke per the deploy workflow. Order below is the recommended sequence.

**🟥 Availability — CHEAPEST HIGH-LEVERAGE FIX (do first, ~20 min, doesn't touch audit logic).**
One corrupt JSON file can currently take down the *entire* app, not just one page:
- `_run_scheduled_deductions()` runs at startup with **no try/except** (`app.py` ~L4077). A
  malformed sales/FG record → app won't boot → looks like a total outage. Wrap it (and
  `_seed_sku_meta_defaults`) so a bad record logs + the app still serves.
- `_load_json()` (`helpers.py` ~L80) doesn't catch `json.JSONDecodeError` → a truncated
  file 500s every route that reads it. Make it fail soft (log + return default, or a
  guarded variant for non-critical reads).
- No `/health` endpoint; root requires auth, so Render can't distinguish "crashed" from
  "needs login." Add an unauthenticated `/health` returning 200.

**🟥 Durability — #1 BUSINESS RISK (worst consequence).** All data is JSON on a *single*
Render disk; the only backup is the **manual** `/api/admin/backup` zip (manager-gated,
download-by-hand, no offsite/versioning). Disk failure between manual downloads = total
loss of everything since. Fix = **automated daily offsite backup** (Render cron → S3/GCS,
or scheduled email of the zip). This was always the spirit of Track A item 1 — the gap is
it's still manual.

**🟧 Integrity — the "no transactions" tax (architectural; mitigate or eventually move to a DB).**
All stem from JSON files lacking cross-file/optimistic-concurrency guarantees:
- **Lost-update under concurrency:** per-path lock is released *between* read and write, so
  two simultaneous saves to e.g. `finished_goods.json` silently clobber (inventory wrong,
  no error). Low probability at 1–2 users, bad for the audit trail. Mitigation: re-read
  inside the lock / add a version field.
- **Torn multi-file writes:** production completion writes runs→raw→FG as 3 separate files
  (`app.py` ~L2492; same shape in `_run_scheduled_deductions` and `delete_traceability_record`).
  Crash between them = raw consumed with no FG (or vice versa).
- **Ripe approve cross-system desync (money path):** `ripe_order_action` (`ripe_orders.py`
  ~L465) deducts FG + records the sale *before* notifying Ripe; if that call fails it
  returns 502 but inventory already moved, and a retry can double-approve. Worth a closer
  look despite being rare.
- Clean long-horizon answer for the inventory/sales/audit core is **SQLite/Postgres** — a
  real project, a deliberate fork, NOT a squeeze-in.

**🟨 Security / config — mostly internal hardening (not outsider-exploitable; behind Render HTTPS).**
- **Fail-open defaults:** `APP_PASSWORD` defaults to `"soma2026"`, `SECRET_KEY` to a known
  string if env vars unset (`app.py` ~L80/L97). Add a startup assertion that *refuses to
  boot* without them — removes the "forgot the env var" footgun. (APP_PASSWORD default was
  already a known weakness pre-audit.)
- Login uses `==` not `hmac.compare_digest` (`app.py` ~L845); `SESSION_COOKIE_SECURE` not
  set. Both small.

**⬜ Operational backlog (overlaps Track A, still open):** no tests, no CI, no staging branch,
Python version unpinned (`render.yaml` says only `python`; deps are `==`-pinned but no lock
for transitives), no written runbook. The **runbook** (one page: how to roll back, where
data lives, who to call) is the highest-value of these. Also: `vision_scan.py` Claude API
call has no timeout; Shopify/Clover importers have timeouts but no retry.

**Recommended sequence:** (1) availability fix → (2) automated backup → (3) startup
secret/password assertions. All three are small, clearly safe, and avoid the consumption
chain. The integrity items + JSON→DB question are deliberate conversations, not quick wins.

---

## What NOT to do

- Do not use string replacement (`src.replace(block, '')`) to extract functions from app.py — this caused major damage in a previous session by silently truncating adjacent functions
- Do not modify `ripe_orders.py` without also checking the Ripe portal app — they share an API contract
- Do not deploy without checking Render logs — startup crashes return 500 on all routes and look like "data missing"
- Do not touch the data directory on Render — all deploys should exclude `data/*`

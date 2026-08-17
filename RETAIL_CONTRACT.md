# Retail Direct-Ship Contract — Ripe ↔ Soma

**This file must be byte-identical in both repositories.** It lives at the root of
`ripe-portal` and `soma-scheduler`. If the two copies ever differ, that diff *is* the
bug — the two applications have drifted apart on a shared data shape. Diff them before
trusting either.

Status: **settled specification, not yet implemented.** Written 2026-08-17.

Scope: retail direct-ship orders only (`order_mode: "retail"`). The wholesale contract
is documented in each repository's `CLAUDE.md` and is unchanged by this.

---

## The arrangement

Ripe keys each retail order into the Ripe portal by hand, attaches a shipping label it
generated and paid for, and settles a batch of them with one credit-card payment. Soma
packs each parcel at the factory, affixes Ripe's label, and hands it to Ripe's carrier
(Trexity, Canpar, or Canada Post).

Soma does not choose a carrier, buy a label, price shipping, or track delivery.

**Why orders are keyed by hand rather than fed from Shopify:** the order form picks
products from Soma's catalogue, so `sku_key` is correct by construction. That picker is
the SKU validation boundary. Any future automated intake must feed that same boundary
rather than bypass it — Ripe's storefront SKU conventions must never reach Soma's
inventory.

---

## The order record

Soma reads exactly these fields off a retail order. Everything else on the record is
Ripe-internal and Soma must not depend on it.

| Field | Owner | Notes |
|---|---|---|
| `id` | Ripe | 12-char uppercase. The join key for every cross-app call. |
| `order_mode` | Ripe | Always `"retail"`. |
| `created_at` | Ripe | ISO 8601 UTC. |
| `status` | both | See status machine below. |
| `payment_status` | Ripe | Soma must treat anything other than `"paid"` as invisible. |
| `items[]` | Ripe | See below. |
| `subtotal` | Ripe | Sum of `line_total`. |
| `total` | Ripe | `subtotal` + surcharge. |
| `payment_key` | Ripe | Always `"stripe_checkout"`. |
| `payment_label` | Ripe | Always `"Card (Stripe Checkout)"`. |
| `delivery_label` | Ripe | **See "Sales ledger" below — this is not display-only.** |
| `delivery_address` | Ripe | Empty string for direct ship. Never the customer's address. |
| `requested_date` | Ripe | Unused for direct ship. Soma must not read it. |
| `credits_applied` | Ripe | Always `[]` — credits are wholesale-e-transfer-only. |
| `customer_name` | Ripe | Required. Printed on the packing slip. |
| `order_number` | Ripe | Required. Ripe's own order reference. |
| `attachment` | Ripe | The shipping label. Required before the order can be settled. |
| `stripe_checkout_session_id` | Ripe | Shared across every order in the same batch. |

### `items[]`

```
product_id   int      resolved against Soma's catalogue
name         string
sku          string
format       string   MUST begin with "SS"
unit_price   float
units        int      the real quantity — jars
cases        float    units / 12, fractional, derived only
line_total   float    units * unit_price
```

Soma's sale-record logic keys off `units`, not `cases`. `cases` exists only so retail
orders render in views built for wholesale.

---

## Validation — Ripe side, server-enforced

These are enforced in `_build_retail_order_from_payload`. Enforcing them in the template
alone is not enforcement.

1. **Shelf-stable only.** Every item's `format` must start with `SS`. Frozen (`FZ`) and
   Back Bar (`BB`) are rejected outright, always, with no override.
2. **`order_number` and `customer_name` are required** before any item may be added.
3. **At least one jar.** `units > 0` on at least one line.
4. **A label must be attached** before the order can join a batch.
5. **Surcharge is 2.9%**, carried as its own line on the Checkout session — never folded
   into unit prices. This is a Canadian surcharge-disclosure requirement and matches how
   the wholesale invoice discloses it.

---

## Batch settlement

Orders are created unpaid and accumulate in a queue. Ripe settles them together.

1. Ripe submits an order → it is created `pending` / unpaid, **invisible to Soma**.
2. Ripe attaches the label against the now-existing order id.
3. Steps 1–2 repeat for as many orders as needed.
4. Ripe settles: one Stripe Checkout session covering the whole queue, **summarized**
   (a single amount line plus the surcharge line), not itemized per order.
5. The same `stripe_checkout_session_id` is stamped on every order in the batch **before**
   the redirect.
6. On `checkout.session.completed`, the webhook marks **every order carrying that session
   id** as paid.

There is no batch entity, no `batch_id`, and no batch file. The shared session id *is*
the batch.

**Rules.**

- The batch is snapshotted at settle. Orders keyed afterwards belong to the next batch.
- An abandoned checkout leaves orders pending and re-settleable. Re-settling **re-stamps**
  the session id; a stale one must never be left in place.
- A pending unpaid order may be removed from the queue outright.
- Payment always lands before Soma sees anything. There is no credit exposure and
  `_is_unpaid_retail` requires no change.
- Because the Checkout is summarized, Ripe's order history must group by
  `stripe_checkout_session_id` — that is the only record of batch composition.

---

## Status machine

```
pending  (created, unpaid, hidden from Soma)
   │
   ├── batch settles ──▶  pending / payment_status="paid"   ← visible to Soma
   │                            │
   │                            ├── Approve & Print ──▶  fulfilled   (terminal)
   │                            │
   │                            └── Cancel ───────────▶  declined    (terminal)
   │
   └── removed from queue ──▶ deleted
```

**`approved` is not used for retail direct ship.** Soma touches each order exactly once,
so approve and fulfill collapse into a single terminal state. Do not introduce an
intermediate "packed, awaiting pickup" status — it would double the interaction cost of
the highest-volume path in the system.

Note that the three existing modes each define `fulfilled` differently (wholesale =
collected; SBBC retail = courier-confirmed delivery). For retail direct ship it means
**packed and handed to the carrier**, and nothing further is ever observed — Soma never
bought the label and has no visibility into delivery.

---

## Soma's obligations

**Approve & Print** — one action per order:

1. Record the sale and FIFO-deduct finished goods, **dated the moment of the click**, not
   the payment date and not any date carried on the order.
2. Print the Soma-generated retail packing slip: customer name, Ripe's order number,
   SKUs and quantities. **No pricing, ever.**
3. Print the attached label PDF (up to ~4 pages for a multi-parcel order).

A batch-level **Approve & Print All** runs the above per order underneath and reports a
summary (`"18 approved, 2 could not be filled"`), leaving shortfalls in the queue.
All-or-nothing batch approval is prohibited — one short SKU must never block the rest.

**Cancel** — when stock cannot cover an order:

1. Maps to the existing `decline` action, which skips Stripe entirely for an order with
   no outstanding invoice.
2. **No money moves.** No refund, no void. The payment stays captured with Soma and
   `payment_status` remains `"paid"`.
3. No stock is reversed, because nothing was recorded until the click.
4. A Soma admin manually adds a credit to Ripe's account in Company Settings. Show the
   order total on the cancel confirmation so that number is in front of them.
5. That credit is redeemable against **wholesale e-transfer orders only.** Known,
   accepted, and not to be "fixed" without a decision.

**Display** — orders are grouped by batch, each individually openable.

---

## Sales ledger mapping

`delivery_label` and `delivery_address` are copied into every sale record as
`location_name` and `location_address`. **They are permanent ledger data, not display
strings.**

The retail builder currently hardcodes them to Soma's pickup location. For direct ship
that would record every parcel as a pickup that never happened. Direct-ship orders must
carry:

```
delivery_label    "Direct ship (3rd-party carrier)"
delivery_address  ""
```

The customer's shipping address is **never** stored on either side. It exists only on
Ripe's label. Soma needs to know what to pack and which label goes on it, not where it
is going.

---

## The failure mode this design accepts

Because the destination address lives only on the label, **attaching the wrong label to
an order ships real goods to the wrong person, and nothing in either system can detect
it.** There is no cross-check available.

The mitigation is procedural and must survive any redesign: one label uploaded per order
at entry time, never batched afterwards, and Soma's pack view showing the item list and
the label together with Ripe's order number visible on both.

---

## What must not happen

- **Never accept `FZ` or `BB` in a retail order.** Not by override, not by admin, not by
  direct API call. Frozen product moving by parcel carrier is a cold-chain failure.
- **Never expose an unpaid retail order to a Soma-facing endpoint.** Apply
  `_is_unpaid_retail()` to any new one.
- **Never refund or void a cancelled retail order.** Money stays with Soma; the credit is
  issued by hand.
- **Never record a retail sale before Soma clicks.** That timing is what makes cancelling
  free of inventory consequences.
- **Never print pricing on a retail packing slip.** It goes to Ripe's end customer.
- **Never store the end customer's address** on either side.
- **Never let the two copies of this file diverge.**

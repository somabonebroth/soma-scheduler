"""Organic Sale (sales.add_organic_scan_order, 2026-09-30).

Scanned organic cases come off exactly the lot scanned; other products FIFO;
all-or-nothing; organic SKUs only by scan, plain SKUs never by scan; prices
from the buyer's catalogue.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import helpers

ORG = "Soma|Organic Chicken Bone Broth|SS-750ML"
PLAIN = "Soma|Chicken Bone Broth|SS-750ML"


def fg(id, recipe, lot, remaining, cert, created):
    return {"id": id, "brand": "Soma", "recipe": recipe, "format": "SS-750ML", "certification": cert,
            "lot": lot, "quantity_produced": remaining, "quantity_remaining": remaining,
            "created_at": created}


BUYER = {"id": "b1", "name": "Nature's Emporium",
         "skus": [{"sku_key": ORG, "price": 9.5}, {"sku_key": PLAIN, "price": 7}]}


class Route(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fg_path = os.path.join(self.tmp, "fg.json")
        self.sales_path = os.path.join(self.tmp, "sales.json")
        with open(self.fg_path, "w") as f:
            json.dump([fg("o1", "Organic Chicken Bone Broth", "210927", 48, "Organic", "2026-09-22"),
                       fg("o2", "Organic Chicken Bone Broth", "280927", 240, "Organic", "2026-09-29"),
                       fg("p1", "Chicken Bone Broth", "150927", 24, "Conventional", "2026-09-16"),
                       fg("p2", "Chicken Bone Broth", "280927", 300, "Conventional", "2026-09-29")], f)
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.fg_path),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.sales_path),
            mock.patch.object(app, "_load_buyers", return_value=[BUYER]),
            mock.patch.object(helpers, "ORGANIC_CONTACTS_PATH", os.path.join(self.tmp, "c.json")),
        ]
        for p in self.patches:
            p.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def post(self, cases=(), lines=(), buyer_id="b1"):
        return self.c.post("/api/organic/sales/organic-order", json={
            "buyer_id": buyer_id, "sale_date": "2026-10-02", "po_number": "NE-1",
            "cases": list(cases), "lines": list(lines)})

    def remaining(self):
        return {f["id"]: f["quantity_remaining"] for f in json.load(open(self.fg_path))}

    def sales(self):
        return json.load(open(self.sales_path)) if os.path.exists(self.sales_path) else []

    def test_scanned_lots_exact_and_plain_fifo(self):
        r = self.post(cases=[{"sku_key": ORG, "lot": "280927", "cases": 3},
                             {"sku_key": ORG, "lot": "210927", "cases": 1}],
                      lines=[{"sku_key": PLAIN, "cases": 3}])
        self.assertEqual(r.status_code, 200, r.get_json())
        # Organic: the NEWER lot drained as scanned (not FIFO); plain: oldest first.
        self.assertEqual(self.remaining(), {"o1": 36, "o2": 204, "p1": 0, "p2": 288})
        rows = self.sales()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({s["order_id"] for s in rows}), 1)
        org = next(s for s in rows if s["sku_key"] == ORG)
        self.assertEqual((org["quantity"], org["cases"], org["certification"]), (48, 4, "Organic"))
        self.assertEqual(sorted(l["lot"] for l in org["lots"]), ["210927", "280927"])
        self.assertEqual((org["unit_price"], org["line_total"]), (9.5, 456.0))
        self.assertEqual((org["buyer"], org["sale_date"], org["entry"]), ("Nature's Emporium", "2026-10-02", "organic_sale"))
        self.assertTrue(org["deducted_at"])
        self.assertEqual(r.get_json()["slip_sale_id"], rows[0]["id"])

    def test_over_scan_saves_nothing(self):
        r = self.post(cases=[{"sku_key": ORG, "lot": "210927", "cases": 5}],
                      lines=[{"sku_key": PLAIN, "cases": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.remaining(), {"o1": 48, "o2": 240, "p1": 24, "p2": 300})
        self.assertEqual(self.sales(), [])

    def test_organic_cannot_be_a_plain_line(self):
        r = self.post(lines=[{"sku_key": ORG, "cases": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("must be scanned", r.get_json()["details"][0])

    def test_only_the_buyers_own_products(self):
        # 2026-10-02: a Benefits by Nature case went into a Nature's Emporium order.
        other = "Benefits by Nature|Organic Chicken Bone Broth|SS-750ML"
        with open(self.fg_path) as f:
            rows = json.load(f)
        rows.append(dict(fg("x1", "Organic Chicken Bone Broth", "210727", 48, "Organic", "2026-07-21"),
                         brand="Benefits by Nature"))
        with open(self.fg_path, "w") as f:
            json.dump(rows, f)
        r = self.post(cases=[{"sku_key": ORG, "lot": "280927", "cases": 1},
                             {"sku_key": other, "lot": "210727", "cases": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertIn("Benefits by Nature Organic Chicken Bone Broth · SS-750ML is not on Nature's Emporium's",
                      r.get_json()["details"][0])
        self.assertEqual(self.sales(), [])
        self.assertEqual(self.remaining()["x1"], 48)

    def test_plain_cannot_be_scanned(self):
        r = self.post(cases=[{"sku_key": PLAIN, "lot": "280927", "cases": 1}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.sales(), [])

    def test_needs_a_known_buyer_and_something_to_record(self):
        self.assertEqual(self.post(cases=[{"sku_key": ORG, "lot": "280927", "cases": 1}], buyer_id="x").status_code, 400)
        self.assertEqual(self.post().status_code, 400)

    def test_record_sale_order_route_unchanged(self):
        r = self.c.post("/api/organic/sales/order", json={
            "buyer": "Healthy Planet", "sale_date": "2026-10-01", "po_number": "HP-9",
            "lines": [{"sku_key": PLAIN, "quantity": 12, "unit_price": 7}]})
        self.assertEqual(r.status_code, 200)
        row = self.sales()[0]
        self.assertEqual((row["buyer"], row["sale_date"], row["po_number"], row["quantity"], row["line_total"]),
                         ("Healthy Planet", "2026-10-01", "HP-9", 12, 84.0))
        self.assertNotIn("deducted_at", row)


    # Go-live 2026-10-01: Record Sale's routes refuse organic stock.
    def test_record_sale_order_refuses_any_organic_line(self):
        r = self.c.post("/api/organic/sales/order", json={
            "buyer": "Nature's Emporium", "sale_date": "2026-10-01",
            "lines": [{"sku_key": PLAIN, "quantity": 12}, {"sku_key": ORG, "quantity": 12}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("each case is scanned", r.get_json()["error"])
        self.assertEqual(self.sales(), [])  # the plain line was NOT saved either
        self.assertEqual(self.remaining(), {"o1": 48, "o2": 240, "p1": 24, "p2": 300})

    def test_record_sale_order_refuses_organic_named_by_recipe(self):
        r = self.c.post("/api/organic/sales/order", json={
            "buyer": "x", "lines": [{"brand": "Soma", "recipe": "Organic Chicken Bone Broth",
                                     "format": "SS-750ML", "quantity": 12}]})
        self.assertEqual(r.status_code, 400)

    def test_single_sale_refuses_organic(self):
        for body in ({"sku_key": ORG, "quantity": 12}, {"fg_id": "o1", "quantity": 12},
                     {"sku_key": ORG, "quantity": 12, "allocated_lots": [{"lot": "210927", "quantity": 12}]}):
            r = self.c.post("/api/organic/sales", json=dict(body, buyer="x", sale_date="2026-10-01"))
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self.sales(), [])

    def test_single_sale_still_sells_plain(self):
        r = self.c.post("/api/organic/sales", json={"sku_key": PLAIN, "quantity": 12, "buyer": "x",
                                                     "sale_date": "2026-10-01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.remaining()["p1"], 12)


if __name__ == "__main__":
    unittest.main()

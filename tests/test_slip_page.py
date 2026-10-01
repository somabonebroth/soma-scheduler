"""The packing slip as a printable page (sales.py, 2026-10-01).

A PDF opened from the home-screen app on an iPhone has no print control, so a
recorded sale's slip is also a web page with a Print button. The page and the
PDF read the same `_packing_slip_data`, so they show the same lines.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import helpers
import sales

ORG = "Soma|Organic Chicken Bone Broth|SS-750ML"
BUYER = {"id": "b1", "name": "Nature's Emporium", "address": "1 Main St",
         "skus": [{"sku_key": ORG, "price": 9.5}]}


class SlipPage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = {n: os.path.join(self.tmp, n + ".json") for n in ("fg", "sales", "info", "contacts")}
        with open(self.p["fg"], "w") as f:
            json.dump([{"id": "o1", "brand": "Soma", "recipe": "Organic Chicken Bone Broth", "format": "SS-750ML",
                        "certification": "Organic", "lot": "280927", "quantity_produced": 240,
                        "quantity_remaining": 240, "created_at": "2026-09-29"}], f)
        with open(self.p["info"], "w") as f:
            json.dump({"name": "Soma Bone Broth"}, f)
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.p["fg"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.p["sales"]),
            mock.patch.object(app, "COMPANY_INFO_PATH", self.p["info"]),
            mock.patch.object(helpers, "COMPANY_INFO_PATH", self.p["info"]),
            mock.patch.object(helpers, "ORGANIC_CONTACTS_PATH", self.p["contacts"]),
            mock.patch.object(app, "_load_buyers", return_value=[BUYER]),
            mock.patch.object(sales, "_send_email"),
        ]
        for p in self.patches:
            p.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"
        r = self.c.post("/api/organic/sales/organic-order", json={
            "buyer_id": "b1", "sale_date": "2026-10-02", "po_number": "NE-9",
            "cases": [{"sku_key": ORG, "lot": "280927", "cases": 2}]})
        self.sale_id = r.get_json()["slip_sale_id"]

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_page_shows_the_order(self):
        r = self.c.get(f"/sales/{self.sale_id}/packing-slip")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for text in ("Nature&#39;s Emporium", "1 Main St", "NE-9", "280927",
                     "Total 24 units (2 cases)", "window.print()"):
            self.assertIn(text, html)

    def test_pdf_still_builds(self):
        r = self.c.get(f"/api/organic/sales/{self.sale_id}/packing-slip")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_unknown_sale_is_404(self):
        self.assertEqual(self.c.get("/sales/nope/packing-slip").status_code, 404)

    def test_floor_cannot_open_it(self):
        with self.c.session_transaction() as s:
            s["role"] = "production"
        self.assertNotEqual(self.c.get(f"/sales/{self.sale_id}/packing-slip").status_code, 200)


if __name__ == "__main__":
    unittest.main()

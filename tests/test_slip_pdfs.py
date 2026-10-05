"""Portal packing slips as 4x6 PDFs (2026-10-05).

The iPhone home-screen app cannot print a page (window.print() is a no-op
there), so each slip page's Print button hands a PDF to the share sheet.
These check every slip page names its PDF and every PDF route builds one.
"""
import unittest
from unittest import mock

import app
import ripe_orders
import retail_orders

ITEMS = [{"name": "Nature's Emporium Plain Chicken & Herbs", "format": "SS-876ML", "sku": "NE-1",
          "cases": 4, "units": 48},
         {"name": "Beef <frozen>", "format": "FZ-750ML", "sku": "B1", "cases": 1, "units": 12}]
RIPE = {"id": "ord_1", "order_number": "R-1042", "business_name": "Coco & Co", "notes": "Back door",
        "items": ITEMS, "supplies": [{"label": "Box", "qty_label": "3 boxes"}],
        "customer_name": "Pat", "created_at": "2026-10-04T10:00:00"}
SBBC = {"id": "SBBC-77", "buyer": "Alma Care", "status": "approved", "created_at": "2026-10-03T09:00",
        "fulfillment_date": "2026-10-06", "delivery_zone": 1,
        "delivery": {"contact_name": "Jo", "address": "1 King St", "postal": "M5J 1A1", "notes": "Ring"},
        "items": [{"name": "Soma Chicken", "format": "SS-750ML", "sku_key": "SOMA|CHICKEN|SS-750ML", "qty": 6}]}


class SlipPdfs(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def check(self, page, pdf):
        r = self.c.get(pdf)
        self.assertEqual(r.status_code, 200, pdf)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertTrue(r.data.startswith(b"%PDF"))
        html = self.c.get(page).get_data(as_text=True)
        self.assertIn(f'data-pdf="{pdf}"', html)

    def test_ripe_wholesale_and_retail(self):
        with mock.patch.object(ripe_orders, "_ripe_request", return_value=(200, RIPE)), \
             mock.patch.object(ripe_orders, "_fetch_ripe_orders", return_value=([RIPE], 200)):
            self.check("/ripe-orders/ord_1/packing-slip", "/ripe-orders/ord_1/packing-slip.pdf")
            self.check("/ripe-retail/ord_1/packing-slip", "/ripe-retail/ord_1/packing-slip.pdf")
        with mock.patch.object(ripe_orders, "_ripe_request", return_value=(200, dict(RIPE, items=[], supplies=[]))):
            self.assertEqual(self.c.get("/ripe-orders/ord_1/packing-slip.pdf").status_code, 200)
        with mock.patch.object(ripe_orders, "_ripe_request", return_value=(404, {})):
            self.assertEqual(self.c.get("/ripe-orders/ord_1/packing-slip.pdf").status_code, 404)

    def test_sbbc(self):
        with mock.patch.object(retail_orders, "_retail_request", return_value=(200, SBBC)):
            self.check("/retail-orders/SBBC-77/packing-slip", "/retail-orders/SBBC-77/packing-slip.pdf")

    def test_production_role_locked_out(self):
        with self.c.session_transaction() as s:
            s["role"] = "production"
        self.assertNotEqual(self.c.get("/ripe-orders/ord_1/packing-slip.pdf").status_code, 200)


if __name__ == "__main__":
    unittest.main()

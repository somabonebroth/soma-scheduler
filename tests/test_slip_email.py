"""Organic Sale emails its packing slip (sales.py + Company Settings, 2026-10-01).

Every recorded Organic Sale emails the slip PDF to the fixed list in Company
Settings. The email comes after the sale is saved and can never undo or block
it. A bad address is skipped and reported without blocking other settings.
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
BUYER = {"id": "b1", "name": "Nature's Emporium", "skus": [{"sku_key": ORG, "price": 9.5}]}


class SlipEmail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = {n: os.path.join(self.tmp, n + ".json") for n in ("fg", "sales", "info", "contacts")}
        with open(self.p["fg"], "w") as f:
            json.dump([{"id": "o1", "brand": "Soma", "recipe": "Organic Chicken Bone Broth", "format": "SS-750ML",
                        "certification": "Organic", "lot": "280927", "quantity_produced": 240,
                        "quantity_remaining": 240, "created_at": "2026-09-29"}], f)
        self.set_emails("jeremy@example.com, books@example.com")
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.p["fg"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.p["sales"]),
            mock.patch.object(app, "COMPANY_INFO_PATH", self.p["info"]),
            mock.patch.object(helpers, "COMPANY_INFO_PATH", self.p["info"]),
            mock.patch.object(helpers, "ORGANIC_CONTACTS_PATH", self.p["contacts"]),
            mock.patch.object(app, "_load_buyers", return_value=[BUYER]),
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

    def set_emails(self, text):
        with open(self.p["info"], "w") as f:
            json.dump({"name": "Soma Bone Broth", "organic_slip_emails": text}, f)

    def sell(self):
        return self.c.post("/api/organic/sales/organic-order", json={
            "buyer_id": "b1", "sale_date": "2026-10-02", "po_number": "NE-9",
            "cases": [{"sku_key": ORG, "lot": "280927", "cases": 2}]})

    def test_slip_is_emailed_with_the_pdf(self):
        with mock.patch.object(sales, "_send_email") as send:
            r = self.sell()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["email"], {"sent": True, "to": ["jeremy@example.com", "books@example.com"]})
        recipients, subject, body, attachments = send.call_args.args
        self.assertEqual(recipients, ["jeremy@example.com", "books@example.com"])
        self.assertIn("Nature's Emporium", subject)
        self.assertIn("PO NE-9", subject)
        self.assertIn("LOT# 280927", body)
        filename, data, subtype = attachments[0]
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(subtype, "pdf")

    def test_a_failed_email_still_saves_the_sale(self):
        with mock.patch.object(sales, "_send_email", side_effect=OSError("SMTP down")):
            r = self.sell()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["email"]["sent"], False)
        self.assertIn("SMTP down", r.get_json()["email"]["error"])
        self.assertEqual(len(json.load(open(self.p["sales"]))), 1)

    def test_no_addresses_sends_nothing(self):
        self.set_emails("")
        with mock.patch.object(sales, "_send_email") as send:
            r = self.sell()
        send.assert_not_called()
        self.assertEqual(r.get_json()["email"], {"sent": False, "reason": "no_recipients"})

    def test_not_set_up_reports_clearly(self):
        with mock.patch.dict(os.environ, {"SMTP_USER": "", "SMTP_PASS": ""}):
            r = self.sell()
        self.assertIn("not set up", r.get_json()["email"]["error"])

    def test_bad_address_is_skipped_not_blocking(self):
        r = self.c.patch("/api/company-info", json={"organic_slip_emails": "ok@example.com, nope@", "phone": "555"})
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertIn("nope@", d["field_errors"]["organic_slip_emails"])
        self.assertEqual(d["info"]["phone"], "555")
        self.assertEqual(d["info"]["organic_slip_emails"], "jeremy@example.com, books@example.com")
        r = self.c.patch("/api/company-info", json={"organic_slip_emails": "a@x.com; b@y.com"})
        self.assertEqual(r.get_json()["info"]["organic_slip_emails"], "a@x.com, b@y.com")

    def test_test_button(self):
        with mock.patch.object(sales, "_send_email") as send:
            r = self.c.post("/api/organic/slip-email/test")
        self.assertEqual(r.get_json(), {"ok": True, "to": ["jeremy@example.com", "books@example.com"]})
        send.assert_called_once()
        self.set_emails("")
        self.assertEqual(self.c.post("/api/organic/slip-email/test").status_code, 400)

    def test_slip_button_unchanged(self):
        with mock.patch.object(sales, "_send_email"):
            sale_id = self.sell().get_json()["slip_sale_id"]
        r = self.c.get(f"/api/organic/sales/{sale_id}/packing-slip")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        self.assertTrue(r.data.startswith(b"%PDF"))
        self.assertEqual(self.c.get("/api/organic/sales/nope/packing-slip").status_code, 404)


if __name__ == "__main__":
    unittest.main()

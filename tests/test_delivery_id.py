"""Raw-material deliveries (raw_materials.py, 2026-09-29).

A delivery's lines are saved together and its invoice photo is stored against
the first line only. Every line must still lead to that invoice, so each line
names its delivery — stamped on new saves, derived from the id for old ones.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

import app  # noqa: F401 — must load before its blueprints (circular import)
import helpers
from helpers import _delivery_id


class Derive(unittest.TestCase):
    def test_stamped_id_wins(self):
        self.assertEqual(_delivery_id({"id": "x", "delivery_id": "rm_bulk_20260101120000_000"}),
                         "rm_bulk_20260101120000_000")

    def test_old_bulk_line_points_at_line_000(self):
        self.assertEqual(_delivery_id({"id": "rm_bulk_20260101120000_004"}),
                         "rm_bulk_20260101120000_000")
        self.assertEqual(_delivery_id({"id": "rm_bulk_20260101120000_000"}),
                         "rm_bulk_20260101120000_000")

    def test_no_invoice_lines(self):
        self.assertIsNone(_delivery_id({"id": "rm_bulk_20260101120000_001", "migration_baseline": True}))
        self.assertIsNone(_delivery_id({"id": "rm_adj_20260101120000_000", "adjustment": True}))
        self.assertIsNone(_delivery_id({"id": "202601011200003"}))


class Routes(unittest.TestCase):
    """Save a two-line delivery, attach the photo to line 1, read it via line 2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        photos = os.path.join(self.tmp, "photos")
        os.makedirs(photos)
        recipes = {"A": {"kettle_overnight": [
            {"name": "Organic Carrots", "amount": 1, "unit": "kg"},
            {"name": "Organic Onions", "amount": 1, "unit": "kg"}]}}
        self.patches = [
            mock.patch.object(app, "ORGANIC_RAW_PATH", os.path.join(self.tmp, "raw.json")),
            mock.patch.object(app, "ORGANIC_CUSTOM_ITEMS_PATH", os.path.join(self.tmp, "custom.json")),
            mock.patch.object(app, "RM_RECEIPT_PHOTOS_DIR", photos),
            mock.patch.object(app, "load_recipes", return_value=recipes),
            mock.patch.object(helpers, "ORGANIC_CONTACTS_PATH", os.path.join(self.tmp, "contacts.json")),
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

    def test_every_line_reaches_the_invoice(self):
        r = self.c.post("/api/organic/raw-materials/bulk", json={"baseline": False, "entries": [
            {"item": "Organic Carrots", "unit": "kg", "quantity": 10, "supplier": "Farm", "supplier_lot": "C1"},
            {"item": "Organic Onions", "unit": "kg", "quantity": 5, "supplier": "Farm", "supplier_lot": "O1"}]})
        self.assertEqual(r.status_code, 200)
        first, second = r.get_json()["ids"]
        entries = {e["id"]: e for e in r.get_json()["entries"]}
        self.assertEqual(entries[first]["delivery_id"], first)
        self.assertEqual(entries[second]["delivery_id"], first)

        self.c.post("/api/organic/raw-materials/receipt-photo/" + first,
                    data={"photo": (io.BytesIO(b"\xff\xd8photo"), "inv.jpg")},
                    content_type="multipart/form-data")
        r = self.c.get("/api/organic/raw-materials/receipt-photo/" + second)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"\xff\xd8photo")
        r.close()

        listed = self.c.get("/api/organic/raw-materials").get_json()
        self.assertEqual({m["delivery_id"] for m in listed}, {first})

    def test_baseline_has_no_delivery(self):
        r = self.c.post("/api/organic/raw-materials/bulk", json={"baseline": True, "entries": [
            {"item": "Organic Carrots", "unit": "kg", "quantity": 10}]})
        self.assertNotIn("delivery_id", r.get_json()["entries"][0])
        self.assertEqual(self.c.get("/api/organic/raw-materials/receipt-photo/"
                                    + r.get_json()["ids"][0]).status_code, 404)


if __name__ == "__main__":
    unittest.main()

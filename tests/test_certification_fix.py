"""Correcting a product marked with the wrong certification (recipes.py, 2026-10-01).

A finished-goods batch keeps the certification it was made with, and every
organic check reads the batch — so fixing the recipe card alone leaves stock
already made still "Organic". The recipe save reports the mismatch; the
correction rewrites that SKU's batches and sale rows and records the old value.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import recipes

RECIPES = {"Beef with Herbs and Lion's Mane": {"brand": "Benefits by Nature", "format": "SS-876ML",
                                               "certification": "Conventional"},
           "Organic Chicken": {"brand": "Soma", "format": "SS-750ML", "certification": "Organic"}}


def row(id, brand, recipe, fmt, cert):
    return {"id": id, "brand": brand, "recipe": recipe, "format": fmt, "certification": cert,
            "quantity_remaining": 12}


class CertFix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fg_path = os.path.join(self.tmp, "fg.json")
        self.sales_path = os.path.join(self.tmp, "sales.json")
        beef = ("Benefits by Nature", "Beef with Herbs and Lion's Mane", "SS-876ML")
        with open(self.fg_path, "w") as f:
            json.dump([row("b1", *beef, "Organic"), row("b2", *beef, "Organic"),
                       row("b3", "Benefits by Nature", "Beef with Herbs and Lion's Mane", "SS-473ML", "Organic"),
                       row("c1", "Soma", "Organic Chicken", "SS-750ML", "Organic")], f)
        with open(self.sales_path, "w") as f:
            json.dump([row("s1", *beef, "Organic"), row("s2", "Soma", "Organic Chicken", "SS-750ML", "Organic")], f)
        self.patches = [mock.patch.object(app, "ORGANIC_FG_PATH", self.fg_path),
                        mock.patch.object(app, "ORGANIC_SALES_PATH", self.sales_path),
                        mock.patch.object(app, "load_recipes", return_value=RECIPES),
                        mock.patch.dict(os.environ, {"RECIPE_PASSWORD": "rpw"})]
        for p in self.patches:
            p.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"
            s["recipe_unlocked"] = True

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def certs(self, path):
        return {r["id"]: r["certification"] for r in json.load(open(path))}

    def test_mismatch_is_reported(self):
        self.assertEqual(recipes._cert_mismatch("Beef with Herbs and Lion's Mane"),
                         {"batches": 2, "from": ["Organic"], "to": "Conventional"})
        self.assertIsNone(recipes._cert_mismatch("Organic Chicken"))

    def test_correction_touches_only_that_sku(self):
        r = self.c.post("/api/recipes/Beef%20with%20Herbs%20and%20Lion's%20Mane/apply-certification")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["updated"], {"finished_goods": 2, "sales": 1})
        # The 473 ML format is a different SKU (another recipe card) and stays.
        self.assertEqual(self.certs(self.fg_path),
                         {"b1": "Conventional", "b2": "Conventional", "b3": "Organic", "c1": "Organic"})
        self.assertEqual(self.certs(self.sales_path), {"s1": "Conventional", "s2": "Organic"})
        b1 = next(f for f in json.load(open(self.fg_path)) if f["id"] == "b1")
        self.assertEqual(b1["certification_was"], "Organic")
        self.assertTrue(b1["certification_corrected_at"])
        self.assertIsNone(recipes._cert_mismatch("Beef with Herbs and Lion's Mane"))

    def test_unknown_recipe_and_production_role(self):
        self.assertEqual(self.c.post("/api/recipes/Nope/apply-certification").status_code, 404)
        with self.c.session_transaction() as s:
            s["role"] = "production"
        self.assertEqual(self.c.post("/api/recipes/Organic%20Chicken/apply-certification").status_code, 403)


if __name__ == "__main__":
    unittest.main()

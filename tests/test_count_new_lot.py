"""Finished-goods count: an organic LOT# not on record (app.py, 2026-10-01).

The count lists only lots Soma has stock for. At the go-live count a case can
carry a LOT# Soma shows at zero or never recorded; the count page adds it as
its own item and completing creates stock under that exact LOT#. A LOT# that
is not a ddmmyy date is refused, so a typo cannot invent a lot.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import helpers

KEY = "Soma|Organic Chicken Bone Broth|SS-750ML"


class CountNewLot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fg_path = os.path.join(self.tmp, "fg.json")
        with open(self.fg_path, "w") as f:
            json.dump([{"id": "o1", "brand": "Soma", "recipe": "Organic Chicken Bone Broth",
                        "format": "SS-750ML", "certification": "Organic", "lot": "210927",
                        "quantity_produced": 48, "quantity_remaining": 48,
                        "created_at": "2026-09-22T10:00:00"}], f)
        recipes = {"Organic Chicken Bone Broth": {"brand": "Soma", "format": "SS-750ML",
                                                  "certification": "Organic"}}
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.fg_path),
            mock.patch.object(app, "AUDITS_PATH", os.path.join(self.tmp, "audits.json")),
            mock.patch.object(app, "load_recipes", return_value=recipes),
            mock.patch.object(helpers, "ADJUSTMENTS_PATH", os.path.join(self.tmp, "adj.json")),
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

    def complete(self, results, audit_id="a1"):
        return self.c.post(f"/api/audit/{audit_id}/complete",
                           json={"kind": "fg", "brand": "Soma", "results": results})

    def fg(self):
        return json.load(open(self.fg_path))

    def test_new_lot_becomes_stock_under_that_lot(self):
        r = self.complete({KEY + "@@210927": {"counted": 48}, KEY + "@@280927": {"counted": 24}})
        self.assertEqual(r.status_code, 200, r.get_json())
        new = [f for f in self.fg() if f["lot"] == "280927"]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["quantity_remaining"], new[0]["certification"], new[0]["source"]),
                         (24, "Organic", "audit_baseline"))
        self.assertEqual(next(f for f in self.fg() if f["id"] == "o1")["quantity_remaining"], 48)

    def test_typo_lot_is_refused_and_nothing_applied(self):
        r = self.complete({KEY + "@@210927": {"counted": 36}, KEY + "@@28927": {"counted": 24}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("28927", r.get_json()["error"])
        self.assertEqual([(f["lot"], f["quantity_remaining"]) for f in self.fg()], [("210927", 48)])

    def test_known_lot_and_zero_count_need_no_date_check(self):
        self.assertEqual(app._unknown_lot_typos({KEY + "@@210927": {"counted": 12},
                                                 KEY + "@@oops": {"counted": 0},
                                                 KEY + "@@skipped": {"counted": None}}), [])


if __name__ == "__main__":
    unittest.main()

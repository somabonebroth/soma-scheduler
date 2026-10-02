"""Hand corrections are logged (2026-10-01).

The per-lot "Edit Qty" on Manage Inventory used to overwrite stock silently, so
Organic Lots showed the difference as "unrecorded". Now:
- finished goods (`/api/organic/finished-goods/lot-adjust`): a decrease is a
  `subtract` adjustment with `drained`; an increase is `lot_increase` with
  `added_to` — and Organic Lots + the drift check both balance after either;
- raw materials (PUT /api/organic/raw-materials/<id>): a `raw_lot_edit` record.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import helpers
import ledger
from organic_lots import build_folders

ORG = {"brand": "Soma", "recipe": "Organic Chicken", "format": "SS-750ML", "certification": "Organic"}
KEY = helpers._sku_key("Soma", "Organic Chicken", "SS-750ML")


class LotEdit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = lambda n: os.path.join(self.tmp, n)
        self.paths = {"fg": p("fg.json"), "adj": p("adj.json"), "raw": p("raw.json"), "sales": p("sales.json")}
        json.dump([dict(ORG, id="a", lot="210927", quantity_produced=240, quantity_remaining=240,
                        created_at="2026-09-01T10:00:00")], open(self.paths["fg"], "w"))
        json.dump([{"id": "rm1", "item": "Organic Bones", "unit": "kg", "supplier_lot": "B1",
                    "quantity": 50, "remaining": 50}], open(self.paths["raw"], "w"))
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.paths["fg"]),
            mock.patch.object(app, "ORGANIC_RAW_PATH", self.paths["raw"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.paths["sales"]),
            mock.patch.object(helpers, "ADJUSTMENTS_PATH", self.paths["adj"]),
            mock.patch.object(ledger, "ADJUSTMENTS_PATH", self.paths["adj"]),
            mock.patch.object(ledger, "_reset_cutover_date", return_value=None),
        ]
        for x in self.patches:
            x.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def adj(self):
        return json.load(open(self.paths["adj"])) if os.path.exists(self.paths["adj"]) else []

    def edit_fg(self, n, reason="Breakage"):
        r = self.c.post("/api/organic/finished-goods/lot-adjust",
                        json={"sku_key": KEY, "lot": "210927", "new_remaining": n, "reason": reason})
        self.assertEqual(r.status_code, 200, r.get_json())

    def folder_sku(self):
        fg = json.load(open(self.paths["fg"]))
        return build_folders(fg, [], [], self.adj(), [], set())[0]["skus"][0]

    def drift(self):
        return ledger.compute_fg_reconciliation()["entries"][0]["drift"]

    def test_decrease_is_a_recorded_reduction(self):
        self.edit_fg(228)
        a = self.adj()[-1]
        self.assertEqual((a["kind"], a["reason"], a["drained"]),
                         ("subtract", "Breakage", [{"fg_id": "a", "lot": "210927", "quantity": 12}]))
        s = self.folder_sku()
        self.assertEqual((s["reduced"], s["held"], s["unrecorded"]), (12, 228, 0))
        self.assertEqual(self.drift(), 0)

    def test_increase_is_recorded_as_added(self):
        self.edit_fg(252, reason="Recount")
        a = self.adj()[-1]
        self.assertEqual((a["kind"], a["added_to"][0]["quantity"]), ("lot_increase", 12))
        s = self.folder_sku()
        self.assertEqual((s["added"], s["held"], s["unrecorded"]), (240 + 12, 252, 0))
        self.assertEqual(self.drift(), 0)

    def test_no_change_writes_nothing(self):
        self.edit_fg(240)
        self.assertEqual(self.adj(), [])

    def test_raw_edit_is_logged(self):
        r = self.c.put("/api/organic/raw-materials/rm1", json={"remaining": 47.5, "reason": "Spillage"})
        self.assertEqual(r.status_code, 200)
        a = self.adj()[-1]
        self.assertEqual((a["kind"], a["raw_material_id"], a["from"], a["to"], a["reason"]),
                         ("raw_lot_edit", "rm1", 50, 47.5, "Spillage"))


if __name__ == "__main__":
    unittest.main()

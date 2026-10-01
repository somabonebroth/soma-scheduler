"""The startup backfill never rewrites an existing batch (app.py, 2026-10-01).

_backfill_organic_finished_goods runs on every boot. It used to re-run the
full completion for every past day, resetting each batch's remaining to
produced - sold — so every deploy undid stock-count shortages, breakage and
hand corrections. It now only creates a missing record.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app

FG_ID = "fg_2026-09-21_1_K1"


class BackfillCreateOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "checklists"))
        self.p = {n: os.path.join(self.tmp, n + ".json") for n in ("runs", "fg", "sales", "raw")}
        with open(self.p["runs"], "w") as f:
            json.dump([{"id": "run1", "recipe": "Organic Chicken", "brand": "Soma", "vessel": "K1",
                        "week_id": "2026-09-21", "day_idx": 0, "status": "completed", "amount_produced": 240,
                        "ingredients_used": [{"item": "x", "quantity_used": 1, "raw_material_id": "r1"}]}], f)
        with open(os.path.join(self.tmp, "checklists", "2026-09-21_day1.json"), "w") as f:
            json.dump({"produced": {"K1": 240}}, f)
        with open(self.p["sales"], "w") as f:
            json.dump([{"id": "s1", "quantity": 24, "lots": [{"lot": "210927", "quantity": 24, "fg_ids": [FG_ID],
                        "breakdown": [{"fg_id": FG_ID, "quantity": 24}]}]}], f)
        with open(self.p["raw"], "w") as f:
            json.dump([], f)
        recipes = {"Organic Chicken": {"brand": "Soma", "format": "SS-750ML", "certification": "Organic"}}
        self.patches = [
            mock.patch.object(app, "ORGANIC_RUNS_PATH", self.p["runs"]),
            mock.patch.object(app, "ORGANIC_FG_PATH", self.p["fg"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.p["sales"]),
            mock.patch.object(app, "ORGANIC_RAW_PATH", self.p["raw"]),
            mock.patch.object(app, "CHECKLISTS_DIR", os.path.join(self.tmp, "checklists")),
            mock.patch.object(app, "load_recipes", return_value=recipes),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def fg(self):
        return json.load(open(self.p["fg"]))

    def test_restart_keeps_a_recorded_reduction(self):
        with open(self.p["fg"], "w") as f:
            json.dump([{"id": FG_ID, "run_id": "run1", "brand": "Soma", "recipe": "Organic Chicken",
                        "format": "SS-750ML", "certification": "Organic", "lot": "210927",
                        "quantity_produced": 240, "quantity_remaining": 204,
                        "last_adjusted_at": "2026-10-01T09:00:00"}], f)
        app._backfill_organic_finished_goods()
        row = self.fg()[0]
        self.assertEqual(row["quantity_remaining"], 204)          # was reset to 216 before the fix
        self.assertEqual(row["last_adjusted_at"], "2026-10-01T09:00:00")

    def test_restart_still_creates_a_missing_record(self):
        with open(self.p["fg"], "w") as f:
            json.dump([], f)
        app._backfill_organic_finished_goods()
        self.assertEqual([(r["id"], r["quantity_produced"], r["quantity_remaining"]) for r in self.fg()],
                         [(FG_ID, 240, 216)])

    def test_tablet_resave_still_updates(self):
        # The ordinary save path is unchanged: a corrected jar count updates the batch.
        with open(self.p["fg"], "w") as f:
            json.dump([{"id": FG_ID, "run_id": "run1", "brand": "Soma", "recipe": "Organic Chicken",
                        "format": "SS-750ML", "certification": "Organic", "lot": "210927",
                        "quantity_produced": 240, "quantity_remaining": 216}], f)
        app._check_organic_completion("2026-09-21", 1, {"produced": {"K1": 252}})
        self.assertEqual((self.fg()[0]["quantity_produced"], self.fg()[0]["quantity_remaining"]), (252, 228))


if __name__ == "__main__":
    unittest.main()

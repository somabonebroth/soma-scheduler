"""Organic batches hold full cases only (app._complete_organic_run, 2026-10-01).

The kitchen enters every jar. Organic stock takes full cases of 12; the rest
is recorded on the batch as loose jars (sold as hot cups, not certified) so
the Organic Lots folder balances against the kitchen's count. Non-organic
batches keep every jar.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
from organic_lots import build_folders


class CaseRounding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = {n: os.path.join(self.tmp, n + ".json") for n in ("runs", "fg", "sales", "raw")}
        runs = [{"id": "r_org", "recipe": "Organic Chicken", "brand": "Soma", "vessel": "K1",
                 "week_id": "2026-09-28", "day_idx": 0, "status": "scheduled"},
                {"id": "r_conv", "recipe": "Chicken", "brand": "Soma", "vessel": "K2",
                 "week_id": "2026-09-28", "day_idx": 0, "status": "scheduled"}]
        for n, data in (("runs", runs), ("fg", []), ("sales", []), ("raw", [])):
            with open(self.p[n], "w") as f:
                json.dump(data, f)
        recipes = {"Organic Chicken": {"brand": "Soma", "format": "SS-750ML", "certification": "Organic"},
                   "Chicken": {"brand": "Soma", "format": "SS-750ML", "certification": "Conventional"}}
        self.patches = [
            mock.patch.object(app, "ORGANIC_RUNS_PATH", self.p["runs"]),
            mock.patch.object(app, "ORGANIC_FG_PATH", self.p["fg"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.p["sales"]),
            mock.patch.object(app, "ORGANIC_RAW_PATH", self.p["raw"]),
            mock.patch.object(app, "load_recipes", return_value=recipes),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def save(self, k1, k2=0):
        app._check_organic_completion("2026-09-28", 1, {"produced": {"K1": k1, "K2": k2}})
        return {f["vessel"]: f for f in json.load(open(self.p["fg"]))}

    def test_organic_keeps_full_cases_and_records_the_rest(self):
        fg = self.save(245, 245)
        org, conv = fg["K1"], fg["K2"]
        self.assertEqual((org["quantity_produced"], org["quantity_remaining"], org["jars_counted"], org["loose_jars"]),
                         (240, 240, 245, 5))
        self.assertEqual((conv["quantity_produced"], conv["quantity_remaining"]), (245, 245))
        self.assertNotIn("loose_jars", conv)
        run = next(r for r in json.load(open(self.p["runs"])) if r["id"] == "r_org")
        self.assertEqual(run["amount_produced"], 245)   # the batch made 245; raw is charged per batch

    def test_under_a_case_is_all_loose(self):
        org = self.save(10)["K1"]
        self.assertEqual((org["quantity_produced"], org["loose_jars"]), (0, 10))

    def test_resave_keeps_sales(self):
        self.save(245)
        with open(self.p["sales"], "w") as f:
            json.dump([{"id": "s1", "quantity": 24, "lots": [{"lot": "280927", "quantity": 24,
                        "breakdown": [{"fg_id": "fg_2026-09-28_1_K1", "quantity": 24}]}]}], f)
        org = self.save(250)["K1"]
        self.assertEqual((org["quantity_produced"], org["quantity_remaining"], org["loose_jars"]), (240, 216, 10))

    def test_folder_balances_against_the_kitchen_count(self):
        fg = list(self.save(245).values())
        folder = build_folders(fg, json.load(open(self.p["runs"])), [], [], [], set())[0]
        s = folder["skus"][0]
        self.assertEqual((s["made"], s["hot_cups"], s["held"], s["unrecorded"]), (240, 5, 240, 0))


if __name__ == "__main__":
    unittest.main()

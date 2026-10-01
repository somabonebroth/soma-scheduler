"""Re-apply a count a restart undid (app.py, 2026-10-01).

A count shortage drained a production batch; a restart (before the
create-only backfill fix) put the jars back. The repair takes the lot back to
what the count left it at, less what was sold since — removing only, once.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import app
import helpers
from organic_lots import build_folders

KEY = "Soma|Organic Chicken Bone Broth|SS-750ML"


class ReapplyCount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = {n: os.path.join(self.tmp, n + ".json") for n in ("fg", "sales", "audits", "adj")}
        with open(self.p["fg"], "w") as f:
            json.dump([{"id": "b1", "run_id": "run1", "brand": "Soma", "recipe": "Organic Chicken Bone Broth",
                        "format": "SS-750ML", "certification": "Organic", "lot": "210927",
                        "quantity_produced": 240, "quantity_remaining": 240, "created_at": "2026-09-22T10:00:00"}], f)
        for n in ("sales", "adj"):
            with open(self.p[n], "w") as f:
                json.dump([], f)
        recipes = {"Organic Chicken Bone Broth": {"brand": "Soma", "format": "SS-750ML", "certification": "Organic"}}
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.p["fg"]),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.p["sales"]),
            mock.patch.object(app, "AUDITS_PATH", self.p["audits"]),
            mock.patch.object(app, "ADJUSTMENTS_PATH", self.p["adj"]),
            mock.patch.object(helpers, "ADJUSTMENTS_PATH", self.p["adj"]),
            mock.patch.object(app, "load_recipes", return_value=recipes),
        ]
        for p in self.patches:
            p.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"
        # The count: shelf has 216 (18 cases), Soma had 240 → drained 24.
        r = self.c.post("/api/audit/a1/complete", json={"kind": "fg", "brand": "Soma",
                                                        "results": {KEY + "@@210927": {"counted": 216}}})
        assert r.status_code == 200, r.get_json()
        # A restart (old behaviour) put the 24 back.
        self.set_held(240)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def set_held(self, n):
        fg = json.load(open(self.p["fg"]))
        fg[0]["quantity_remaining"] = n
        json.dump(fg, open(self.p["fg"], "w"))

    def held(self):
        return json.load(open(self.p["fg"]))[0]["quantity_remaining"]

    def test_plan_then_apply_then_nothing(self):
        plan = self.c.get("/admin/reapply-count/run").get_json()["rows"]
        self.assertEqual([(r["lot"], r["counted"], r["holds"], r["remove"]) for r in plan], [("210927", 216, 240, 24)])
        self.assertEqual(self.held(), 240)                      # GET changes nothing
        self.assertEqual(self.c.post("/admin/reapply-count/run").get_json()["reapplied"][0]["removed"], 24)
        self.assertEqual(self.held(), 216)
        self.assertEqual(self.c.get("/admin/reapply-count/run").get_json()["rows"][0]["remove"], 0)
        self.assertEqual(self.c.post("/admin/reapply-count/run").get_json()["reapplied"], [])
        # Organic Lots now adds up: no "unrecorded" jars on the lot.
        folder = build_folders(json.load(open(self.p["fg"])), [], [], json.load(open(self.p["adj"])), [], set())[0]
        self.assertEqual(folder["skus"][0]["unrecorded"], 0)

    def test_a_sale_after_the_count_is_respected(self):
        sales = [{"id": "s1", "created_at": datetime.now().isoformat() + "Z", "quantity": 12,
                  "lots": [{"lot": "210927", "quantity": 12, "breakdown": [{"fg_id": "b1", "quantity": 12}]}]}]
        json.dump(sales, open(self.p["sales"], "w"))
        self.set_held(228)  # 240 restored by the restart, minus the 12 sold
        row = self.c.get("/admin/reapply-count/run").get_json()["rows"][0]
        self.assertEqual((row["should_hold"], row["remove"]), (204, 24))

    def test_never_adds(self):
        self.set_held(200)  # below what the count left: not this repair's business
        self.assertEqual(self.c.get("/admin/reapply-count/run").get_json()["rows"][0]["remove"], 0)
        self.c.post("/admin/reapply-count/run")
        self.assertEqual(self.held(), 200)


if __name__ == "__main__":
    unittest.main()

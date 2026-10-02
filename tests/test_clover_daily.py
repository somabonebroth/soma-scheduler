"""Daily Clover import (app._clover_commit_for_day, 2026-10-02).

Jar SKUs become sale rows and leave FG; hot cups, creams and other non-jar
items are money only (clover_days.json) and reach the Sales by channel chart;
a re-run never deducts twice; daily and weekly imports never overlap.
"""
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import app
import helpers

PLAIN = "Soma|Chicken Bone Broth|SS-750ML"
DAY = "2026-09-29"            # a Tuesday; week of 2026-09-28


def preview(jars=4, jar_rev=60.0, unparseable=()):
    return {
        "day_id": DAY, "order_count": 9, "line_item_count": 12,
        "matched": [{"sku": "SOMA-CHICKEN-SS750", "brand": "Soma",
                     "recipe": "Chicken Bone Broth", "format": "SS-750ML",
                     "soma_key": PLAIN, "quantity": jars, "revenue": jar_rev,
                     "exists_in_soma": True, "order_ids": ["o1"]}],
        "unparseable": list(unparseable),
        "skipped_no_sku": [{"order_id": "o2", "name": "Hot Cup", "quantity": 3, "revenue": 15.0},
                           {"order_id": "o3", "name": "Hot Cup", "quantity": 1, "revenue": 5.0}],
        "skipped_other_brands": [{"sku": "CREAM-1", "quantity": 2, "revenue": 18.0,
                                  "order_ids": ["o4"]}],
    }


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fg_path = os.path.join(self.tmp, "fg.json")
        self.sales_path = os.path.join(self.tmp, "sales.json")
        self.days_path = os.path.join(self.tmp, "clover_days.json")
        with open(self.fg_path, "w") as f:
            json.dump([{"id": "p1", "brand": "Soma", "recipe": "Chicken Bone Broth",
                        "format": "SS-750ML", "certification": "Conventional", "lot": "150927",
                        "quantity_produced": 24, "quantity_remaining": 24,
                        "created_at": "2026-09-16"}], f)
        self.preview = preview()
        self.patches = [
            mock.patch.object(app, "ORGANIC_FG_PATH", self.fg_path),
            mock.patch.object(app, "ORGANIC_SALES_PATH", self.sales_path),
            mock.patch.object(app, "CLOVER_DAYS_PATH", self.days_path),
            mock.patch.object(app, "RECIPES_PATH", os.path.join(self.tmp, "r.json")),
            mock.patch.object(helpers, "ORGANIC_CONTACTS_PATH", os.path.join(self.tmp, "c.json")),
            mock.patch.object(app, "_toronto_today", return_value=date(2026, 10, 2)),
            mock.patch.object(app, "_clover_env", return_value=("t", "m", None)),
            mock.patch.object(app.clover_importer, "preview_day",
                              side_effect=lambda *a, **k: self.preview),
            mock.patch.object(app, "_load_buyers", return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def load(self, path):
        with open(path) as f:
            return json.load(f)

    def test_jars_deduct_other_items_are_money_only(self):
        body, status = app._clover_commit_for_day(DAY)
        self.assertEqual(status, 200)
        sales = self.load(self.sales_path)
        self.assertEqual(len(sales), 1)
        self.assertEqual((sales[0]["day_id"], sales[0]["week_id"], sales[0]["quantity"],
                          sales[0]["line_total"], sales[0]["sale_date"]),
                         (DAY, "2026-09-28", 4, 60.0, DAY))
        self.assertEqual(self.load(self.fg_path)[0]["quantity_remaining"], 20)
        day = self.load(self.days_path)[0]
        self.assertEqual(day["other_revenue"], 38.0)
        self.assertEqual(day["extra_revenue"], 38.0)
        self.assertEqual(day["total_revenue"], 98.0)
        hot = next(i for i in day["other_items"] if i["name"] == "Hot Cup")
        self.assertEqual((hot["quantity"], hot["revenue"]), (4, 20.0))

    def test_rerun_never_deducts_twice(self):
        app._clover_commit_for_day(DAY)
        body, _ = app._clover_commit_for_day(DAY)
        self.assertEqual(body["skipped_count"], 1)
        self.assertEqual(len(self.load(self.sales_path)), 1)
        self.assertEqual(self.load(self.fg_path)[0]["quantity_remaining"], 20)
        self.assertEqual(len(self.load(self.days_path)), 1)

    def test_short_stock_money_still_counted(self):
        self.preview = preview(jars=30, jar_rev=450.0)
        body, _ = app._clover_commit_for_day(DAY)
        self.assertEqual(body["created_count"], 0)
        self.assertEqual(body["error_count"], 1)
        self.assertEqual(self.load(self.fg_path)[0]["quantity_remaining"], 24)
        day = self.load(self.days_path)[0]
        self.assertEqual(day["unrecorded_revenue"], 450.0)
        self.assertEqual(day["extra_revenue"], 488.0)
        self.assertEqual(day["total_revenue"], 488.0)

    def test_today_refused(self):
        _, status = app._clover_commit_for_day("2026-10-02")
        self.assertEqual(status, 400)

    def test_week_imported_weekly_is_left_alone(self):
        with open(self.sales_path, "w") as f:
            json.dump([{"id": "w", "channel": "clover", "week_id": "2026-09-28",
                        "sku_key": PLAIN, "quantity": 9}], f)
        body, status = app._clover_commit_for_day(DAY)
        self.assertEqual(status, 200)
        self.assertIn("weekly", body["message"])
        self.assertFalse(os.path.exists(self.days_path))

    def test_weekly_refuses_a_week_with_daily_imports(self):
        app._clover_commit_for_day(DAY)
        body, status = app._clover_commit_for_week("2026-09-28")
        self.assertEqual(status, 200)
        self.assertEqual(body["daily_imports"], [DAY])
        self.assertEqual(len(self.load(self.sales_path)), 1)

    def test_sales_by_channel_includes_other_items(self):
        app._clover_commit_for_day(DAY)
        c = app.app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"
        r = c.get("/api/analytics/sales-by-channel?grain=month&end=2026-09&n=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["periods"][0]["revenue"]["soma"], 98.0)
        r = c.get("/api/analytics/sales-by-buyer")
        clover = next(b for b in r.get_json()["buyers"] if b["buyer"] == "SOMA (Clover)")
        self.assertEqual(clover["revenue"], 98.0)


if __name__ == "__main__":
    unittest.main()

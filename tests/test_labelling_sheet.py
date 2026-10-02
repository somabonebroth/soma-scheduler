"""The 4x6 labelling sheet (daily_brief._labelling_sheet, 2026-10-01).

Shapes section 1 of the Daily Summary (_production_section) for the floor:
jars counted on D are labelled on D+1; LOT#, stamp and counts must match
section 1 exactly; organic leftovers go to hot cups with no organic label.
"""
import unittest
from datetime import date

import app  # noqa: F401 — must load before its blueprints (circular import)
from daily_brief import _labelling_sheet, _lot_blocks

ROWS = [
    {"item": "SOMA-Organic Chicken-SS-750ML", "brand": "SOMA", "recipe": "Organic Chicken",
     "format": "SS-750ML", "certification": "Organic", "lot": "300927", "quantity": 96, "loose_jars": 7},
    {"item": "SOMA-Beef-SS-876ML", "brand": "Soma", "recipe": "Beef", "format": "SS-876ML",
     "certification": "", "lot": "300927", "quantity": 41, "loose_jars": 0},
    {"item": "BBN-Beef with Herbs-SS-750ML", "brand": "Benefits by Nature", "recipe": "Beef with Herbs",
     "format": "SS-750ML", "certification": "", "lot": "290927", "quantity": 24},
]


def prod(rows):
    return {"rows": rows, "lots": _lot_blocks(rows)}


class LabellingSheet(unittest.TestCase):
    def test_counted_today_is_labelled_tomorrow(self):
        s = _labelling_sheet(prod(ROWS), date(2026, 9, 30))
        self.assertEqual(s["label_day"], "Thursday 01 October")
        self.assertEqual(s["counted_label"], "Wed 30 Sep")

    def test_lots_carry_the_stamp_and_best_before(self):
        s = _labelling_sheet(prod(ROWS), date(2026, 9, 30))
        first = s["lots"][0]
        self.assertEqual(first["lot"], "300927")
        self.assertEqual(first["stamp"], list("729003"))     # reversed for the face-down type
        self.assertEqual(first["best_before"], "30/09/2027")
        self.assertTrue(s["multi_lot"])

    def test_cases_loose_and_hot_cups(self):
        s = _labelling_sheet(prod(ROWS), date(2026, 9, 30))
        by = {r["name"]: r for r in s["rows"]}
        org = by["Organic Chicken"]
        self.assertEqual(org["format"], "SS-750ML")
        self.assertTrue(org["organic"])
        self.assertEqual((org["cases"], org["loose"], org["hot_cups"]), (8, 0, 7))
        beef = by["Beef"]                       # Soma brand dropped, any case
        self.assertEqual((beef["cases"], beef["loose"]), (3, 5))
        self.assertIn("Benefits by Nature · Beef with Herbs", by)  # other brands lead
        self.assertEqual((s["total_jars"], s["total_cases"], s["total_loose"], s["hot_cups"]), (161, 13, 5, 7))

    def test_nothing_counted(self):
        s = _labelling_sheet(prod([]), date(2026, 9, 30))
        self.assertEqual((s["rows"], s["lots"], s["total_jars"]), ([], [], 0))

    def test_non_date_lot_has_no_best_before(self):
        rows = [dict(ROWS[1], lot="BL-300926")]
        s = _labelling_sheet(prod(rows), date(2026, 9, 30))
        self.assertEqual(s["lots"][0]["best_before"], "")


if __name__ == "__main__":
    unittest.main()

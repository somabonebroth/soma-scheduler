"""Daily Summary "Print ahead" (daily_brief._upcoming_labels, 2026-10-01).

Section 1 for date D = jars counted on D, labelled D+1. The next two labelling
days are D+2 and D+3, whose jars were STARTED on D and D+1 — so products come
from those days' schedules and the LOT# is start + 365.
"""
import unittest
from datetime import date

import app  # noqa: F401 — must load before its blueprints (circular import)
from daily_brief import _upcoming_labels

RECIPES = {
    "Organic Chicken": {"brand": "Soma", "format": "SS-750ML", "certification": "Organic"},
    "Beef": {"brand": "Soma", "format": "SS-876ML", "certification": ""},
}
# Week of Mon 2026-09-28: Thu (3) and Fri (4) scheduled; Sat empty.
SCHED = {"2026-09-28": {"schedule": {
    "3": {"K1": "Organic Chicken", "K2": "Organic Chicken", "K3": "Beef"},
    "4": {"K1": "Beef", "115L": "Unknown recipe"},
}}}


def load(week_id):
    return SCHED.get(week_id)


class PrintAhead(unittest.TestCase):
    def test_viewing_thursday_gives_saturday_and_sunday(self):
        days = _upcoming_labels(date(2026, 10, 1), load_schedule=load, recipes=RECIPES)
        self.assertEqual([d["label_day"] for d in days], ["2026-10-03", "2026-10-04"])
        self.assertEqual([d["batched_on"] for d in days], ["2026-10-01", "2026-10-02"])
        self.assertEqual([d["lot"] for d in days], ["011027", "021027"])

    def test_one_row_per_product_with_its_vessels(self):
        sat = _upcoming_labels(date(2026, 10, 1), load_schedule=load, recipes=RECIPES)[0]
        by = {r["recipe"]: r for r in sat["rows"]}
        self.assertEqual(by["Organic Chicken"]["vessels"], ["K1", "K2"])
        self.assertEqual(by["Organic Chicken"]["certification"], "Organic")
        self.assertEqual(by["Beef"]["format"], "SS-876ML")

    def test_unknown_recipe_and_empty_day_and_missing_week(self):
        sun = _upcoming_labels(date(2026, 10, 1), load_schedule=load, recipes=RECIPES)[1]
        self.assertEqual([r["recipe"] for r in sun["rows"]], ["Beef"])
        # Sat batch -> Mon labels, Sun batch crosses into a week with no schedule.
        days = _upcoming_labels(date(2026, 10, 3), load_schedule=load, recipes=RECIPES)
        self.assertEqual([d["rows"] for d in days], [[], []])


if __name__ == "__main__":
    unittest.main()

"""Daily Summary "used more than received" flag (daily_brief, 2026-10-02).

One Check per ingredient (not one red Issue per batch line), plain wording,
a Record delivery link dated on the batch day, and nothing for ingredients
made in-house (the Adjunct unit or the word in the name).
"""
import unittest
from datetime import date
from urllib.parse import parse_qs, urlparse

import daily_brief

ON = date(2026, 9, 30)


def ex(recipe, ingredient, shortfall, unit):
    return {"recipe": recipe, "vessel": "K1", "ingredient": ingredient,
            "shortfall": shortfall, "unit": unit}


class MissingDelivery(unittest.TestCase):
    def test_grouped_per_ingredient_with_links(self):
        out = daily_brief._missing_delivery_issues([
            ex("Chicken Bone Broth", "Organic Chicken Bones", 8, "kg"),
            ex("Chicken Soup Base", "Organic Chicken Bones", 4.5, "kg"),
            ex("Chicken Bone Broth", "Grey Salt", 0.25, "kg"),
        ], ON)
        self.assertEqual(len(out), 2)
        bones = next(i for i in out if i["text"].startswith("Organic Chicken Bones"))
        self.assertEqual(bones["level"], "medium")
        self.assertIn("12.5 kg more used than was ever received", bones["text"])
        self.assertIn("Chicken Bone Broth, Chicken Soup Base", bones["text"])
        q = parse_qs(urlparse(bones["links"][0]["href"]).query)
        self.assertEqual(q, {"item": ["Organic Chicken Bones"], "date": ["2026-09-30"]})
        self.assertEqual(bones["links"][1]["href"], "/admin/reconcile-raw")

    def test_in_house_left_out(self):
        out = daily_brief._missing_delivery_issues([
            ex("Beef Bone Broth", "Organic Garlic Ginger", 1, "Adjunct"),
            ex("Beef Bone Broth", "Garlic-Ginger Adjunct", 2, "g"),
        ], ON)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()

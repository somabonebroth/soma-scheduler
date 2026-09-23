"""Ingredient lot rename (raw_materials.py, 2026-09-23).

After recipe ingredients were renamed to "Organic X", lots received as "X"
matched no recipe. The plan renames every lot of an orphaned name that has an
"Organic" twin in the recipes, and leaves alone any name a recipe still uses.
"""
import unittest

import app  # noqa: F401 — must load before its blueprints (circular import)
from raw_materials import _ingredient_rename_plan


def recipe(*names):
    return {"kettle_overnight": [{"name": n, "amount": 1, "unit": "kg"} for n in names]}


class Plan(unittest.TestCase):
    def test_orphan_with_organic_twin_is_renamed(self):
        mats = [{"item": "Carrots", "unit": "kg", "remaining": 3},
                {"item": "carrots ", "unit": "kg", "remaining": 0},
                {"item": "Organic Carrots", "unit": "kg", "remaining": 1}]
        plan = _ingredient_rename_plan(mats, {"A": recipe("Organic Carrots")}, [])
        self.assertEqual(len(plan["renames"]), 1)
        r = plan["renames"][0]
        self.assertEqual((r["to"], r["lots"], r["in_stock"]), ("Organic Carrots", 2, 3))

    def test_name_still_used_by_a_recipe_is_left_alone(self):
        mats = [{"item": "Carrots", "unit": "kg", "remaining": 3}]
        recipes = {"A": recipe("Organic Carrots"), "B": recipe("Carrots")}
        plan = _ingredient_rename_plan(mats, recipes, [])
        self.assertEqual(plan, {"renames": [], "unmatched": []})

    def test_archived_recipe_does_not_keep_a_name(self):
        old = dict(recipe("Carrots"), archived=True)
        mats = [{"item": "Carrots", "unit": "kg", "remaining": 3}]
        plan = _ingredient_rename_plan(mats, {"A": recipe("Organic Carrots"), "B": old}, [])
        self.assertEqual(len(plan["renames"]), 1)

    def test_orphan_without_twin_is_reported_not_renamed(self):
        mats = [{"item": "Parsley", "unit": "kg", "remaining": 2}]
        plan = _ingredient_rename_plan(mats, {"A": recipe("Organic Carrots")}, [])
        self.assertEqual(plan["renames"], [])
        self.assertEqual(plan["unmatched"][0]["name"], "Parsley")

    def test_custom_item_counts_as_known(self):
        mats = [{"item": "Jars", "unit": "ea", "remaining": 2}]
        plan = _ingredient_rename_plan(mats, {}, [{"name": "Jars", "unit": "ea"}])
        self.assertEqual(plan, {"renames": [], "unmatched": []})


if __name__ == "__main__":
    unittest.main()

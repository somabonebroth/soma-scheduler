"""Raw materials mass balance (audit_tools.raw_mass_balance, 2026-10-15).

Every raw lot received in the period, followed to today:
received = used in certified + used in non-certified + counts & corrections + on hand.
"""
import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SECRET_KEY", "test")
import app  # noqa: E402,F401
from audit_tools import raw_mass_balance  # noqa: E402

RECIPES = {"Org Chicken": {"certification": "Organic"}, "Plain Chicken": {"certification": ""}}


def lot(i, item, qty, rem, date, **kw):
    return dict({"id": i, "item": item, "unit": "kg", "quantity": qty, "remaining": rem,
                 "date_received": date, "supplier": "Farm", "supplier_lot": "L" + i}, **kw)


def run(recipe, week, day, lines):
    return {"status": "completed", "recipe": recipe, "vessel": "K1", "week_id": week, "day_idx": day,
            "ingredients_used": lines}


def use(rid, item, qty):
    return {"raw_material_id": rid, "item": item, "quantity_used": qty, "unit": "kg", "supplier_lot": "x"}


class Balance(unittest.TestCase):
    def test_split_by_certification_and_counts(self):
        mats = [lot("a", "Organic Carrots", 100, 8, "2026-09-02"),
                lot("b", "Organic Carrots", 50, 50, "2026-08-01")]          # outside window
        runs = [run("Org Chicken", "2026-09-07", 0, [use("a", "Organic Carrots", 60)]),
                run("Plain Chicken", "2026-09-07", 1, [use("a", "Organic Carrots", 30)])]
        out = raw_mass_balance(mats, runs, [], RECIPES, "2026-09-01", "2026-09-30")
        (g,) = out["ingredients"]
        self.assertEqual((g["received"], g["certified"], g["non_certified"], g["on_hand"], g["counts"]),
                         (100, 60, 30, 8, 2))
        self.assertEqual(len(g["lots"]), 1)
        self.assertEqual(out["flags"], [])

    def test_lot_added_by_a_count_is_not_received(self):
        mats = [lot("c", "Organic Onion", 5, 5, "2026-09-03", adjustment=True)]
        (g,) = raw_mass_balance(mats, [], [], RECIPES, "2026-09-01", "2026-09-30")["ingredients"]
        self.assertEqual((g["received"], g["on_hand"], g["counts"]), (0, 5, -5))

    def test_flags_on_certified_batches(self):
        mats = [lot("d", "Lemongrass", 10, 9, "2026-09-02")]
        runs = [run("Org Chicken", "2026-09-07", 0, [
            use("d", "Lemongrass", 1),
            {"item": "Organic Celery", "supplier_lot": "INSUFFICIENT_STOCK", "quantity_used": 3, "unit": "kg"}])]
        out = raw_mass_balance(mats, runs, [], RECIPES, "2026-09-01", "2026-09-30")
        self.assertEqual(sorted(f["kind"] for f in out["flags"]), ["no_stock", "not_organic"])
        self.assertTrue(out["ingredients"][0]["not_organic_in_certified"])

    def test_fg_certification_wins_over_recipe(self):
        mats = [lot("e", "Organic Bones", 10, 0, "2026-09-02")]
        r = run("Plain Chicken", "2026-09-07", 0, [use("e", "Organic Bones", 10)])
        r.update(finish_week_id="2026-09-07", finish_day_idx=1)
        fg = [{"id": "fg_2026-09-07_1_K1", "certification": "Organic"}]
        (g,) = raw_mass_balance(mats, [r], fg, RECIPES, "2026-09-01", "2026-09-30")["ingredients"]
        self.assertEqual((g["certified"], g["non_certified"]), (10, 0))


class OnlyCertifiedRecipeIngredients(unittest.TestCase):
    def test_filter_keeps_recipe_ingredients_and_certified_usage(self):
        from audit_tools import certified_recipe_ingredients
        recipes = {"Org Chicken": {"certification": "Organic", "kettle_overnight": [
                       {"name": "Organic Carrots", "amount": 1, "unit": "kg"}]},
                   "Beef": {"certification": "", "kettle_overnight": [
                       {"name": "Beef Bones", "amount": 1, "unit": "kg"}]}}
        only = certified_recipe_ingredients(recipes)
        self.assertEqual(only, {"organic carrots"})
        mats = [lot("a", "Organic Carrots", 10, 10, "2026-09-02"),
                lot("b", "Beef Bones", 10, 10, "2026-09-02"),
                lot("c", "Lemongrass", 5, 4, "2026-09-02")]
        runs = [run("Org Chicken", "2026-09-07", 0, [use("c", "Lemongrass", 1)])]
        out = raw_mass_balance(mats, runs, [], recipes, "2026-09-01", "2026-09-30", only=only)
        self.assertEqual([g["item"] for g in out["ingredients"]], ["Lemongrass", "Organic Carrots"])


class Route(unittest.TestCase):
    def test_bad_dates_and_role(self):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"
        self.assertEqual(c.get("/api/organic/raw-mass-balance?from=x&to=y").status_code, 400)
        self.assertEqual(c.get("/api/organic/raw-mass-balance?from=2026-09-01&to=2026-09-30").status_code, 200)
        with c.session_transaction() as s:
            s["role"] = "production"
        self.assertEqual(c.get("/api/organic/raw-mass-balance?from=2026-09-01&to=2026-09-30").status_code, 403)


if __name__ == "__main__":
    unittest.main()

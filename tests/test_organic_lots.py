"""Organic Lots folders (organic_lots.py, 2026-09-30).

One folder per organic LOT#: invoices in, jars made, sales and reductions out,
jars held. Read-only — built from the existing records, joined on fg_id.
"""
import unittest

import app  # noqa: F401 — must load before its blueprints (circular import)
from organic_lots import build_folders

ORG = {"brand": "Soma", "recipe": "Organic Chicken", "format": "SS-750ML", "certification": "Organic"}
KEY = "SOMA|ORGANIC CHICKEN|SS-750ML"


def fg(id, lot, made, held, run_id=None, **kw):
    return dict(ORG, id=id, lot=lot, quantity_produced=made, quantity_remaining=held,
                run_id=run_id, created_at="2026-09-01T10:00:00", **kw)


RUN = {"id": "run1", "recipe": "Organic Chicken", "vessel": "K1", "week_id": "2026-09-21", "day_idx": 0,
       "ingredients_used": [
           {"item": "Organic Bones", "supplier_lot": "B1", "quantity_used": 40, "unit": "kg",
            "raw_material_id": "rm_bulk_20260915090000_001", "supplier": "Farm", "date_received": "2026-09-15"},
           {"item": "Organic Carrots", "supplier_lot": "C1", "quantity_used": 5, "unit": "kg",
            "raw_material_id": "rm_bulk_20260915090000_002", "supplier": "Farm", "date_received": "2026-09-15"},
           {"item": "Organic Onions", "supplier_lot": "BL-010926", "quantity_used": 2, "unit": "kg",
            "raw_material_id": "base1", "supplier": "(physical count)", "date_received": "2026-09-01"},
           {"item": "Organic Garlic", "supplier_lot": "INSUFFICIENT_STOCK", "quantity_used": 1, "unit": "kg"}]}
MATS = [{"id": "rm_bulk_20260915090000_000", "item": "Organic Parsley"},
        {"id": "rm_bulk_20260915090000_001", "item": "Organic Bones"},
        {"id": "rm_bulk_20260915090000_002", "item": "Organic Carrots"},
        {"id": "base1", "item": "Organic Onions", "migration_baseline": True}]


def sale(id, order, qty, fg_id, lot, **kw):
    return dict(ORG, id=id, order_id=order, quantity=qty, buyer="Nature's Emporium",
                sale_date="2026-09-25", created_at="2026-09-23T09:00:00",
                lots=[{"lot": lot, "quantity": qty, "fg_ids": [fg_id],
                       "breakdown": [{"fg_id": fg_id, "quantity": qty}]}], **kw)


class Folders(unittest.TestCase):
    def build(self, fgs, sales=(), adjustments=(), photos=("rm_bulk_20260915090000_000",)):
        return build_folders(list(fgs), [RUN], list(sales), list(adjustments), MATS, set(photos))

    def test_only_organic_gets_a_folder(self):
        other = dict(fg("x", "210927", 12, 12), certification="Conventional")
        self.assertEqual([f["lot"] for f in self.build([fg("a", "210927", 240, 240, "run1"), other])],
                         ["210927"])
        self.assertEqual(self.build([other]), [])

    def test_made_sold_reduced_held_add_up(self):
        f = self.build([fg("a", "210927", 240, 180, "run1")],
                       sales=[sale("s1", "ORD1", 48, "a", "210927")],
                       adjustments=[{"kind": "subtract", "created_at": "2026-09-24T08:00:00",
                                     "reason": "Breakage", "notes": "dropped",
                                     "drained": [{"fg_id": "a", "lot": "210927", "quantity": 12}]}])[0]
        s = f["skus"][0]
        self.assertEqual((s["made"], s["sold"], s["reduced"], s["held"], s["unrecorded"]), (240, 48, 12, 180, 0))
        self.assertEqual(f["status"], "held")
        self.assertEqual([o["kind"] for o in f["out"]], ["sale", "reduction"])
        self.assertEqual(f["out"][1]["reason"], "Breakage")

    def test_closed_at_zero(self):
        f = self.build([fg("a", "210927", 48, 0, "run1")], sales=[sale("s1", "ORD1", 48, "a", "210927")])[0]
        self.assertEqual(f["status"], "closed")

    def test_sale_shows_the_whole_order(self):
        other_line = dict(sale("s2", "ORD1", 24, "zzz", "BASELINE"), recipe="Beef", certification="")
        f = self.build([fg("a", "210927", 240, 192, "run1")],
                       sales=[sale("s1", "ORD1", 48, "a", "210927"), other_line])[0]
        o = f["out"][0]
        self.assertEqual(o["jars"], 48)
        self.assertEqual(len(o["lines"]), 2)
        self.assertEqual([l["from_this_lot"] for l in o["lines"]], [48, 0])
        self.assertEqual(o["slip_sale_id"], "s1")

    def test_invoices_grouped_by_delivery(self):
        f = self.build([fg("a", "210927", 240, 240, "run1")])[0]
        farm = [i for i in f["invoices"] if i["supplier"] == "Farm"][0]
        self.assertEqual(len(farm["lines"]), 2)  # bones + carrots, one delivery
        self.assertEqual(farm["photo_id"], "rm_bulk_20260915090000_000")
        opening = [i for i in f["invoices"] if i["opening_count"]][0]
        self.assertIsNone(opening["photo_id"])
        self.assertEqual([l["item"] for l in f["no_lot"]], ["Organic Garlic"])
        self.assertEqual(f["batch_date"], "2026-09-21")

    def test_missing_invoice_photo(self):
        f = self.build([fg("a", "210927", 240, 240, "run1")], photos=())[0]
        farm = [i for i in f["invoices"] if i["supplier"] == "Farm"][0]
        self.assertIsNone(farm["photo_id"])

    def test_opening_count_and_count_corrections(self):
        base = fg("r", "210927", 60, 50, source="reset_baseline", reset_at="2026-09-30T08:00:00")
        f = self.build([base], adjustments=[{"kind": "audit_fg", "brand": "Soma", "recipe": "Organic Chicken",
                                             "format": "SS-750ML", "lot": "210927", "diff": -10,
                                             "created_at": "2026-10-05T08:00:00"}])[0]
        s = f["skus"][0]
        self.assertEqual((s["added"], s["reduced"], s["held"], s["unrecorded"]), (60, 10, 50, 0))
        self.assertEqual(f["added"][0]["label"], "Opening count")
        self.assertEqual(f["batch_date"], "2026-09-21")  # from the LOT#, no batch on record

    def test_hand_correction_shows_as_unrecorded(self):
        f = self.build([fg("a", "210927", 240, 230, "run1")])[0]
        self.assertEqual(f["skus"][0]["unrecorded"], 10)

    def test_held_first_then_newest(self):
        folders = self.build([fg("a", "010927", 12, 0, "run1"), fg("b", "150927", 12, 12),
                              fg("c", "200927", 12, 12)])
        self.assertEqual([f["lot"] for f in folders], ["200927", "150927", "010927"])


class Route(unittest.TestCase):
    def test_manager_only(self):
        c = app.app.test_client()
        self.assertEqual(c.get("/api/organic-lots").status_code, 401)
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "production"
        self.assertEqual(c.get("/api/organic-lots").status_code, 403)


if __name__ == "__main__":
    unittest.main()

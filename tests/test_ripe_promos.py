"""Ripe credits: one-time credits vs monthly promos (helpers._renew_monthly_credits).

A monthly promo issues ONE credit per calendar month for its amount; a past
month's leftover expires (kept for the ledger), a spent one is dropped; editing
the promo moves this month's credit by the difference; the Ripe-facing list
(_active_ripe_credits) never shows an expired credit.
"""
import unittest
from datetime import datetime

from helpers import (_renew_monthly_credits, _active_ripe_credits,
                     _sanitize_ripe_credits, _sanitize_monthly_promos)

SEP = datetime(2026, 9, 22)
OCT = datetime(2026, 10, 1)


def _info(**kw):
    base = {"ripe_credits": [{"id": "c1", "name": "Breakage", "amount": 40}],
            "ripe_monthly_promos": [{"id": "p1", "name": "Marketing", "amount": 50}]}
    base.update(kw)
    return base


class MonthlyPromos(unittest.TestCase):
    def test_issues_one_credit_per_month_and_only_once(self):
        info = _info()
        self.assertTrue(_renew_monthly_credits(info, SEP))
        monthly = [c for c in info["ripe_credits"] if c["kind"] == "monthly"]
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0]["id"], "p1-2026-09")
        self.assertEqual(monthly[0]["name"], "Marketing — Sep 2026")
        self.assertEqual(monthly[0]["amount"], 50)
        self.assertEqual(monthly[0]["issued"], 50)
        # a second read in the same month changes nothing, even after a draw
        monthly[0]["amount"] = 20
        self.assertFalse(_renew_monthly_credits(info, SEP))
        self.assertEqual([c["amount"] for c in info["ripe_credits"] if c["kind"] == "monthly"], [20])

    def test_one_time_credits_are_untouched(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        once = [c for c in info["ripe_credits"] if c["kind"] == "once"]
        self.assertEqual(once, [{"id": "c1", "name": "Breakage", "amount": 40, "kind": "once"}])

    def test_new_month_expires_leftover_and_issues_fresh(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        sep = next(c for c in info["ripe_credits"] if c["id"] == "p1-2026-09")
        sep["amount"] = 30  # $20 was used in September
        self.assertTrue(_renew_monthly_credits(info, OCT))
        by_id = {c["id"]: c for c in info["ripe_credits"]}
        self.assertTrue(by_id["p1-2026-09"]["expired"])
        self.assertEqual(by_id["p1-2026-09"]["amount"], 30)   # kept for the ledger
        self.assertEqual(by_id["p1-2026-10"]["amount"], 50)   # fresh, no rollover
        # Ripe sees October only (plus the one-time credit)
        self.assertEqual({c["id"] for c in _active_ripe_credits(info)}, {"c1", "p1-2026-10"})

    def test_new_month_drops_a_fully_used_instance(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        next(c for c in info["ripe_credits"] if c["id"] == "p1-2026-09")["amount"] = 0
        _renew_monthly_credits(info, OCT)
        self.assertNotIn("p1-2026-09", {c["id"] for c in info["ripe_credits"]})

    def test_editing_the_promo_moves_this_month_by_the_difference(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        inst = next(c for c in info["ripe_credits"] if c["id"] == "p1-2026-09")
        inst["amount"] = 30  # $20 used
        info["ripe_monthly_promos"][0]["amount"] = 75
        info["ripe_monthly_promos"][0]["name"] = "Marketing credit"
        self.assertTrue(_renew_monthly_credits(info, SEP))
        inst = next(c for c in info["ripe_credits"] if c["id"] == "p1-2026-09")
        self.assertEqual(inst["amount"], 55)      # 30 left + (75 - 50)
        self.assertEqual(inst["issued"], 75)
        self.assertEqual(inst["name"], "Marketing credit — Sep 2026")
        # cutting below what was already used floors at zero
        info["ripe_monthly_promos"][0]["amount"] = 10
        _renew_monthly_credits(info, SEP)
        inst = next(c for c in info["ripe_credits"] if c["id"] == "p1-2026-09")
        self.assertEqual(inst["amount"], 0)

    def test_no_promos_and_no_monthly_credits_is_a_no_op(self):
        info = {"ripe_credits": [{"id": "c1", "name": "X", "amount": 5}]}
        self.assertFalse(_renew_monthly_credits(info, SEP))
        self.assertEqual(info["ripe_credits"], [{"id": "c1", "name": "X", "amount": 5}])

    def test_legacy_scalar_is_migrated_before_issuing(self):
        info = {"ripe_credit": 15, "ripe_monthly_promos": [{"id": "p1", "name": "M", "amount": 5}]}
        self.assertTrue(_renew_monthly_credits(info, SEP))
        self.assertNotIn("ripe_credit", info)
        ids = {c["id"] for c in info["ripe_credits"]}
        self.assertEqual(ids, {"legacy", "p1-2026-09"})

    def test_sanitizers(self):
        self.assertEqual(_sanitize_monthly_promos([{"name": " ", "amount": 0}, {"id": "p", "name": "A", "amount": "12.5"}]),
                         [{"id": "p", "name": "A", "amount": 12.5}])
        kept = _sanitize_ripe_credits([{"id": "p-2026-09", "name": "A — Sep 2026", "amount": 3,
                                        "kind": "monthly", "template_id": "p", "month": "2026-09",
                                        "expired": True, "issued": 50}])[0]
        self.assertEqual(kept["kind"], "monthly")
        self.assertTrue(kept["expired"])
        self.assertEqual(kept["issued"], 50)
        # a plain row is "once" and carries no monthly fields
        self.assertEqual(_sanitize_ripe_credits([{"id": "c", "name": "B", "amount": 1}]),
                         [{"id": "c", "name": "B", "amount": 1, "kind": "once"}])


if __name__ == "__main__":
    unittest.main()

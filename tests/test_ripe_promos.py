"""Ripe credits: one-time credits vs monthly promos (helpers._renew_monthly_credits).

A monthly promo has ONE running credit; its amount is ADDED to that balance
once per calendar month and nothing expires. Editing the promo moves the
balance by the difference against this month's top-up; removing the promo
keeps the balance as a one-time credit.
"""
import unittest
from datetime import datetime

from helpers import (_renew_monthly_credits, _active_ripe_credits,
                     _sanitize_ripe_credits, _sanitize_monthly_promos)

SEP = datetime(2026, 9, 22)
OCT = datetime(2026, 10, 1)
NOV = datetime(2026, 11, 3)


def _info(**kw):
    base = {"ripe_credits": [{"id": "c1", "name": "Breakage", "amount": 40}],
            "ripe_monthly_promos": [{"id": "p1", "name": "Marketing", "amount": 50}]}
    base.update(kw)
    return base


def _monthly(info):
    return [c for c in info["ripe_credits"] if c["kind"] == "monthly"]


class MonthlyPromos(unittest.TestCase):
    def test_first_read_grants_this_month_once(self):
        info = _info()
        self.assertTrue(_renew_monthly_credits(info, SEP))
        (m,) = _monthly(info)
        self.assertEqual((m["id"], m["name"], m["amount"], m["issued"], m["month"]),
                         ("p1", "Marketing", 50, 50, "2026-09"))
        m["amount"] = 20  # a draw
        self.assertFalse(_renew_monthly_credits(info, SEP))
        self.assertEqual(_monthly(info)[0]["amount"], 20)

    def test_one_time_credits_are_untouched(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        once = [c for c in info["ripe_credits"] if c["kind"] == "once"]
        self.assertEqual(once, [{"id": "c1", "name": "Breakage", "amount": 40, "kind": "once"}])

    def test_new_month_adds_up_nothing_expires(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        _monthly(info)[0]["amount"] = 30           # $20 used in September
        self.assertTrue(_renew_monthly_credits(info, OCT))
        (m,) = _monthly(info)
        self.assertEqual(m["amount"], 80)          # 30 left + 50
        self.assertEqual(m["issued"], 100)
        self.assertFalse(_renew_monthly_credits(info, OCT))
        _renew_monthly_credits(info, NOV)
        self.assertEqual(_monthly(info)[0]["amount"], 130)
        # Ripe sees one line per promo plus the one-time credit
        self.assertEqual({c["id"]: c["amount"] for c in _active_ripe_credits(info)},
                         {"c1": 40, "p1": 130})

    def test_editing_the_promo_moves_this_month_by_the_difference(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        _monthly(info)[0]["amount"] = 30           # $20 used
        info["ripe_monthly_promos"][0].update(amount=75, name="Marketing credit")
        self.assertTrue(_renew_monthly_credits(info, SEP))
        (m,) = _monthly(info)
        self.assertEqual((m["amount"], m["issued"], m["name"]), (55, 75, "Marketing credit"))
        info["ripe_monthly_promos"][0]["amount"] = 10   # below what was used → floors at 0
        _renew_monthly_credits(info, SEP)
        self.assertEqual(_monthly(info)[0]["amount"], 0)
        # next month tops up the new amount on top of whatever is left
        _renew_monthly_credits(info, OCT)
        self.assertEqual(_monthly(info)[0]["amount"], 10)

    def test_removing_the_promo_keeps_the_balance_as_one_time(self):
        info = _info()
        _renew_monthly_credits(info, SEP)
        info["ripe_monthly_promos"] = []
        self.assertTrue(_renew_monthly_credits(info, SEP))
        by_id = {c["id"]: c for c in info["ripe_credits"]}
        self.assertEqual(by_id["p1"]["kind"], "once")
        self.assertEqual(by_id["p1"]["amount"], 50)
        _renew_monthly_credits(info, OCT)               # no more top-ups
        self.assertEqual({c["id"]: c["amount"] for c in info["ripe_credits"]}, {"c1": 40, "p1": 50})

    def test_first_cut_per_month_instances_merge_into_one_balance(self):
        info = _info(ripe_credits=[
            {"id": "p1-2026-08", "name": "Marketing — Aug 2026", "amount": 15, "issued": 50,
             "kind": "monthly", "template_id": "p1", "month": "2026-08"},
            {"id": "p1-2026-09", "name": "Marketing — Sep 2026", "amount": 50, "issued": 50,
             "kind": "monthly", "template_id": "p1", "month": "2026-09"}])
        self.assertTrue(_renew_monthly_credits(info, SEP))
        (m,) = _monthly(info)
        self.assertEqual((m["id"], m["name"], m["amount"], m["issued"], m["month"]),
                         ("p1", "Marketing", 65, 100, "2026-09"))

    def test_no_promos_and_no_monthly_credits_is_a_no_op(self):
        info = {"ripe_credits": [{"id": "c1", "name": "X", "amount": 5}]}
        self.assertFalse(_renew_monthly_credits(info, SEP))
        self.assertEqual(info["ripe_credits"], [{"id": "c1", "name": "X", "amount": 5}])

    def test_legacy_scalar_is_migrated_before_granting(self):
        info = {"ripe_credit": 15, "ripe_monthly_promos": [{"id": "p1", "name": "M", "amount": 5}]}
        self.assertTrue(_renew_monthly_credits(info, SEP))
        self.assertNotIn("ripe_credit", info)
        self.assertEqual({c["id"] for c in info["ripe_credits"]}, {"legacy", "p1"})

    def test_sanitizers(self):
        self.assertEqual(_sanitize_monthly_promos([{"name": " ", "amount": 0}, {"id": "p", "name": "A", "amount": "12.5"}]),
                         [{"id": "p", "name": "A", "amount": 12.5}])
        kept = _sanitize_ripe_credits([{"id": "p", "name": "A", "amount": 3, "kind": "monthly",
                                        "template_id": "p", "month": "2026-09", "issued": 50, "month_issued": 25}])[0]
        self.assertEqual((kept["kind"], kept["issued"], kept["month_issued"]), ("monthly", 50, 25))
        self.assertEqual(_sanitize_ripe_credits([{"id": "c", "name": "B", "amount": 1}]),
                         [{"id": "c", "name": "B", "amount": 1, "kind": "once"}])


if __name__ == "__main__":
    unittest.main()

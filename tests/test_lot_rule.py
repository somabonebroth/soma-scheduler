"""The one LOT# rule: batch START date + 365 days, ddmmyy (helpers._lot_for_batch_date)."""
import unittest
from datetime import date, datetime

from helpers import _lot_for_batch_date, _aggregate_lots_for_sku, _sku_key


class LotRule(unittest.TestCase):
    def test_start_plus_365(self):
        self.assertEqual(_lot_for_batch_date(datetime(2026, 9, 17)), "170927")
        self.assertEqual(_lot_for_batch_date(date(2026, 9, 17)), "170927")

    def test_leap_year_is_365_days_not_one_calendar_year(self):
        # 2028 is a leap year: +365 days from 01/03/2027 lands on 29/02/2028.
        self.assertEqual(_lot_for_batch_date(date(2027, 3, 1)), "290228")

    def test_inventory_production_date_is_the_start_day(self):
        fg = [{"brand": "SOMA", "recipe": "Beef", "format": "SS-750ML", "lot": "170927",
               "quantity_produced": 10, "quantity_remaining": 10, "id": "a",
               "week_id": "2026-09-14", "day_idx": 4,
               "start_week_id": "2026-09-14", "start_day_idx": 3}]
        row = _aggregate_lots_for_sku(fg, _sku_key("SOMA", "Beef", "SS-750ML"))[0]
        self.assertEqual(row["production_date"], "2026-09-17")
        self.assertEqual(row["best_before"], "17/09/2027")


if __name__ == "__main__":
    unittest.main()

"""Daily Summary retail channels (daily_brief._channel_day, 2026-10-10).

The day's dollar figure is the WHOLE day — jars plus other items (hot cups,
creams, gift cards, anything without a SOMA- SKU) — the same money the sales
charts add up. Units stay jars only.
"""
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import app
import daily_brief
from tests.test_channel_daily import preview


class ChannelDay(unittest.TestCase):
    def test_other_items_counted_in_revenue(self):
        tmp = tempfile.mkdtemp()
        env = {"CLOVER_API_TOKEN": "t", "CLOVER_MERCHANT_ID": "m"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(app, "RECIPES_PATH", os.path.join(tmp, "r.json")), \
                mock.patch.object(app, "ORGANIC_SALES_PATH", os.path.join(tmp, "s.json")), \
                mock.patch("clover_importer.preview_day", return_value=preview()):
            c = daily_brief._channel_day("clover", date(2026, 9, 29))
        self.assertEqual(c["status"], "ok")
        self.assertEqual(c["units"], 4)                  # jars only
        self.assertEqual(c["jar_revenue"], 60.0)
        self.assertEqual(c["other_revenue"], 38.0)       # 15 + 5 hot cups + 18 cream
        self.assertEqual(c["revenue"], 98.0)
        names = {i["name"]: i for i in c["other_items"]}
        self.assertEqual(names["Hot Cup"]["quantity"], 4)
        self.assertEqual(names["CREAM-1"]["revenue"], 18.0)


if __name__ == "__main__":
    unittest.main()

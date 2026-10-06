"""Finished-goods count: the save matches stock the way the list did (2026-10-06).

The count list keys each SKU with `_sku_key` (format normalised), but the save
matched the raw stored strings. A brand whose recipes store the format any
other way ('ss-750ml', a trailing space) showed the right on-hand figure, then
matched nothing on save: a shortage changed nothing and a count was added ON
TOP of the old stock. Found on the Ripe and Nature's Emporium counts.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import app
import helpers


class CountFormatMatch(unittest.TestCase):
    def run_count(self, fmt, counted):
        tmp = tempfile.mkdtemp()
        fg_path = os.path.join(tmp, "fg.json")
        with open(fg_path, "w") as f:
            json.dump([{"id": "r1", "brand": "Ripe", "recipe": "Big Kahuna", "format": fmt,
                        "lot": "BL-010926", "quantity_produced": 60, "quantity_remaining": 60,
                        "created_at": "2026-09-01T10:00:00"}], f)
        with mock.patch.object(app, "ORGANIC_FG_PATH", fg_path), \
                mock.patch.object(app, "AUDITS_PATH", os.path.join(tmp, "audits.json")), \
                mock.patch.object(app, "load_recipes",
                                  return_value={"Big Kahuna": {"brand": "Ripe", "format": fmt}}), \
                mock.patch.object(helpers, "ADJUSTMENTS_PATH", os.path.join(tmp, "adj.json")):
            items = app._build_fg_audit_items("Ripe")
            self.assertEqual([i["system_qty"] for i in items], [60])
            c = app.app.test_client()
            with c.session_transaction() as s:
                s["authenticated"] = True
                s["role"] = "manager"
            r = c.post("/api/audit/a1/complete",
                       json={"kind": "fg", "brand": "Ripe",
                             "results": {items[0]["id"]: {"counted": counted}}})
            self.assertEqual(r.status_code, 200, r.get_json())
            return json.load(open(fg_path))

    def test_shortage_lands_whatever_the_stored_spelling(self):
        for fmt in ("SS-750ML", "ss-750ml", "SS-750ml ", "Frozen 750ml"):
            with self.subTest(fmt=fmt):
                fg = self.run_count(fmt, 36)
                self.assertEqual(sum(f["quantity_remaining"] for f in fg), 36)
                self.assertEqual(len(fg), 1)

    def test_surplus_is_written_in_the_stored_spelling(self):
        fg = self.run_count("ss-750ml", 72)
        self.assertEqual(sum(f["quantity_remaining"] for f in fg), 72)
        new = [f for f in fg if f.get("source") == "audit_baseline"]
        self.assertEqual((new[0]["format"], new[0]["recipe"], new[0]["quantity_remaining"]),
                         ("ss-750ml", "Big Kahuna", 12))


if __name__ == "__main__":
    unittest.main()

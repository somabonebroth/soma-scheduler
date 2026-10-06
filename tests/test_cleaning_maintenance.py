"""Cleaning & Maintenance split from the kitchen's End of Day (2026-10-06).

The rotating jobs are their own page and their own Daily Summary section:
section 4 lists each job signed that day with who signed it and its note;
the job notes no longer join the notes at the top, and a job alone never
makes the kitchen "ran" (no missing-closing flag on a maintenance-only day).
"""
import unittest
from datetime import date
from unittest import mock

import app  # noqa: F401 — must load before its blueprints (circular import)
import daily_brief

DAY = date(2026, 10, 5)
CLEAN = {
    "closing": {"signed": False, "complete": False, "missed": [], "total": 10},
    "jobs_done": [{"title": "Descale dishwasher", "staff": "Ana",
                   "notes": "Needed two cycles", "ts": "2026-10-05T15:20:00"}],
    "jobs_overdue": 2,
    "declined": False,
    "manager_note": "",
}
FOH = {"closing": {}, "manager_note": "", "exists": False}
PROD = {"completed": False, "ccp_issues": []}


class CleaningMaintenance(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(daily_brief.cleaning, "day_summary", return_value=CLEAN)
        p2 = mock.patch.object(daily_brief.cleaning, "foh_day_summary", return_value=FOH)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_section_lists_job_signer_and_note(self):
        m = daily_brief._maintenance_section(DAY)
        self.assertEqual(m["overdue"], 2)
        self.assertEqual(m["jobs_done"][0]["staff"], "Ana")
        self.assertEqual(m["jobs_done"][0]["notes"], "Needed two cycles")

    def test_job_note_not_in_top_notes_and_no_rotation_key(self):
        checks = daily_brief._checklists_section(DAY, PROD)
        self.assertNotIn("rotation", checks)
        self.assertFalse(any(n["text"] == "Needed two cycles" for n in checks["notes"]))

    def test_job_alone_is_not_the_kitchen_running(self):
        checks = daily_brief._checklists_section(DAY, PROD)
        self.assertFalse(checks["_kitchen_ran"])
        self.assertFalse(any("Closing checklist" in i["text"] for i in checks["issues"]))


if __name__ == "__main__":
    unittest.main()

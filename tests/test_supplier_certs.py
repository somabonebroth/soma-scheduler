"""Supplier certificates (suppliers.py, 2026-09-23).

A supplier is a `supplier` or a `distributor` and carries a list of
certificates, each with its own expiry and PDF. A distributor's list also holds
the certificates of the suppliers it buys from (`source` = that supplier).
The Daily Summary's "Request renewal" list is every certificate expired or
within 14 days of expiry.
"""
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import suppliers
from suppliers import _cert_status, _normalize_supplier, _clean_certificates, _annotate

TODAY = date(2026, 9, 23)


class Status(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(_cert_status("", TODAY), ("none", None))
        self.assertEqual(_cert_status("bad", TODAY), ("none", None))
        self.assertEqual(_cert_status("2026-09-22", TODAY), ("expired", -1))
        self.assertEqual(_cert_status("2026-09-23", TODAY), ("renew", 0))
        self.assertEqual(_cert_status("2026-10-07", TODAY), ("renew", 14))
        self.assertEqual(_cert_status("2026-10-08", TODAY), ("expiring", 15))
        self.assertEqual(_cert_status("2026-11-22", TODAY), ("expiring", 60))
        self.assertEqual(_cert_status("2026-11-23", TODAY), ("current", 61))


class Legacy(unittest.TestCase):
    def test_single_cert_fields_fold_into_the_list(self):
        s = _normalize_supplier({"id": "1", "name": "P", "certifications": "Certified Organic",
                                 "cert_expiry": "2027-01-01", "cert_doc_id": "doc.pdf"})
        self.assertEqual(s["type"], "supplier")
        self.assertEqual(s["certificates"], [{"id": "legacy", "name": "Certified Organic", "source": "",
                                              "expiry": "2027-01-01", "doc_id": "doc.pdf"}])
        for k in ("certifications", "cert_expiry", "cert_doc_id"):
            self.assertNotIn(k, s)

    def test_nothing_recorded_means_no_certificates(self):
        self.assertEqual(_normalize_supplier({"id": "1", "name": "P"})["certificates"], [])

    def test_current_shape_untouched(self):
        s = {"id": "1", "name": "B", "type": "distributor", "certificates": []}
        self.assertEqual(_normalize_supplier(s), s)


class Clean(unittest.TestCase):
    def test_keeps_uploaded_file_by_id(self):
        old = [{"id": "a", "name": "X", "source": "", "expiry": "", "file": "1_a.pdf", "filename": "x.pdf"}]
        certs, err = _clean_certificates([{"id": "a", "name": "X2", "expiry": "2027-01-01",
                                           "file": "../../evil"}], old)
        self.assertIsNone(err)
        self.assertEqual(certs[0]["file"], "1_a.pdf")
        self.assertEqual(certs[0]["name"], "X2")

    def test_rejects_blank_name_and_bad_date(self):
        self.assertTrue(_clean_certificates([{"id": "a", "name": " "}], [])[1])
        self.assertTrue(_clean_certificates([{"id": "a", "name": "X", "expiry": "soon"}], [])[1])

    def test_bad_or_duplicate_id_gets_a_new_one(self):
        certs, _ = _clean_certificates([{"id": "../x", "name": "A"}, {"id": "b", "name": "B"},
                                        {"id": "b", "name": "C"}], [])
        ids = [c["id"] for c in certs]
        self.assertEqual(len(set(ids)), 3)
        self.assertNotIn("../x", ids)

    def test_worst_status_summarises_the_supplier(self):
        s = _annotate({"certificates": [{"id": "a", "name": "A", "expiry": "2028-01-01"},
                                        {"id": "b", "name": "B", "expiry": "2026-09-30"}]}, TODAY)
        self.assertEqual((s["cert_status"], s["cert_days_left"]), ("renew", 7))


class Renewals(unittest.TestCase):
    def test_distributor_upstream_certificate_is_chased(self):
        data = [
            {"id": "1", "name": "The Butcher Shoppe", "type": "distributor", "certificates": [
                {"id": "a", "name": "Certified Organic", "source": "", "expiry": "2027-06-01"},
                {"id": "b", "name": "Certified Organic", "source": "Pfennings", "expiry": "2026-10-01"}]},
            {"id": "2", "name": "Farm", "certificates": [
                {"id": "c", "name": "GAP", "source": "", "expiry": "2026-09-20"}]},
        ]
        with mock.patch.object(suppliers, "_load_json", return_value=data):
            due = suppliers.renewals_due(TODAY)
        self.assertEqual([(r["supplier"], r["source"], r["days_left"]) for r in due],
                         [("Farm", "", -3), ("The Butcher Shoppe", "Pfennings", 8)])


class Routes(unittest.TestCase):
    """Test-client round trip: create, upload a PDF, save the form again
    (file survives), remove the certificate (file deleted)."""

    def setUp(self):
        import app as app_module
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(suppliers, "SUPPLIERS_PATH", os.path.join(self.tmp, "s.json")),
                        mock.patch.object(suppliers, "CERT_DIR", os.path.join(self.tmp, "certs"))]
        for p in self.patches:
            p.start()
        self.c = app_module.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_round_trip(self):
        import io
        r = self.c.post("/api/suppliers", json={"name": "The Butcher Shoppe", "type": "distributor",
                        "certificates": [{"id": "up1", "name": "Certified Organic",
                                          "source": "Pfennings", "expiry": "2027-01-01"}]})
        self.assertEqual(r.status_code, 201, r.get_json())
        sid = r.get_json()["id"]
        url = f"/api/suppliers/{sid}/certificates/up1/file"
        bad = self.c.post(url, data={"file": (io.BytesIO(b"hello"), "x.pdf")})
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post(url, data={"file": (io.BytesIO(b"%PDF-1.4 test"), "cert.pdf")})
        self.assertEqual(ok.status_code, 200, ok.get_json())
        self.assertEqual(self.c.get(url).data, b"%PDF-1.4 test")
        # Re-saving the form (which never sends `file`) keeps the PDF.
        r = self.c.put(f"/api/suppliers/{sid}", json={"certificates": [
            {"id": "up1", "name": "Certified Organic", "source": "Pfennings", "expiry": "2027-02-01"}]})
        self.assertEqual(r.get_json()["certificates"][0]["file"], f"{sid}_up1.pdf")
        self.assertEqual(self.c.get(url).status_code, 200)
        # Removing the certificate deletes its file.
        self.c.put(f"/api/suppliers/{sid}", json={"certificates": []})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "certs", f"{sid}_up1.pdf")))
        self.assertEqual(self.c.put(f"/api/suppliers/{sid}", json={"type": "shop"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()

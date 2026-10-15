"""Pest control map on the Organic Certification page (audit_tools.py,
2026-10-15): one current file, replaced on upload, type checked by its bytes."""
import io
import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("SECRET_KEY", "test")
import app  # noqa: E402


class PestMap(unittest.TestCase):
    def setUp(self):
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def up(self, name, data):
        return self.c.post("/api/pest-map", data={"file": (io.BytesIO(data), name)},
                           content_type="multipart/form-data")

    def test_upload_replace_and_serve(self):
        r = self.up("map.png", b"\x89PNG....")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/pest-map").json["ext"], "png")
        r = self.up("map.pdf", b"%PDF-1.4 ...")
        self.assertEqual(r.status_code, 200)
        f = self.c.get("/api/pest-map/file")
        self.assertEqual(f.status_code, 200)
        self.assertEqual(f.mimetype, "application/pdf")
        files = os.listdir(os.path.join(app.DATA_DIR, "pest_control"))
        self.assertNotIn("pest_control_map.png", files)  # the old map is gone

    def test_refuses_fakes_and_other_types(self):
        self.assertEqual(self.up("map.pdf", b"not a pdf").status_code, 400)
        self.assertEqual(self.up("map.docx", b"PK..").status_code, 400)

    def test_production_role_locked_out(self):
        with self.c.session_transaction() as s:
            s["role"] = "production"
        self.assertEqual(self.c.get("/api/pest-map").status_code, 403)


if __name__ == "__main__":
    unittest.main()

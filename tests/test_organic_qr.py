"""Organic case QR labels (helpers + /api/label, 2026-09-30).

An organic case label carries a QR "SOMA:<LOT#>:<SKU code>"; every other
label is the original text-only label. One label per request either way — the
number of copies is set in the printer's app.
"""
import re
import unittest
from unittest import mock

import app
import pdf_engine
from helpers import _organic_qr_payload, _organic_sku_code, _parse_organic_qr

KEY = "SOMA|ORGANIC CHICKEN BONE BROTH|SS-750ML"


class Payload(unittest.TestCase):
    def test_shape(self):
        p = _organic_qr_payload("210927", KEY)
        self.assertRegex(p, r"^SOMA:210927:[0-9A-F]{6}$")
        # QR alphanumeric mode: capitals, digits and a few symbols only.
        self.assertTrue(re.fullmatch(r"[0-9A-Z $%*+\-./:]+", p))

    def test_sku_code_is_stable_and_case_blind(self):
        self.assertEqual(_organic_sku_code(KEY), _organic_sku_code(KEY.lower()))
        self.assertNotEqual(_organic_sku_code(KEY), _organic_sku_code(KEY.replace("750", "473")))

    def test_parse_round_trip(self):
        self.assertEqual(_parse_organic_qr(_organic_qr_payload("210927", KEY)),
                         ("210927", _organic_sku_code(KEY)))
        self.assertEqual(_parse_organic_qr(" soma:210927:" + _organic_sku_code(KEY).lower()),
                         ("210927", _organic_sku_code(KEY)))

    def test_parse_rejects_other_codes(self):
        for text in ("", "https://example.com", "SOMA:210927", "ABC:210927:123456", "SOMA::123456",
                     "SOMA:210927:12345"):
            self.assertIsNone(_parse_organic_qr(text), text)


class LabelRoute(unittest.TestCase):
    RECIPES = {"Organic Chicken Bone Broth": {"certification": "Organic"},
               "Chicken Bone Broth": {"certification": "Conventional"}}

    def setUp(self):
        self.patch = mock.patch.object(app, "load_recipes", return_value=self.RECIPES)
        self.patch.start()
        self.c = app.app.test_client()
        with self.c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "manager"

    def tearDown(self):
        self.patch.stop()

    def label(self, recipe):
        r = self.c.post("/api/label", json={"brand_name": "Soma", "recipe_name": recipe,
                                            "recipe_format": "SS-750ML", "lot": "210927", "copies": 20})
        self.assertEqual(r.status_code, 200)
        return r.data

    @staticmethod
    def pages(pdf):
        return len(re.findall(rb"/Type\s*/Page\b", pdf))

    def test_always_one_label(self):
        # Copies are set in the printer's app (Flash Label), never here.
        self.assertEqual(self.pages(self.label("Organic Chicken Bone Broth")), 1)
        self.assertEqual(self.pages(self.label("Chicken Bone Broth")), 1)

    def test_the_qr_is_drawn_only_for_organic(self):
        with mock.patch.object(app, "generate_label_pdf") as gen:
            gen.side_effect = lambda buf, *a, **k: buf.write(b"%PDF")
            self.label("Organic Chicken Bone Broth")
            self.assertEqual(gen.call_args.kwargs["qr_data"], _organic_qr_payload("210927", KEY))
            self.label("Chicken Bone Broth")
            self.assertIsNone(gen.call_args.kwargs["qr_data"])


class LabelFit(unittest.TestCase):
    """Nothing on an organic label may run into the printer's dead right edge
    (the first version wrapped by a character-count guess and got cut off)."""

    def drawn(self, brand, product):
        import io
        from reportlab.pdfgen.canvas import Canvas
        calls = []
        real = Canvas.drawString

        def spy(canvas, x, y, text, *a, **k):
            calls.append((x, y, text, canvas._fontname, canvas._fontsize))
            return real(canvas, x, y, text, *a, **k)
        with mock.patch.object(Canvas, "drawString", spy):
            pdf_engine.generate_label_pdf(io.BytesIO(), brand, product, "210927", "21/09/2027",
                                          qr_data="SOMA:210927:33812A")
        return calls

    def check(self, brand, product):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.lib.units import inch
        calls = self.drawn(brand, product)
        text = " ".join(t for _, _, t, _, _ in calls)
        for word in (brand + " " + product).split():
            for part in word.split("-"):
                self.assertIn(part, text)          # nothing dropped
        for x, y, t, font, size in calls:
            self.assertLessEqual(x + stringWidth(t, font, size), 2 * inch - 10 + 0.01, t)
            self.assertGreaterEqual(y, 4, t)       # nothing off the bottom
            self.assertLessEqual(y + size, 1 * inch - 3, t)
        return calls

    def test_ordinary_names_keep_full_size(self):
        calls = self.check("Soma Bone Broth", "Organic Chicken Bone Broth-SS-750ML")
        self.assertEqual(calls[0][4], 7)  # brand not shrunk

    def test_long_names_shrink_instead_of_overflowing(self):
        self.check("Nature's Emporium Private Label", "Organic Grass-Fed Beef Bone Broth with Turmeric-SS-750ML")


if __name__ == "__main__":
    unittest.main()

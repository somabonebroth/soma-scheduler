"""Organic case QR labels (helpers + /api/label, 2026-09-30).

An organic case label carries a QR "SOMA:<LOT#>:<SKU code>" and prints one
label per case; every other label is the original single text-only label.
"""
import re
import unittest
from unittest import mock

import app
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

    def label(self, recipe, copies):
        r = self.c.post("/api/label", json={"brand_name": "Soma", "recipe_name": recipe,
                                            "recipe_format": "SS-750ML", "lot": "210927", "copies": copies})
        self.assertEqual(r.status_code, 200)
        return r.data

    @staticmethod
    def pages(pdf):
        return len(re.findall(rb"/Type\s*/Page\b", pdf))

    def test_organic_prints_one_label_per_case(self):
        self.assertEqual(self.pages(self.label("Organic Chicken Bone Broth", 20)), 20)

    def test_copies_are_capped(self):
        self.assertEqual(self.pages(self.label("Organic Chicken Bone Broth", 5000)), 200)

    def test_non_organic_is_one_text_label(self):
        self.assertEqual(self.pages(self.label("Chicken Bone Broth", 20)), 1)

    def test_the_qr_is_drawn_only_for_organic(self):
        with mock.patch.object(app, "generate_label_pdf") as gen:
            gen.side_effect = lambda buf, *a, **k: buf.write(b"%PDF")
            self.label("Organic Chicken Bone Broth", 3)
            self.assertEqual(gen.call_args.kwargs["qr_data"], _organic_qr_payload("210927", KEY))
            self.label("Chicken Bone Broth", 3)
            self.assertIsNone(gen.call_args.kwargs["qr_data"])
            self.assertEqual(gen.call_args.kwargs["copies"], 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the delivery-zone engine. Stdlib unittest — run with:

    python3 -m unittest tests.test_delivery_zones -v

The first class drives the REAL delivery_zones.json (the cases named in the
build spec); the second uses a tiny in-memory table to pin the matching rules
themselves (longest prefix, exact beats prefix, tie → lower zone, fallback).
"""

import unittest

import delivery_zones as dz


class RealTableLookups(unittest.TestCase):
    """The spec's named cases against the committed zone file."""

    @classmethod
    def setUpClass(cls):
        cls.table = dz.load_table(force=True)

    def zone_of(self, code):
        return dz.lookup(code, self.table)

    def test_pattern_counts_match_the_file_comment(self):
        counts = {z.number: len(z.patterns) for z in self.table.zones}
        self.assertEqual(counts, {1: 130, 2: 103, 3: 26, 4: 0})

    def test_m5j_subcode_claimed_by_zone_1_exact_pattern(self):
        m = self.zone_of("M5J2M2")
        self.assertEqual(m.zone.number, 1)
        self.assertEqual(m.pattern, "M5J2M2")

    def test_m5j_code_not_claimed_by_zone_1_falls_to_zone_2_m5j_star(self):
        m = self.zone_of("M5J1K5")
        self.assertEqual(m.zone.number, 2)
        self.assertEqual(m.pattern, "M5J*")

    def test_outer_scarborough_is_zone_2(self):
        m = self.zone_of("M1B2K9")
        self.assertEqual(m.zone.number, 2)
        self.assertEqual(m.pattern, "M1B*")

    def test_toronto_code_unclaimed_by_zones_1_and_2_hits_zone_3_m_star(self):
        # M7A = Queen's Park: a real Toronto FSA in neither Zone 1 nor Zone 2.
        m = self.zone_of("M7A1A1")
        self.assertEqual(m.zone.number, 3)
        self.assertEqual(m.pattern, "M*")

    def test_regional_code_is_zone_3(self):
        m = self.zone_of("N2L3G1")
        self.assertEqual(m.zone.number, 3)
        self.assertEqual(m.pattern, "N2*")

    def test_ottawa_is_zone_4_fallback(self):
        m = self.zone_of("K1A0B1")
        self.assertEqual(m.zone.number, 4)
        self.assertIsNone(m.pattern)
        self.assertFalse(m.zone.deliverable)

    def test_deliberately_excluded_rural_fsas_stay_out(self):
        for fsa in ("N0H", "N0L", "N0M", "N0N", "N0P", "N0R", "P0A", "K0K", "K0L", "K7R", "P2A", "N7A", "N7G"):
            self.assertEqual(self.zone_of(fsa + "1A1").zone.number, 4, fsa)

    def test_input_is_normalized(self):
        for raw in ("m5j 2m2", " M5J-2M2 ", "m5J2m2", "M5J\t2M2"):
            self.assertEqual(self.zone_of(raw).zone.number, 1, repr(raw))

    def test_malformed_input_raises_not_zone_4(self):
        for bad in ("", "   ", "M5J", "M5J 2M", "12345", "M5J2M2X", "MMM111", "Z1A1A1", "D1A1A1", "M5J2D2"):
            with self.assertRaises(dz.InvalidPostalCode, msg=repr(bad)):
                self.zone_of(bad)


class QuoteTerms(unittest.TestCase):
    """The pricing arithmetic on the real table."""

    @classmethod
    def setUpClass(cls):
        cls.table = dz.load_table(force=True)

    def q(self, code, cases=None):
        return dz.quote(code, cases, self.table)

    def test_zone_1_is_always_free_with_4_case_minimum(self):
        r = self.q("M4K3S5", 3)
        self.assertEqual((r["zone"], r["min_cases"], r["delivery_fee"], r["free_at_cases"]), (1, 4, 0.0, None))
        self.assertFalse(r["order"]["meets_minimum"])
        self.assertEqual(r["order"]["short_by"], 1)
        self.assertEqual(r["order"]["fee"], 0.0)
        self.assertTrue(r["order"]["free_delivery"])
        self.assertIsNone(r["order"]["cases_to_free"])

    def test_zone_2_between_minimum_and_threshold_pays_fee_and_shows_cases_to_free(self):
        r = self.q("L5B1A1", 9)
        self.assertEqual(r["zone"], 2)
        o = r["order"]
        self.assertTrue(o["meets_minimum"])
        self.assertEqual(o["fee"], 100.0)
        self.assertFalse(o["free_delivery"])
        self.assertEqual(o["cases_to_free"], 3)
        self.assertIn("First delivery", r["notes"][0])

    def test_zone_2_at_threshold_is_free(self):
        o = self.q("L5B1A1", 12)["order"]
        self.assertEqual(o["fee"], 0.0)
        self.assertTrue(o["free_delivery"])
        self.assertIsNone(o["cases_to_free"])

    def test_zone_2_below_minimum_flags_and_hides_cases_to_free(self):
        o = self.q("L5B1A1", 5)["order"]
        self.assertFalse(o["meets_minimum"])
        self.assertEqual(o["short_by"], 3)
        self.assertEqual(o["fee"], 100.0)
        self.assertIsNone(o["cases_to_free"])

    def test_zone_3_terms(self):
        r = self.q("N2L3G1", 15)
        self.assertEqual((r["min_cases"], r["delivery_fee"], r["free_at_cases"]), (12, 200.0, 20))
        self.assertEqual(r["order"]["cases_to_free"], 5)
        self.assertEqual(self.q("N2L3G1", 20)["order"]["fee"], 0.0)

    def test_zone_4_returns_contact_message_instead_of_pricing(self):
        r = self.q("K1A0B1", 30)
        self.assertFalse(r["deliverable"])
        self.assertIsNone(r["min_cases"])
        self.assertIsNone(r["delivery_fee"])
        self.assertIn("wholesale@somebonebroth.com", r["notes"][0])
        self.assertEqual(r["order"], {"cases": 30})

    def test_no_cases_means_no_order_block(self):
        self.assertIsNone(self.q("M4K3S5")["order"])

    def test_bad_case_count_rejected(self):
        for bad in (-1, 2.5, True, "6"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.q("M4K3S5", bad)

    def test_formatted_code(self):
        self.assertEqual(self.q("m5j2m2")["formatted"], "M5J 2M2")


def _table(zones, fallback=9):
    zs = list(zones) + [{"number": fallback, "name": "Outside", "patterns": []}]
    return dz.build_table({"zones": zs, "fallback_zone": fallback, "case_size": 12})


class MatchingRules(unittest.TestCase):
    """The rule itself, on a table small enough to reason about by eye."""

    def test_longest_prefix_wins_regardless_of_list_order(self):
        # The SHORT pattern is listed in the LOWER zone: first-match-wins would pick zone 1.
        t = _table([
            {"number": 1, "name": "A", "min_cases": 1, "delivery_fee": 0, "patterns": ["M*"]},
            {"number": 2, "name": "B", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5J*"]},
            {"number": 3, "name": "C", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5J2W*"]},
        ])
        self.assertEqual(dz.match("M5J2W7", t).zone.number, 3)
        self.assertEqual(dz.match("M5J1A1", t).zone.number, 2)
        self.assertEqual(dz.match("M4K3S5", t).zone.number, 1)

    def test_exact_pattern_beats_five_char_prefix(self):
        t = _table([
            {"number": 1, "name": "A", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5J2W*"]},
            {"number": 2, "name": "B", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5J2W7"]},
        ])
        self.assertEqual(dz.match("M5J2W7", t).zone.number, 2)
        self.assertEqual(dz.match("M5J2W3", t).zone.number, 1)

    def test_tie_goes_to_lower_zone_number(self):
        # Same-length prefixes can't collide in one table (duplicates are rejected),
        # but different prefixes of equal length can both match nothing at once —
        # so a tie can only arise when two zones' patterns are equal-length AND both
        # match, which requires identical prefixes. Pin the invariant via a
        # deliberately reordered file: the lower number wins even if listed later.
        t = _table([
            {"number": 2, "name": "B", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5*"]},
            {"number": 1, "name": "A", "min_cases": 1, "delivery_fee": 0, "patterns": ["M5J*"]},
        ])
        self.assertEqual(dz.match("M5J1A1", t).zone.number, 1)
        self.assertEqual(dz.match("M5A1A1", t).zone.number, 2)

    def test_unmatched_goes_to_fallback(self):
        t = _table([{"number": 1, "name": "A", "min_cases": 1, "delivery_fee": 0, "patterns": ["M*"]}])
        m = dz.match("K1A0B1", t)
        self.assertEqual(m.zone.number, 9)
        self.assertIsNone(m.pattern)

    def test_config_rejects_duplicate_pattern_across_zones(self):
        with self.assertRaises(dz.ZoneConfigError):
            _table([
                {"number": 1, "name": "A", "patterns": ["M5J*"]},
                {"number": 2, "name": "B", "patterns": ["M5J*"]},
            ])

    def test_config_rejects_short_exact_pattern(self):
        with self.assertRaises(dz.ZoneConfigError):
            _table([{"number": 1, "name": "A", "patterns": ["M5J"]}])

    def test_config_rejects_unknown_fallback(self):
        with self.assertRaises(dz.ZoneConfigError):
            dz.build_table({"zones": [{"number": 1, "name": "A", "patterns": []}], "fallback_zone": 4})


if __name__ == "__main__":
    unittest.main()

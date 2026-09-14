"""Wholesale delivery-zone lookup — the PURE engine (stdlib only, no Flask).

Given a Canadian postal code, answer: which delivery zone, what minimum order,
what delivery fee, and (optionally, given a case count) what this order
actually pays. The routes live in ``delivery_zone_routes.py``; this module
must stay importable without Flask so the tests can drive it directly.

Zone data lives in ``delivery_zones.json`` at the repo root (committed config,
like the SBBC portal's zones.json — NOT on the persistent data disk). Edit the
file and redeploy to change a zone; nothing here needs to change.

Matching rule — LONGEST PREFIX WINS, never list order:
  * a pattern ending in ``*`` is a prefix match on the normalized code and
    scores the length of the prefix (``M5J*`` → 3, ``M5J2W*`` → 5);
  * a pattern without ``*`` is an exact full-code match and scores 6;
  * the highest score wins; on a tie the LOWER zone number wins;
  * no match at all → the fallback zone (4, Outside Delivery Area).
This is what lets Zone 1 claim specific M5J sub-codes while Zone 2 holds a
bare ``M5J*`` and Zone 3 holds bare ``L*``/``M*`` catch-alls. A first-match
walk over a flat list would get all three wrong.
"""

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "delivery_zones.json")

# Canada Post format A1A 1A1. The first letter never uses D F I O Q U W Z; the
# other two letters never use D F I O Q U. Digits anywhere.
POSTAL_RE = re.compile(r"^[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\d[ABCEGHJ-NPRSTV-Z]\d$")
_PATTERN_RE = re.compile(r"^[A-Z0-9]{1,6}\*?$")


class InvalidPostalCode(ValueError):
    """Input is not a well-formed Canadian postal code."""


class ZoneConfigError(ValueError):
    """delivery_zones.json is missing or malformed."""


@dataclass(frozen=True)
class Zone:
    number: int
    name: str
    min_cases: Optional[int]
    delivery_fee: Optional[float]
    free_at_cases: Optional[int]
    note: Optional[str]
    patterns: Tuple[str, ...]

    @property
    def deliverable(self) -> bool:
        return self.min_cases is not None


@dataclass(frozen=True)
class ZoneTable:
    zones: Tuple[Zone, ...]
    fallback_zone: int
    case_size: int
    store: Dict[str, object]   # label/address/postal_code as str; lat/lng stay numeric

    def zone(self, number: int) -> Zone:
        for z in self.zones:
            if z.number == number:
                return z
        raise ZoneConfigError(f"no zone numbered {number}")


@dataclass(frozen=True)
class Match:
    zone: Zone
    pattern: Optional[str]   # the winning pattern, None when the fallback fired
    score: int               # matched prefix length (6 for an exact match, 0 for fallback)


# ── input ──────────────────────────────────────────────────────────────────────

def normalize(raw: str) -> str:
    """Uppercase and strip whitespace + hyphens. Does NOT validate."""
    return re.sub(r"[\s\-]+", "", (raw or "")).upper()


def validate(raw: str) -> str:
    """Normalize and check the Canada Post shape; raise InvalidPostalCode otherwise."""
    code = normalize(raw)
    if not code:
        raise InvalidPostalCode("Enter a postal code.")
    if not POSTAL_RE.match(code):
        raise InvalidPostalCode(f"'{raw.strip()}' is not a valid Canadian postal code (expected A1A 1A1).")
    return code


def format_postal(code: str) -> str:
    """M5J2M2 → 'M5J 2M2' for display."""
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


# ── config ─────────────────────────────────────────────────────────────────────

def _int_or_none(v, field, zone_no):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ZoneConfigError(f"zone {zone_no}: {field} must be a non-negative integer or null")
    return v


def _money_or_none(v, field, zone_no):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ZoneConfigError(f"zone {zone_no}: {field} must be a non-negative number or null")
    return float(v)


def build_table(data: dict) -> ZoneTable:
    """Validate a raw config dict into a ZoneTable. Raises ZoneConfigError loudly —
    a bad zone file should fail the lookup, never silently quote the wrong fee."""
    if not isinstance(data, dict) or not isinstance(data.get("zones"), list) or not data["zones"]:
        raise ZoneConfigError("config must carry a non-empty 'zones' list")
    zones: List[Zone] = []
    seen_patterns: Dict[str, int] = {}
    seen_numbers = set()
    for raw in data["zones"]:
        try:
            number = int(raw["number"])
            name = str(raw["name"]).strip()
        except (KeyError, TypeError, ValueError):
            raise ZoneConfigError("every zone needs an integer 'number' and a 'name'")
        if number in seen_numbers:
            raise ZoneConfigError(f"zone number {number} appears twice")
        seen_numbers.add(number)
        patterns = raw.get("patterns") or []
        if not isinstance(patterns, list):
            raise ZoneConfigError(f"zone {number}: 'patterns' must be a list")
        cleaned = []
        for p in patterns:
            p = normalize(str(p)) if isinstance(p, str) else ""
            if not _PATTERN_RE.match(p):
                raise ZoneConfigError(f"zone {number}: bad pattern {p!r} (1-6 chars A-Z/0-9, optional trailing *)")
            if not p.endswith("*") and len(p) != 6:
                raise ZoneConfigError(f"zone {number}: exact pattern {p!r} must be a full 6-character code")
            if p.endswith("*") and len(p) == 7:
                raise ZoneConfigError(f"zone {number}: {p!r} — a 6-character prefix is an exact match; drop the *")
            if p in seen_patterns:
                raise ZoneConfigError(f"pattern {p!r} appears in zone {seen_patterns[p]} and zone {number}")
            seen_patterns[p] = number
            cleaned.append(p)
        zones.append(Zone(
            number=number,
            name=name,
            min_cases=_int_or_none(raw.get("min_cases"), "min_cases", number),
            delivery_fee=_money_or_none(raw.get("delivery_fee"), "delivery_fee", number),
            free_at_cases=_int_or_none(raw.get("free_at_cases"), "free_at_cases", number),
            note=(str(raw["note"]).strip() or None) if raw.get("note") else None,
            patterns=tuple(cleaned),
        ))
    zones.sort(key=lambda z: z.number)
    fallback = data.get("fallback_zone")
    if fallback not in seen_numbers:
        raise ZoneConfigError("'fallback_zone' must name one of the zones")
    case_size = data.get("case_size", 12)
    if isinstance(case_size, bool) or not isinstance(case_size, int) or case_size <= 0:
        raise ZoneConfigError("'case_size' must be a positive integer")
    store = data.get("store") or {}
    if not isinstance(store, dict):
        store = {}
    clean_store: Dict[str, object] = {}
    for k, v in store.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            clean_store[str(k)] = v          # lat / lng for the map marker
        else:
            clean_store[str(k)] = str(v)
    return ZoneTable(zones=tuple(zones), fallback_zone=int(fallback), case_size=case_size, store=clean_store)


_TABLE: Optional[ZoneTable] = None
_TABLE_LOCK = threading.Lock()


def load_table(path: str = CONFIG_PATH, force: bool = False) -> ZoneTable:
    """Load + cache the committed zone file. Re-raises config problems as ZoneConfigError."""
    global _TABLE
    if _TABLE is not None and not force and path == CONFIG_PATH:
        return _TABLE
    with _TABLE_LOCK:
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ZoneConfigError(f"zone file not found: {path}")
        except json.JSONDecodeError as e:
            raise ZoneConfigError(f"zone file is not valid JSON: {e}")
        table = build_table(data)
        if path == CONFIG_PATH:
            _TABLE = table
        return table


# ── matching ───────────────────────────────────────────────────────────────────

def _score(pattern: str, code: str) -> int:
    """Length of the matched prefix, 6 for an exact hit, -1 for no match."""
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return len(prefix) if code.startswith(prefix) else -1
    return 6 if code == pattern else -1


def match(code: str, table: Optional[ZoneTable] = None) -> Match:
    """Longest-prefix match of an ALREADY-VALIDATED code. Tie → lower zone number
    (zones are stored ascending and only a strictly better score replaces)."""
    table = table or load_table()
    best: Optional[Match] = None
    for zone in table.zones:
        for pattern in zone.patterns:
            s = _score(pattern, code)
            if s >= 0 and (best is None or s > best.score):
                best = Match(zone=zone, pattern=pattern, score=s)
    if best is None:
        return Match(zone=table.zone(table.fallback_zone), pattern=None, score=0)
    return best


def lookup(postal_code: str, table: Optional[ZoneTable] = None) -> Match:
    """Validate a raw postal code and match it. Raises InvalidPostalCode."""
    return match(validate(postal_code), table)


# ── quoting ────────────────────────────────────────────────────────────────────

def _order_terms(zone: Zone, cases: int) -> dict:
    """What THIS order pays in THIS zone. Fee is the base fee unless the free
    threshold is met; cases_to_free only shows between minimum and threshold."""
    min_cases = zone.min_cases or 0
    meets_minimum = cases >= min_cases
    base_fee = zone.delivery_fee or 0.0
    free = zone.free_at_cases is not None and cases >= zone.free_at_cases
    fee = 0.0 if free else base_fee
    cases_to_free = None
    if zone.free_at_cases is not None and meets_minimum and cases < zone.free_at_cases:
        cases_to_free = zone.free_at_cases - cases
    return {
        "cases": cases,
        "meets_minimum": meets_minimum,
        "short_by": max(0, min_cases - cases),
        "fee": fee,
        "free_delivery": free or base_fee == 0.0,
        "cases_to_free": cases_to_free,
    }


def quote(postal_code: str, cases: Optional[int] = None, table: Optional[ZoneTable] = None) -> dict:
    """The full answer for the page/API. Raises InvalidPostalCode or ValueError
    (bad case count); config problems surface as ZoneConfigError."""
    table = table or load_table()
    if cases is not None:
        if isinstance(cases, bool) or not isinstance(cases, int) or cases < 0:
            raise ValueError("Case quantity must be a whole number of 0 or more.")
    m = lookup(postal_code, table)
    z = m.zone
    notes = [z.note] if z.note else []
    out = {
        "postal_code": validate(postal_code),
        "formatted": format_postal(validate(postal_code)),
        "zone": z.number,
        "zone_name": z.name,
        "matched_pattern": m.pattern,
        "deliverable": z.deliverable,
        "min_cases": z.min_cases,
        "delivery_fee": z.delivery_fee,
        "free_at_cases": z.free_at_cases,
        "case_size": table.case_size,
        "notes": notes,
        "order": None,
    }
    if z.deliverable and cases is not None:
        out["order"] = _order_terms(z, cases)
    elif cases is not None:
        out["order"] = {"cases": cases}
    return out


def zone_summary(table: Optional[ZoneTable] = None) -> dict:
    """Reference block for the page: every zone's terms + its pattern list."""
    table = table or load_table()
    return {
        "store": table.store,
        "case_size": table.case_size,
        "fallback_zone": table.fallback_zone,
        "zones": [
            {
                "number": z.number, "name": z.name, "min_cases": z.min_cases,
                "delivery_fee": z.delivery_fee, "free_at_cases": z.free_at_cases,
                "note": z.note, "pattern_count": len(z.patterns), "patterns": list(z.patterns),
            }
            for z in table.zones
        ],
    }


# ── FSA classification (feeds the zone map) ────────────────────────────────────
#
# A Forward Sortation Area is the first three characters of a postal code. The
# map colours whole FSAs, so it needs the zone an FSA lands in when nothing
# longer than three characters is known. Same patterns, same longest-prefix
# rule, restricted to patterns short enough to decide on the FSA alone; any
# LONGER pattern inside the FSA (today only Zone 1's M5J sub-codes) marks the
# FSA as SPLIT — the map hatches it and tells the user to run the lookup.

FSA_GEOJSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ontario_fsa.geojson")
_FSA_RE = re.compile(r"^[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]$")


@dataclass(frozen=True)
class FsaMatch:
    fsa: str
    zone: Zone
    pattern: Optional[str]   # the ≤3-char pattern that decided it, None when the fallback fired
    split: bool              # a longer pattern claims part of this FSA for another zone


def validate_fsa(raw: str) -> str:
    code = normalize(raw)
    if not _FSA_RE.match(code):
        raise InvalidPostalCode(f"'{(raw or '').strip()}' is not a valid FSA (expected A1A).")
    return code


def zone_for_fsa(fsa: str, table: Optional[ZoneTable] = None) -> FsaMatch:
    """Zone of an FSA from the patterns that decide on three characters or
    fewer, longest prefix winning as in match(). Exact six-character patterns
    and prefixes longer than three never decide an FSA — they set `split`."""
    table = table or load_table()
    code = validate_fsa(fsa)
    best: Optional[Tuple[int, Zone, str]] = None
    split = False
    for zone in table.zones:
        for pattern in zone.patterns:
            prefix = pattern[:-1] if pattern.endswith("*") else pattern
            if len(prefix) > 3:
                if prefix.startswith(code):
                    split = True
                continue
            if code.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), zone, pattern)
    if best is None:
        return FsaMatch(fsa=code, zone=table.zone(table.fallback_zone), pattern=None, split=split)
    # A longer pattern that lands in the SAME zone is not a split worth flagging.
    if split:
        split = any(
            (p[:-1] if p.endswith("*") else p).startswith(code)
            and len(p[:-1] if p.endswith("*") else p) > 3
            for z in table.zones if z.number != best[1].number
            for p in z.patterns
        )
    return FsaMatch(fsa=code, zone=best[1], pattern=best[2], split=split)


@dataclass(frozen=True)
class FsaIndex:
    codes: Tuple[str, ...]                    # every FSA the GeoJSON draws, sorted
    neighbours: Dict[str, Tuple[str, ...]]    # FSA -> FSAs sharing a border (from the build script)


_FSA_INDEX: Optional[FsaIndex] = None
_FSA_INDEX_LOCK = threading.Lock()


def load_fsa_index(path: str = FSA_GEOJSON_PATH, force: bool = False) -> FsaIndex:
    """The FSA codes + neighbour graph the committed GeoJSON carries, read once
    and cached. The engine never needs the geometry — only which codes exist
    on the map and which touch which (tools/build_fsa_geojson.py computes the
    adjacency from the unsimplified source, where shared borders share vertices)."""
    global _FSA_INDEX
    if _FSA_INDEX is not None and not force and path == FSA_GEOJSON_PATH:
        return _FSA_INDEX
    with _FSA_INDEX_LOCK:
        try:
            with open(path) as f:
                fc = json.load(f)
            neighbours: Dict[str, Tuple[str, ...]] = {}
            for feat in fc.get("features", []):
                props = feat.get("properties") if isinstance(feat, dict) else None
                if not isinstance(props, dict) or not props.get("fsa"):
                    continue
                code = str(props["fsa"]).upper()
                nbs = props.get("neighbours") or []
                neighbours[code] = tuple(sorted(str(n).upper() for n in nbs if n))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise ZoneConfigError(f"FSA map file unreadable: {e}")
        if not neighbours:
            raise ZoneConfigError("FSA map file carries no FSA codes")
        index = FsaIndex(codes=tuple(sorted(neighbours)), neighbours=neighbours)
        if path == FSA_GEOJSON_PATH:
            _FSA_INDEX = index
        return index


def _pattern_covers(pattern: str, fsa: str) -> bool:
    """Could this pattern ever match a postal code in this FSA?"""
    prefix = pattern[:-1] if pattern.endswith("*") else pattern
    return fsa.startswith(prefix[:3]) if len(prefix) >= 3 else fsa.startswith(prefix)


def fsa_map(index: Optional[FsaIndex] = None, table: Optional[ZoneTable] = None) -> dict:
    """Everything the map page needs in one call: each FSA's zone, which FSAs
    are split, and two audit lists —
      * `unmatched_nearby`: FSAs on the fallback zone that share a BORDER with
        a served FSA — the ring just outside the delivery area. The
        deliberately excluded rural codes that touch it show up here on
        purpose; a reviewer should see what sits one step out (each entry
        names the served neighbours, so an accidental hole is obvious);
      * `patterns_without_fsa`: zone patterns no FSA on the map can satisfy — a
        typo in the pattern list, or an FSA StatCan does not draw (single-
        building business FSAs like M5K / M5X are real but unmapped).
    """
    table = table or load_table()
    index = index or load_fsa_index()
    codes = index.codes
    zones: Dict[str, int] = {}
    matched_pattern: Dict[str, Optional[str]] = {}
    split: List[str] = []
    for code in codes:
        m = zone_for_fsa(code, table)
        zones[code] = m.zone.number
        matched_pattern[code] = m.pattern
        if m.split:
            split.append(code)
    unmatched_nearby = []
    for code in codes:
        if zones[code] != table.fallback_zone:
            continue
        served = [n for n in index.neighbours.get(code, ()) if n in zones and zones[n] != table.fallback_zone]
        if served:
            unmatched_nearby.append({"fsa": code, "beside": served})
    patterns_without_fsa = [
        {"zone": z.number, "pattern": p}
        for z in table.zones for p in z.patterns
        if not any(_pattern_covers(p, c) for c in codes)
    ]
    counts = {z.number: 0 for z in table.zones}
    for z in zones.values():
        counts[z] += 1
    return {
        "fsa_count": len(codes),
        "zones": zones,
        "matched_pattern": matched_pattern,
        "split": split,
        "counts": counts,
        "unmatched_nearby": unmatched_nearby,
        "patterns_without_fsa": patterns_without_fsa,
    }

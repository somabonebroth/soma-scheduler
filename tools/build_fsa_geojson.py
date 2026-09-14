#!/usr/bin/env python3
"""Build static/ontario_fsa.geojson from StatCan's 2021 Forward Sortation Area
boundary file — a ONE-TIME data-prep step run on a Mac, never on Render.

    python3 -m venv /tmp/geo && /tmp/geo/bin/pip install pyshp pyproj
    /tmp/geo/bin/python tools/build_fsa_geojson.py [--zip PATH] [--out PATH]

Needs pyshp + pyproj locally ONLY — they are deliberately NOT in
requirements.txt; the app never reads the shapefile, only the committed
GeoJSON this writes. Re-run only if StatCan publishes new FSA boundaries.

What it does:
  1. downloads (or reuses) lfsa000b21a_e.zip — the CARTOGRAPHIC boundary file,
     clipped to shorelines, in NAD83 / Statistics Canada Lambert (metres);
  2. keeps Ontario (PRUID 35 — the K L M N P FSAs, 520 of them);
  3. simplifies each ring with Douglas-Peucker in projected metres (urban
     FSAs lightly, rural "x0x" FSAs hard — they are enormous and full of
     lakes), drops islands/lakes too small to see, always keeps the largest
     outer ring so a one-building downtown FSA (M5K, M5L, M5X) survives;
  4. reprojects to WGS84 and rounds to 4 dp (~11 m);
  5. finds each FSA's NEIGHBOURS — FSAs sharing at least two boundary
     vertices in the (topologically clean, unsimplified) source — so the
     app can say which unserved FSAs sit right beside served ones;
  6. writes a FeatureCollection with properties `fsa` + `neighbours`.

Rings are simplified independently, so neighbours can show hairline slivers
where a shared edge was thinned differently on each side — cosmetic, hidden
by the 1 px outline the map draws. Topology-preserving simplification would
need a real geo toolchain and is not worth it for an internal quoting aid.

Caveats to keep in mind (also stated on the page): these are 2021 CENSUS
FSAs — an FSA created since will not appear, and StatCan's census FSAs differ
slightly from Canada Post's live ones.
"""

import argparse
import io
import json
import os
import sys
import urllib.request
import zipfile

try:
    import shapefile
    from pyproj import CRS, Transformer
except ImportError:
    sys.exit("needs pyshp + pyproj in a LOCAL venv (see the module docstring) — never add them to requirements.txt")

STATCAN_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
               "boundary-limites/files-fichiers/lfsa000b21a_e.zip")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "static", "ontario_fsa.geojson")
ONTARIO_PRUID = "35"

# Douglas-Peucker tolerance in projected METRES. Urban FSAs are small and dense
# (a city block matters); rural "x0x" FSAs are hundreds of km wide.
TOL_URBAN_M = 40.0
TOL_RURAL_M = 250.0
# Drop any ring (island or lake) below this area UNLESS it is the FSA's largest
# outer ring. 0.25 km² keeps the Toronto Islands (~3 km²) and every downtown
# FSA; it sheds the thousands of ponds that make P0X a 640k-point polygon.
MIN_RING_AREA_M2 = 250_000.0


def is_rural(fsa: str) -> bool:
    """Canada Post: a 0 as the second character means a rural FSA."""
    return fsa[1] == "0"


def ring_area(ring):
    """Shoelace area (absolute, m²) of a closed ring."""
    a = 0.0
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def douglas_peucker(points, tol):
    """Iterative Douglas-Peucker on an open polyline (list of (x, y))."""
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    tol2 = tol * tol
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        sx, sy = points[s]
        ex, ey = points[e]
        dx, dy = ex - sx, ey - sy
        seg2 = dx * dx + dy * dy
        best_d2, best_i = -1.0, -1
        for i in range(s + 1, e):
            px, py = points[i]
            if seg2 == 0.0:
                d2 = (px - sx) ** 2 + (py - sy) ** 2
            else:
                t = ((px - sx) * dx + (py - sy) * dy) / seg2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                qx, qy = sx + t * dx, sy + t * dy
                d2 = (px - qx) ** 2 + (py - qy) ** 2
            if d2 > best_d2:
                best_d2, best_i = d2, i
        if best_d2 > tol2:
            keep[best_i] = True
            stack.append((s, best_i))
            stack.append((best_i, e))
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring, tol):
    """Simplify a closed ring; returns None if it collapses (< 4 points incl. closure)."""
    pts = list(ring)
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    out = douglas_peucker(pts, tol)
    if len(out) < 4:
        return None
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def simplify_geometry(geom, tol):
    """geom is pyshp's __geo_interface__ Polygon/MultiPolygon in projected
    metres. Returns (polygons, dropped_rings) where polygons is a list of
    [outer, hole, hole...] rings, simplified and filtered."""
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else list(geom["coordinates"])
    # Find the largest outer ring — it always survives.
    areas = [ring_area(p[0]) for p in polys]
    largest = max(range(len(polys)), key=lambda i: areas[i]) if polys else -1
    out, dropped = [], 0
    for i, rings in enumerate(polys):
        outer = rings[0]
        if i != largest and areas[i] < MIN_RING_AREA_M2:
            dropped += 1
            continue
        s_outer = simplify_ring(outer, tol)
        if s_outer is None:
            if i == largest:
                s_outer = list(outer) if outer[0] == outer[-1] else list(outer) + [outer[0]]
            else:
                dropped += 1
                continue
        s_rings = [s_outer]
        for hole in rings[1:]:
            if ring_area(hole) < MIN_RING_AREA_M2:
                dropped += 1
                continue
            s_hole = simplify_ring(hole, tol)
            if s_hole is None:
                dropped += 1
                continue
            s_rings.append(s_hole)
        out.append(s_rings)
    return out, dropped


def neighbours_by_shared_vertices(shapes_by_fsa):
    """Adjacency from the UNSIMPLIFIED rings: StatCan's file is topologically
    clean, so two FSAs that share a border share the exact same vertices.
    Two or more shared vertices = a shared edge (one = a corner touch, ignored).
    Vertices are keyed on integer metres to be safe against float noise."""
    owner = {}          # vertex key -> first fsa seen
    pairs = {}          # (fsa_a, fsa_b) -> shared vertex count
    for fsa, shape in shapes_by_fsa.items():
        seen_here = set()
        for x, y in shape.points:
            key = (int(round(x)) << 32) | (int(round(y)) & 0xFFFFFFFF)
            if key in seen_here:
                continue
            seen_here.add(key)
            other = owner.get(key)
            if other is None:
                owner[key] = fsa
            elif other != fsa:
                k = (other, fsa) if other < fsa else (fsa, other)
                pairs[k] = pairs.get(k, 0) + 1
    out = {fsa: set() for fsa in shapes_by_fsa}
    for (a, b), n in pairs.items():
        if n >= 2:
            out[a].add(b)
            out[b].add(a)
    return {fsa: sorted(v) for fsa, v in out.items()}


def fetch_zip(path):
    if os.path.exists(path):
        print(f"reusing {path}")
        return
    print(f"downloading {STATCAN_URL} (~160 MB)…")
    with urllib.request.urlopen(STATCAN_URL) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=os.path.join(os.path.expanduser("~"), "Downloads", "lfsa000b21a_e.zip"),
                    help="where to cache/read the StatCan zip")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    fetch_zip(args.zip)
    zf = zipfile.ZipFile(args.zip)
    names = {os.path.splitext(n)[1].lower(): n for n in zf.namelist() if n.lower().endswith((".shp", ".shx", ".dbf", ".prj"))}
    reader = shapefile.Reader(
        shp=io.BytesIO(zf.read(names[".shp"])),
        shx=io.BytesIO(zf.read(names[".shx"])),
        dbf=io.BytesIO(zf.read(names[".dbf"])),
        encoding="latin-1",
    )
    prj_wkt = zf.read(names[".prj"]).decode("ascii")
    to_wgs84 = Transformer.from_crs(CRS.from_wkt(prj_wkt), "EPSG:4326", always_xy=True)

    shapes_by_fsa = {}
    for i, rec in enumerate(reader.iterRecords()):
        if rec["PRUID"] == ONTARIO_PRUID:
            shapes_by_fsa[rec["CFSAUID"]] = reader.shape(i)
    print(f"{len(shapes_by_fsa)} Ontario FSAs read; computing neighbours…")
    neighbours = neighbours_by_shared_vertices(shapes_by_fsa)

    features = []
    total_in = total_out = total_dropped = 0
    for fsa, shape in shapes_by_fsa.items():
        geom = shape.__geo_interface__
        total_in += len(shape.points)
        tol = TOL_RURAL_M if is_rural(fsa) else TOL_URBAN_M
        polys, dropped = simplify_geometry(geom, tol)
        total_dropped += dropped
        ll_polys = []
        for rings in polys:
            ll_rings = []
            for ring in rings:
                xs, ys = to_wgs84.transform([p[0] for p in ring], [p[1] for p in ring])
                ll = [[round(x, 4), round(y, 4)] for x, y in zip(xs, ys)]
                # rounding can create consecutive duplicates — squash them
                dedup = [ll[0]]
                for p in ll[1:]:
                    if p != dedup[-1]:
                        dedup.append(p)
                if dedup[0] != dedup[-1]:
                    dedup.append(dedup[0])
                if len(dedup) >= 4:
                    ll_rings.append(dedup)
                total_out += len(dedup)
            if ll_rings:
                ll_polys.append(ll_rings)
        if not ll_polys:
            print(f"WARNING: {fsa} collapsed to nothing", file=sys.stderr)
            continue
        geometry = ({"type": "Polygon", "coordinates": ll_polys[0]} if len(ll_polys) == 1
                    else {"type": "MultiPolygon", "coordinates": ll_polys})
        features.append({
            "type": "Feature",
            "properties": {"fsa": fsa, "neighbours": neighbours[fsa]},
            "geometry": geometry,
        })

    features.sort(key=lambda f: f["properties"]["fsa"])
    fc = {
        "type": "FeatureCollection",
        "name": "Ontario FSAs, 2021 Census (Statistics Canada cartographic boundary file, simplified)",
        "features": features,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(fc, f, separators=(",", ":"))
    size = os.path.getsize(args.out)
    isolated = sorted(f["properties"]["fsa"] for f in features if not f["properties"]["neighbours"])
    print(f"neighbour links: {sum(len(f['properties']['neighbours']) for f in features) // 2}; "
          f"FSAs with none: {isolated or 'none'}")
    print(f"{len(features)} Ontario FSAs; points {total_in:,} → {total_out:,}; "
          f"{total_dropped} small rings dropped; {size / 1e6:.2f} MB → {args.out}")


if __name__ == "__main__":
    main()

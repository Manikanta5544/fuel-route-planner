"""CSV -> clean stations -> offline coordinate join -> committed build artifacts."""

import csv
import difflib
import io
import json
import re
import statistics
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from routing.resolver import normalize

US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
FUZZY_CUTOFF = 0.88
US_SIMPLIFY_DEG = 0.005  # about 500 m
FOREIGN_SIMPLIFY_DEG = 0.002  # about 200 m: the border matters more than the coast
BAND_DEG = 1.0  # how far into Canada/Mexico the border check looks
ENDPOINT_BUFFER_DEG = 0.02  # tolerance for city centroids that sit on the coast
EXPECTED = {
    "rows_total": 8151,
    "non_us_rows_dropped": 620,
    "unique_us_stops": 6626,
    "identical_duplicate_rows": 26,
    "conflicting_price_stops": 487,
}
SOURCES = {
    "census": (
        "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
        "2024_Gaz_place_national.zip",
        "2024_Gaz_place_national.txt",
    ),
    "uscities": (
        "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv",
        "us_cities.csv",
    ),
    "boundary": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
        "ne_10m_admin_0_countries.geojson",
        "ne_10m_countries.geojson",
    ),
}
_SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|metropolitan government|metro government|"
    r"unified government|urban county|city|town|village|borough|municipality|plantation|CDP)$"
)
_PRICE_RULES = {"min": min, "median": statistics.median, "mean": statistics.fmean}


def download(dest_dir: Path) -> dict[str, str]:
    """Fetch each source; a failing source is reported, not fatal."""
    status = {}
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key, (url, filename) in SOURCES.items():
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
                data = resp.read()
            if url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    data = zf.read(filename)
            (dest_dir / filename).write_bytes(data)
            status[key] = "downloaded"
        except Exception as exc:  # network or format failure
            status[key] = f"failed: {exc}"
    return status


def clean_stations(csv_path: Path, price_rule: str = "min") -> tuple[list[dict], dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    non_us = Counter(r["State"].strip() for r in rows if r["State"].strip() not in US_STATES)
    us_rows = [r for r in rows if r["State"].strip() in US_STATES]
    seen, unique_rows = set(), []
    for r in us_rows:
        key = tuple(r.values())
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)
    by_id = defaultdict(list)
    for r in unique_rows:
        by_id[r["OPIS Truckstop ID"].strip()].append(r)
    agg, stations, conflicting, multi_place = _PRICE_RULES[price_rule], [], 0, 0
    for sid, group in by_id.items():
        prices = [float(r["Retail Price"]) for r in group]
        conflicting += len(set(prices)) > 1
        multi_place += len({(r["City"].strip(), r["State"].strip()) for r in group}) > 1
        best = max(group, key=lambda r: len(r["Truckstop Name"]))
        stations.append(
            {
                "id": sid,
                "name": best["Truckstop Name"].strip(),
                "address": best["Address"].strip(),
                "city": group[0]["City"].strip(),
                "state": group[0]["State"].strip(),
                "price": round(float(agg(prices)), 5),
            }
        )
    report = {
        "rows_total": len(rows),
        "non_us_rows_dropped": sum(non_us.values()),
        "non_us_by_state": dict(non_us),
        "identical_duplicate_rows": len(us_rows) - len(unique_rows),
        "unique_us_stops": len(stations),
        "conflicting_price_stops": conflicting,
        "ids_with_multiple_places": multi_place,
        "price_rule": price_rule,
    }
    return stations, report


def _strip_suffix(name: str) -> str:
    return _SUFFIX.sub("", re.sub(r"\s*\(.*\)$", "", name.split("/")[0]).strip())


def load_census(path: Path) -> dict[str, tuple[float, float]]:
    """Census places gazetteer (tab-delimited): prefer non-CDP, then larger land area."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # Census gazetteer files may be Latin-1
        text = raw.decode("latin-1")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    recs = [dict(zip(header, (c.strip() for c in row), strict=False)) for row in reader if row]
    recs.sort(key=lambda r: (r["NAME"].endswith("CDP"), -float(r["ALAND"] or 0)))
    table: dict[str, tuple[float, float]] = {}
    for r in recs:
        key = f"{r['USPS']}|{normalize(_strip_suffix(r['NAME']))}"
        table.setdefault(key, (float(r["INTPTLAT"]), float(r["INTPTLONG"])))
    return table


def load_uscities(path: Path) -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            key = f"{r['STATE_CODE']}|{normalize(r['CITY'])}"
            table.setdefault(key, (float(r["LATITUDE"]), float(r["LONGITUDE"])))
    return table


def build_territories(boundary_path: Path) -> tuple[dict, dict]:
    """Contiguous-US land (islands such as the Keys included) and the Canada/Mexico band beside it.

    The band is what the route check measures: a route "leaves the US" only when it runs inside
    Canadian or Mexican territory, so coastlines, bays and bridges never count against it.
    """
    feats = {
        f["properties"].get("ADM0_A3"): f
        for f in json.loads(boundary_path.read_text(encoding="utf-8"))["features"]
    }
    conus = [
        g
        for g in shape(feats["USA"]["geometry"]).geoms
        if g.bounds[1] > 20 and g.centroid.x > -130 and g.centroid.y < 50.5
    ]
    us = unary_union(conus).simplify(US_SIMPLIFY_DEG)
    neighbours = unary_union([shape(feats[k]["geometry"]) for k in ("CAN", "MEX")])
    foreign = neighbours.intersection(us.buffer(BAND_DEG)).simplify(FOREIGN_SIMPLIFY_DEG)
    return mapping(us), mapping(foreign)


def join_coordinates(stations, places, us_area) -> tuple[list[dict], list[dict], dict]:
    names_by_state = defaultdict(list)
    for key in places:
        state, name = key.split("|", 1)
        names_by_state[state].append(name)
    matched, unmatched, methods = [], [], Counter()
    for s in stations:
        norm = normalize(s["city"])
        coords, method = places.get(f"{s['state']}|{norm}"), "exact"
        if coords is None:
            close = difflib.get_close_matches(norm, names_by_state[s["state"]], 1, FUZZY_CUTOFF)
            if close:
                coords, method = places[f"{s['state']}|{close[0]}"], "fuzzy"
        if coords is None:
            unmatched.append({**s, "reason": "no_gazetteer_match"})
        elif not shapely.contains_xy(us_area, coords[1], coords[0]):
            unmatched.append({**s, "reason": "outside_contiguous_us"})
        else:
            methods[method] += 1
            matched.append({**s, "lat": coords[0], "lon": coords[1]})
    return matched, unmatched, dict(methods)


def build(csv_path: Path, gazetteer_dir: Path, out_dir: Path, price_rule: str = "min") -> dict:
    stations, report = clean_stations(csv_path, price_rule)
    us, foreign = build_territories(gazetteer_dir / SOURCES["boundary"][1])
    us_area = shape(us).buffer(ENDPOINT_BUFFER_DEG)
    shapely.prepare(us_area)
    places: dict[str, tuple[float, float]] = {}
    sources_used = {}
    for key, loader in (("census", load_census), ("uscities", load_uscities)):
        path = gazetteer_dir / SOURCES[key][1]
        if path.exists():
            loaded = loader(path)
            sources_used[key] = {"places": len(loaded), "added": len(loaded.keys() - places.keys())}
            for k, v in loaded.items():
                places.setdefault(k, v)
    if not places:
        raise FileNotFoundError(f"no gazetteer file in {gazetteer_dir}; run with --download")
    matched, unmatched, methods = join_coordinates(stations, places, us_area)

    out_dir.mkdir(parents=True, exist_ok=True)
    cols = ("id", "name", "address", "city", "state")
    np.savez_compressed(
        out_dir / "stations.npz",
        price=np.array([s["price"] for s in matched]),
        lat=np.array([s["lat"] for s in matched]),
        lon=np.array([s["lon"] for s in matched]),
        **{c: np.array([s[c] for s in matched]) for c in cols},
    )
    (out_dir / "places.json").write_text(
        json.dumps(
            {k: [round(v[0], 5), round(v[1], 5)] for k, v in sorted(places.items())},
            separators=(",", ":"),
        )
    )
    (out_dir / "usa.geojson").write_text(json.dumps(us, separators=(",", ":")))
    (out_dir / "foreign.geojson").write_text(json.dumps(foreign, separators=(",", ":")))
    with open(out_dir / "unmatched.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, [*cols, "price", "reason"])
        w.writeheader()
        w.writerows(unmatched)

    report |= {
        "gazetteer_sources": sources_used,
        "places_total": len(places),
        "matched_exact": methods.get("exact", 0),
        "matched_fuzzy": methods.get("fuzzy", 0),
        "unmatched": len(unmatched),
        "unmatched_by_reason": dict(Counter(u["reason"] for u in unmatched)),
        "match_rate": round(len(matched) / len(stations), 4),
        "stations_written": len(matched),
        "differs_from_expected": {
            k: {"expected": v, "actual": report[k]} for k, v in EXPECTED.items() if report[k] != v
        },
    }
    (out_dir / "build_report.json").write_text(json.dumps(report, indent=2))
    return report

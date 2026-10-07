import csv
import json
from pathlib import Path

import numpy as np
import pytest

from stations import etl

HEADER = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]
BUILD = Path("data/build")


def write_csv(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


def test_clean_stations_drops_canada_dedupes_and_collapses_conflicts(tmp_path):
    rows = [
        ["1", "PILOT #1", "I-35", "Dallas", "TX", "5", "3.00"],
        ["1", "PILOT #1", "I-35", "Dallas", "TX", "5", "3.00"],  # identical duplicate
        ["1", "PILOT TRAVEL CENTER #1", "I-35", "Dallas", "TX", "5", "3.40"],  # conflicting price
        ["2", "LOVES", "US-1", "Toronto", "ON", "9", "1.50"],  # Canada
        ["3", "FLYING J", "I-80", "Reno", "NV", "7", "3.90"],
    ]
    write_csv(tmp_path / "f.csv", rows)
    stations, report = etl.clean_stations(tmp_path / "f.csv")
    assert report["non_us_rows_dropped"] == 1 and report["identical_duplicate_rows"] == 1
    assert report["unique_us_stops"] == 2 and report["conflicting_price_stops"] == 1
    by_id = {s["id"]: s for s in stations}
    assert by_id["1"]["price"] == 3.0 and by_id["1"]["name"] == "PILOT TRAVEL CENTER #1"
    assert etl.clean_stations(tmp_path / "f.csv", "mean")[0][0]["price"] == pytest.approx(3.2)
    assert etl.clean_stations(tmp_path / "f.csv", "median")[0][0]["price"] == pytest.approx(3.2)


def test_census_gazetteer_parsing_prefers_non_cdp(tmp_path):
    cols = "USPS GEOID ANSICODE NAME LSAD FUNCSTAT ALAND AWATER ALAND_SQMI AWATER_SQMI INTPTLAT"
    header = "\t".join(cols.split()) + "\tINTPTLONG  "
    rows = [
        "TX\t1\t1\tAustin CDP\t57\tS\t5\t0\t1\t0\t30.0\t-97.0",
        "TX\t2\t2\tAustin city\t25\tA\t3\t0\t1\t0\t30.27\t-97.74",
        "NY\t3\t3\tNew York city\t25\tA\t9\t0\t1\t0\t40.66\t-73.94",
        "KY\t4\t4\tLouisville/Jefferson County metro government (balance)\t25\tA\t9\t0\t1\t0\t38.1\t-85.7",
    ]
    (tmp_path / "g.txt").write_text("\n".join([header, *rows]))
    table = etl.load_census(tmp_path / "g.txt")
    assert table["TX|austin"] == (30.27, -97.74) and table["NY|new york"] == (40.66, -73.94)
    assert "KY|louisville" in table


def test_fuzzy_match_is_state_scoped_and_unmatched_are_reported():
    places = {"TX|mckinney": (33.2, -96.6), "OK|mckinney": (35.0, -97.0)}
    stations = [
        {"id": "1", "city": "Mc Kinney", "state": "TX", "price": 3.0},
        {"id": "2", "city": "Nowhereville", "state": "TX", "price": 3.0},
    ]
    matched, unmatched, methods = etl.join_coordinates(stations, places)
    assert [m["lat"] for m in matched] == [33.2] and methods == {"fuzzy": 1}
    assert unmatched[0]["reason"] == "no_gazetteer_match"


def test_committed_build_matches_the_supplied_csv_profile():
    report = json.loads((BUILD / "build_report.json").read_text())
    assert report["differs_from_expected"] == {}
    assert (report["rows_total"], report["non_us_rows_dropped"]) == (8151, 620)
    assert (report["unique_us_stops"], report["identical_duplicate_rows"]) == (6626, 26)
    assert report["conflicting_price_stops"] == 487
    with open(BUILD / "unmatched.csv", newline="") as fh:
        unmatched = list(csv.DictReader(fh))
    assert len(unmatched) == report["unmatched"] and all(u["reason"] for u in unmatched)
    assert report["stations_written"] + report["unmatched"] == report["unique_us_stops"]
    assert report["match_rate"] > 0.99


def test_committed_artifacts_are_valid():
    with np.load(BUILD / "stations.npz", allow_pickle=False) as npz:
        n = len(npz["price"])
        assert {len(npz[k]) for k in npz.files} == {n}
        assert np.isfinite(npz["price"]).all() and (npz["price"] > 0.5).all()
        assert (npz["price"] < 10).all()
        assert (npz["lat"] >= 24).all() and (npz["lat"] <= 50).all()
        assert len(set(npz["id"])) == n
    places = json.loads((BUILD / "places.json").read_text())
    assert places["TX|dallas"] and places["NY|new york"] and len(places) > 20000
    usa = json.loads((BUILD / "usa.geojson").read_text())
    assert usa["type"] in ("Polygon", "MultiPolygon")

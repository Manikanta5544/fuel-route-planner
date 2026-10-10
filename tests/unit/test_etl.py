
import csv
import json
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import shape

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
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)


def test_clean_stations_drops_canada_dedupes_and_collapses_conflicts(tmp_path):
    rows = [
        ["1", "PILOT #1", "I-35", "Dallas", "TX", "5", "3.00"],
        ["1", "PILOT #1", "I-35", "Dallas", "TX", "5", "3.00"],
        ["1", "PILOT TRAVEL CENTER #1", "I-35", "Dallas", "TX", "5", "3.40"],
        ["2", "LOVES", "US-1", "Toronto", "ON", "9", "1.50"],
        ["3", "FLYING J", "I-80", "Reno", "NV", "7", "3.90"],
    ]
    source = tmp_path / "f.csv"
    write_csv(source, rows)

    stations, report = etl.clean_stations(source)

    assert report["non_us_rows_dropped"] == 1
    assert report["identical_duplicate_rows"] == 1
    assert report["unique_us_stops"] == 2
    assert report["conflicting_price_stops"] == 1

    by_id = {station["id"]: station for station in stations}
    assert by_id["1"]["price"] == 3.0
    assert by_id["1"]["name"] == "PILOT TRAVEL CENTER #1"

    assert etl.clean_stations(source, "mean")[0][0]["price"] == pytest.approx(3.2)
    assert etl.clean_stations(source, "median")[0][0]["price"] == pytest.approx(3.2)


def test_census_gazetteer_parsing_prefers_non_cdp(tmp_path):
    cols = (
        "USPS GEOID ANSICODE NAME LSAD FUNCSTAT ALAND AWATER "
        "ALAND_SQMI AWATER_SQMI INTPTLAT"
    )
    header = "\t".join(cols.split()) + "\tINTPTLONG"
    rows = [
        "TX\t1\t1\tAustin CDP\t57\tS\t5\t0\t1\t0\t30.0\t-97.0",
        "TX\t2\t2\tAustin city\t25\tA\t3\t0\t1\t0\t30.27\t-97.74",
        "NY\t3\t3\tNew York city\t25\tA\t9\t0\t1\t0\t40.66\t-73.94",
        "KY\t4\t4\tLouisville/Jefferson County metro government (balance)"
        "\t25\tA\t9\t0\t1\t0\t38.1\t-85.7",
    ]
    source = tmp_path / "g.txt"
    source.write_text("\n".join([header, *rows]), encoding="utf-8")

    table = etl.load_census(source)

    assert table["TX|austin"] == (30.27, -97.74)
    assert table["NY|new york"] == (40.66, -73.94)
    assert "KY|louisville" in table


def test_fuzzy_match_is_state_scoped_and_unmatched_are_reported():
    places = {
        "TX|mckinney": (33.2, -96.6),
        "OK|mckinney": (35.0, -97.0),
    }
    stations = [
        {"id": "1", "city": "Mc Kinney", "state": "TX", "price": 3.0},
        {"id": "2", "city": "Nowhereville", "state": "TX", "price": 3.0},
    ]

    us_area = shape(json.loads((BUILD / "usa.geojson").read_text(encoding="utf-8")))

    matched, unmatched, methods = etl.join_coordinates(stations, places, us_area)

    assert [station["lat"] for station in matched] == [33.2]
    assert methods == {"fuzzy": 1}
    assert unmatched[0]["reason"] == "no_gazetteer_match"


def test_committed_build_matches_the_supplied_csv_profile():
    report = json.loads((BUILD / "build_report.json").read_text(encoding="utf-8"))

    assert report["differs_from_expected"] == {}
    assert (report["rows_total"], report["non_us_rows_dropped"]) == (8151, 620)
    assert (report["unique_us_stops"], report["identical_duplicate_rows"]) == (
        6626,
        26,
    )
    assert report["conflicting_price_stops"] == 487

    with (BUILD / "unmatched.csv").open(newline="", encoding="utf-8") as fh:
        unmatched = list(csv.DictReader(fh))

    assert len(unmatched) == report["unmatched"]
    assert all(row["reason"] for row in unmatched)
    assert report["stations_written"] + report["unmatched"] == report["unique_us_stops"]
    assert report["match_rate"] > 0.99


def test_committed_artifacts_are_valid():
    with np.load(BUILD / "stations.npz", allow_pickle=False) as npz:
        n = len(npz["price"])

        assert {len(npz[key]) for key in npz.files} == {n}
        assert np.isfinite(npz["price"]).all()
        assert (npz["price"] > 0.5).all()
        assert (npz["price"] < 10).all()
        assert (npz["lat"] >= 24).all()
        assert (npz["lat"] <= 50).all()
        assert len(set(npz["id"])) == n

    places = json.loads((BUILD / "places.json").read_text(encoding="utf-8"))
    assert places["TX|dallas"]
    assert places["NY|new york"]
    assert len(places) > 20000

    usa = json.loads((BUILD / "usa.geojson").read_text(encoding="utf-8"))
    assert usa["type"] in ("Polygon", "MultiPolygon")

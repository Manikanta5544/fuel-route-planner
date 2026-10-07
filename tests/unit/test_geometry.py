import json
from pathlib import Path

import numpy as np
from shapely.geometry import LineString

from routing.geometry import UsaArea, decode_polyline, to_lonlat, to_xy
from routing.resolver import Places, normalize, parse_place
from tests.helpers import encode_polyline


def test_polyline_standard_vector():
    pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    np.testing.assert_allclose(pts, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])


def test_polyline_roundtrip_and_precision6():
    pts = [(32.7767, -96.797), (35.1, -90.05), (40.7128, -74.006)]
    np.testing.assert_allclose(decode_polyline(encode_polyline(pts)), pts, atol=1e-5)
    np.testing.assert_allclose(decode_polyline(encode_polyline(pts, 6), 6), pts, atol=1e-6)


def test_projection_roundtrip():
    x, y = to_xy(np.array([-96.797]), np.array([32.7767]))
    lon, lat = to_lonlat(x, y)
    assert abs(lon[0] + 96.797) < 1e-8 and abs(lat[0] - 32.7767) < 1e-8


def test_usa_area():
    usa = UsaArea(json.loads(Path("data/build/usa.geojson").read_text()))
    assert usa.contains(32.7767, -96.797) and usa.contains(40.7128, -74.006)
    assert not usa.contains(43.65, -79.38)  # Toronto
    assert not usa.contains(64.2, -149.5)  # Alaska (contiguous polygon only)
    inside = LineString(np.column_stack(to_xy(np.array([-100.0, -95.0]), np.array([35.0, 35.0]))))
    assert usa.miles_outside(inside) == 0.0
    out = LineString(np.column_stack(to_xy(np.array([-100.0, -95.0]), np.array([55.0, 55.0]))))
    assert usa.miles_outside(out) > 200


def test_place_parsing_and_resolution():
    assert normalize("St. Mary's Ft.") == "saint marys fort"
    assert parse_place("New York City, NY") == ("new york", "NY")
    assert parse_place("Dallas, TX, USA") == ("dallas", "TX")
    assert parse_place("123 Main St, Dallas, TX") is None
    assert parse_place("Dallas") is None
    places = Places({"TX|dallas": (32.78, -96.8)})
    assert places.resolve("dallas, tx") == (32.78, -96.8)
    assert places.resolve("Toronto, ON") is None

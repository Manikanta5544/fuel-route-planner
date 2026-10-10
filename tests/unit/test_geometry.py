
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString

from routing.geometry import decode_polyline, to_lonlat, to_xy
from routing.resolver import Places, normalize, parse_place
from tests.helpers import encode_polyline, us_area


def test_polyline_standard_vector():
    pts = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    np.testing.assert_allclose(
        pts,
        [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]],
    )


def test_polyline_roundtrip_and_precision6():
    pts = [(32.7767, -96.797), (35.1, -90.05), (40.7128, -74.006)]

    np.testing.assert_allclose(decode_polyline(encode_polyline(pts)), pts, atol=1e-5)
    np.testing.assert_allclose(
        decode_polyline(encode_polyline(pts, 6), 6),
        pts,
        atol=1e-6,
    )


def test_projection_roundtrip():
    x, y = to_xy(np.array([-96.797]), np.array([32.7767]))
    lon, lat = to_lonlat(x, y)

    assert abs(lon[0] + 96.797) < 1e-8
    assert abs(lat[0] - 32.7767) < 1e-8


def _line(waypoints, per_leg=60):
    pts = [waypoints[0]]

    for a, b in zip(waypoints, waypoints[1:], strict=False):
        pts += [
            (
                a[0] + (b[0] - a[0]) * k / per_leg,
                a[1] + (b[1] - a[1]) * k / per_leg,
            )
            for k in range(1, per_leg + 1)
        ]

    lat, lng = np.array(pts).T
    return LineString(np.column_stack(to_xy(lng, lat)))


def test_usa_area_endpoints():
    usa = us_area()

    assert usa.contains(32.7767, -96.797)
    assert usa.contains(40.7128, -74.006)
    assert usa.contains(24.7, -81.1)  # Marathon, Florida Keys
    assert not usa.contains(43.65, -79.38)  # Toronto
    assert not usa.contains(64.2, -149.5)  # Alaska


def test_every_station_is_inside_the_us_area():
    from stations.store import StationStore

    store = StationStore.load(Path("data/build"))
    usa = us_area()

    assert all(
        usa.contains(lat, lon)
        for lat, lon in zip(store.lat, store.lon, strict=True)
    )


@pytest.mark.parametrize(
    ("name", "waypoints"),
    [
        (
            "Miami to Key West on US-1",
            [(25.76, -80.19), (25.0, -80.45), (24.72, -81.05), (24.56, -81.78)],
        ),
        (
            "Chesapeake Bay Bridge-Tunnel",
            [(37.0, -76.05), (37.1, -75.98), (37.13, -75.99)],
        ),
        (
            "Bar Harbor to Houlton",
            [(44.39, -68.2), (45.0, -68.6), (46.1, -67.84)],
        ),
        (
            "Minneapolis to International Falls",
            [(44.98, -93.27), (46.79, -92.1), (48.6, -93.4)],
        ),
        (
            "Seattle to Bellingham",
            [(47.61, -122.33), (48.5, -122.4), (48.75, -122.48)],
        ),
    ],
)
def test_us_routes_along_coasts_and_borders_are_not_flagged(name, waypoints):
    assert us_area().foreign_miles(_line(waypoints)) < 2.0, name


@pytest.mark.parametrize(
    ("waypoints", "at_least"),
    [
        (
            [
                (42.33, -83.05),
                (42.30, -83.0),
                (42.98, -81.25),
                (43.09, -79.08),
                (42.89, -78.88),
            ],
            150,
        ),
        (
            [(47.61, -122.33), (48.99, -122.75), (49.28, -123.12)],
            20,
        ),  # Seattle to Vancouver
        (
            [(32.72, -117.16), (32.54, -117.03), (32.45, -116.9)],
            2,
        ),  # San Diego into Tijuana
    ],
)
def test_routes_through_canada_or_mexico_are_flagged(waypoints, at_least):
    assert us_area().foreign_miles(_line(waypoints)) >= at_least


def test_place_parsing_and_resolution():
    assert normalize("St. Mary's Ft.") == "saint marys fort"
    assert parse_place("New York City, NY") == ("new york", "NY", False)
    assert parse_place("Dallas, TX, USA") == ("dallas", "TX", False)
    assert parse_place("123 Main St, Dallas, TX") == ("dallas", "TX", True)
    assert parse_place("Dallas, Texas") == ("dallas", "TX", False)
    assert parse_place("Dallas TX") == ("dallas", "TX", False)
    assert parse_place("Albuquerque New Mexico") == ("albuquerque", "NM", False)
    assert parse_place("San José, CA") == ("san jose", "CA", False)
    assert parse_place("Dallas, TX 75201") == ("dallas", "TX", False)
    assert parse_place("Washington, D.C.") == ("washington", "DC", False)
    assert parse_place("Washington DC") == ("washington", "DC", False)
    assert parse_place("Dallas, Narnia") is None
    assert parse_place("Dallas") is None

    places = Places({"TX|dallas": (32.78, -96.8)})

    plain = places.resolve("dallas, tx")
    assert plain is not None
    assert (plain.lat, plain.lng, plain.precision) == (
        32.78,
        -96.8,
        "city_centroid",
    )
    assert plain.matched == "Dallas, TX"
    assert plain.note is None

    street = places.resolve("1 Elm St, Dallas, TX")
    assert street is not None
    assert street.precision == "city_centroid"
    assert "Street address ignored" in street.note
    assert places.resolve("Toronto, ON") is None


def test_real_gazetteer_resolves_common_spellings():
    from stations.store import load_places

    places = load_places(Path("data/build"))

    for text in [
        "Washington, DC",
        "Washington DC",
        "St. Louis, MO",
        "New York City, NY",
        "Dallas, Texas",
        "San José, CA",
        "Winston-Salem, NC",
        "Las Vegas NV",
    ]:
        assert places.resolve(text) is not None, text

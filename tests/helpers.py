"""Test utilities: polyline encoder, synthetic stations and straight-line routes."""

import math

import numpy as np

from routing.providers import Route
from stations.store import StationStore


def encode_polyline(points, precision: int = 5) -> str:
    out, prev = [], (0, 0)
    for lat, lng in points:
        cur = (round(lat * 10**precision), round(lng * 10**precision))
        for d in (cur[0] - prev[0], cur[1] - prev[1]):
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        prev = cur
    return "".join(out)


def haversine_miles(a, b) -> float:
    (la1, lo1), (la2, lo2) = map(lambda p: (math.radians(p[0]), math.radians(p[1])), (a, b))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def make_store(stations) -> StationStore:
    """stations: [(lat, lon, price)]"""
    n = len(stations)
    arr = np.array(stations, dtype=float).reshape(n, 3)
    ids = np.array([str(i) for i in range(n)])
    return StationStore(
        {
            "id": ids,
            "name": np.array([f"STATION {i}" for i in range(n)]),
            "address": np.array(["I-0, EXIT 0"] * n),
            "city": np.array(["Town"] * n),
            "state": np.array(["TX"] * n),
            "price": arr[:, 2],
            "lat": arr[:, 0],
            "lon": arr[:, 1],
        }
    )


def line_route(
    waypoints, step_miles: float = 1.0, road_factor: float = 1.0, provider: str = "ors"
) -> Route:
    """Densified straight legs between (lat, lng) waypoints; distance = haversine * factor."""
    pts = [waypoints[0]]
    for a, b in zip(waypoints, waypoints[1:], strict=False):
        n = max(1, int(haversine_miles(a, b) / step_miles))
        pts += [
            (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)
        ]
    miles = sum(haversine_miles(a, b) for a, b in zip(pts, pts[1:], strict=False)) * road_factor
    return Route(encode_polyline(pts), miles, miles / 60 * 3600, provider)

"""Provider-shaped test routes: dense polylines through real highway-corridor towns.

These are SYNTHETIC (straight legs with a deterministic wiggle, distance scaled to a realistic
driving figure), not recorded provider responses. They exercise the whole pipeline across US
regions without network access.
"""

import math

from routing.providers import Route
from tests.helpers import encode_polyline, haversine_miles

# name -> (start text, finish text, real-world driving miles (approx.), waypoints (lat, lng))
REGION_ROUTES = {
    "dallas-new-york": (
        "Dallas, TX",
        "New York, NY",
        1550,
        [
            (32.7767, -96.797),
            (33.4418, -94.0377),
            (34.7465, -92.2896),
            (35.1495, -90.049),
            (36.1627, -86.7816),
            (35.9606, -83.9207),
            (37.27, -79.9414),
            (40.2732, -76.8867),
            (40.7357, -74.1724),
            (40.7128, -74.006),
        ],
    ),
    "los-angeles-chicago": (
        "Los Angeles, CA",
        "Chicago, IL",
        2015,
        [
            (34.05, -118.24),
            (34.90, -117.02),
            (34.85, -114.61),
            (35.20, -111.65),
            (35.08, -106.65),
            (35.22, -101.83),
            (35.47, -97.52),
            (36.15, -95.99),
            (37.21, -93.29),
            (38.63, -90.20),
            (39.80, -89.64),
            (41.88, -87.63),
        ],
    ),
    "seattle-miami": (
        "Seattle, WA",
        "Miami, FL",
        3300,
        [
            (47.61, -122.33),
            (47.66, -117.43),
            (46.87, -114.0),
            (45.78, -108.5),
            (44.08, -103.23),
            (41.26, -95.93),
            (39.10, -94.58),
            (35.15, -90.05),
            (33.52, -86.80),
            (33.75, -84.39),
            (30.33, -81.66),
            (28.54, -81.38),
            (25.76, -80.19),
        ],
    ),
    "new-york-boston": (
        "New York, NY",
        "Boston, MA",
        215,
        [(40.71, -74.0), (41.76, -72.69), (42.26, -71.8), (42.36, -71.06)],
    ),
    "detroit-buffalo-us-only": (
        "Detroit, MI",
        "Buffalo, NY",
        340,
        [
            (42.33, -83.05),
            (42.21, -83.21),
            (41.92, -83.40),
            (41.65, -83.54),
            (41.50, -81.69),
            (41.87, -80.79),
            (42.13, -80.09),
            (42.48, -79.33),
            (42.89, -78.88),
        ],
    ),
    "san-francisco-las-vegas": (
        "San Francisco, CA",
        "Las Vegas, NV",
        570,
        [
            (37.77, -122.42),
            (37.64, -121.0),
            (36.74, -119.79),
            (35.37, -119.02),
            (34.90, -117.02),
            (35.27, -116.07),
            (36.17, -115.14),
        ],
    ),
    "houston-atlanta": (
        "Houston, TX",
        "Atlanta, GA",
        790,
        [
            (29.76, -95.37),
            (30.08, -94.13),
            (30.22, -92.02),
            (30.45, -91.15),
            (30.69, -88.04),
            (32.37, -86.30),
            (33.75, -84.39),
        ],
    ),
    "phoenix-denver": (
        "Phoenix, AZ",
        "Denver, CO",
        840,
        [
            (33.45, -112.07),
            (35.20, -111.65),
            (35.08, -106.65),
            (35.69, -105.94),
            (36.9, -104.44),
            (38.25, -104.61),
            (39.74, -104.99),
        ],
    ),
}

# Smooth the one route that hugs the border so the synthetic wiggle cannot push it into Canada.
WIGGLE = {"detroit-buffalo-us-only": 0.25}

# Detroit to Buffalo the short way: through Ontario (what a plain shortest-route query returns).
VIA_ONTARIO = [
    (42.33, -83.05),
    (42.30, -83.0),
    (42.40, -82.19),
    (42.98, -81.25),
    (43.26, -79.87),
    (43.09, -79.08),
    (42.89, -78.88),
]


def wiggled_route(
    waypoints, miles: float, provider: str = "osrm", seed: float = 1.0, wiggle: float = 1.0
) -> Route:
    """Densify legs (0.5 mi), add smooth lateral wiggle (never at waypoints), scale to `miles`."""
    pts, travelled = [waypoints[0]], 0.0
    for a, b in zip(waypoints, waypoints[1:], strict=False):
        leg = haversine_miles(a, b)
        n = max(1, int(leg / 0.5))
        dlat, dlng = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dlat, dlng)
        nlat, nlng = -dlng / norm, dlat / norm
        for k in range(1, n + 1):
            s = travelled + leg * k / n
            off = (
                0.0
                if k == n
                else sum(
                    wiggle * amp * math.sin(2 * math.pi * s / wl + seed * ph)
                    for wl, amp, ph in ((8.0, 0.012, 1.0), (25.0, 0.03, 2.0), (70.0, 0.06, 3.0))
                )
            )
            pts.append((a[0] + dlat * k / n + nlat * off, a[1] + dlng * k / n + nlng * off))
        travelled += leg
    return Route(encode_polyline(pts), miles, miles / 58 * 3600, provider)

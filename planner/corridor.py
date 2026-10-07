"""Route bundle (display geometry + candidate stations with along-route position) and planning."""

import time
from dataclasses import dataclass

import numpy as np
import orjson
import shapely
from shapely import STRtree
from shapely.geometry import LineString

from planner.optimizer import effective_price, plan_stops
from routing.geometry import METRES_PER_MILE, decode_polyline, to_lonlat, to_xy
from routing.providers import Route

CORRIDORS = (8.0, 15.0, 30.0, 50.0)
QUERY_TOLERANCE_M = 500.0
DISPLAY_TOLERANCE_M = 150.0
LEAVES_USA_MILES = 3.0
_EMPTY = np.empty(0)


class Infeasible(Exception):
    """No fuel plan exists even with the widest corridor."""


@dataclass(slots=True)
class Bundle:
    provider: str
    distance_miles: float
    duration_s: float
    polyline: str
    geometry_json: bytes
    bbox: list[float]
    leaves_usa: bool
    idx: np.ndarray  # store indices of candidates within the widest corridor, sorted by pos
    pos: np.ndarray  # miles along the route (scaled to the provider distance)
    off: np.ndarray  # miles off the route
    created: float = 0.0
    ttl: float = 0.0
    fallback: bool = False


@dataclass(slots=True)
class Stop:
    idx: int
    mile: float
    off: float
    gallons: float


@dataclass(slots=True)
class PlanResult:
    corridor_miles: float
    candidates: int
    stops: list[Stop]
    cheapest_idx: int | None
    corridor_ms: float = 0.0
    optimize_ms: float = 0.0


def build_bundle(route: Route, store, usa) -> Bundle:
    ll = decode_polyline(route.polyline)
    line = LineString(np.column_stack(to_xy(ll[:, 1], ll[:, 0])))
    query = shapely.simplify(line, QUERY_TOLERANCE_M)
    qc = shapely.get_coordinates(query)
    seg_len = np.hypot(*np.diff(qc, axis=0).T)
    cum = np.concatenate(([0.0], np.cumsum(seg_len)))
    scale = route.distance_miles / cum[-1] if cum[-1] > 0 else 0.0

    idx, pos, off = np.empty(0, dtype=int), _EMPTY, _EMPTY
    near = store.tree.query(query, predicate="dwithin", distance=CORRIDORS[-1] * METRES_PER_MILE)
    if near.size:
        segments = shapely.linestrings(np.stack([qc[:-1], qc[1:]], axis=1))
        pairs, dist = STRtree(segments).query_nearest(
            store.points[near], return_distance=True, all_matches=False
        )
        order = np.argsort(pairs[0])
        seg, dist = pairs[1][order], dist[order]
        a, ab = qc[seg], qc[seg + 1] - qc[seg]
        t = np.einsum("ij,ij->i", store.xy[near] - a, ab) / np.maximum(
            np.einsum("ij,ij->i", ab, ab), 1e-9
        )
        along = cum[seg] + np.clip(t, 0, 1) * seg_len[seg]
        ranked = np.argsort(along, kind="stable")
        idx, pos, off = near[ranked], along[ranked] * scale, dist[ranked] / METRES_PER_MILE

    shown = shapely.get_coordinates(shapely.simplify(line, DISPLAY_TOLERANCE_M))
    lon, lat = to_lonlat(shown[:, 0], shown[:, 1])
    geometry = orjson.dumps(np.column_stack((lon, lat)).round(5), option=orjson.OPT_SERIALIZE_NUMPY)
    return Bundle(
        provider=route.provider,
        distance_miles=route.distance_miles,
        duration_s=route.duration_s,
        polyline=route.polyline,
        geometry_json=geometry,
        bbox=[
            round(float(v), 5)
            for v in (ll[:, 1].min(), ll[:, 0].min(), ll[:, 1].max(), ll[:, 0].max())
        ],
        leaves_usa=usa.miles_outside(query) > LEAVES_USA_MILES if usa else False,
        idx=idx,
        pos=pos,
        off=off,
        created=time.time(),
    )


def full_geometry_json(bundle: Bundle) -> bytes:
    """Unsimplified geometry, rebuilt from the stored polyline (not cached)."""
    if not bundle.polyline:
        return bundle.geometry_json
    lnglat = np.ascontiguousarray(decode_polyline(bundle.polyline)[:, ::-1].round(5))
    return orjson.dumps(lnglat, option=orjson.OPT_SERIALIZE_NUMPY)


def trivial_bundle(lat: float, lng: float) -> Bundle:
    """Start equals finish: a zero-length route, no provider call."""
    point = [round(lng, 5), round(lat, 5)]
    return Bundle(
        "none",
        0.0,
        0.0,
        "",
        orjson.dumps([point, point]),
        [*point, *point],
        False,
        np.empty(0, dtype=int),
        _EMPTY,
        _EMPTY,
        created=time.time(),
    )


def plan_route(
    bundle: Bundle, store, *, mpg: float, max_range: float, free_offset: float, g_ref: float
) -> PlanResult:
    """Plan stops, widening the corridor 8 -> 15 -> 30 -> 50 miles only when infeasible."""
    corridor_s = optimize_s = 0.0
    for width in CORRIDORS:
        t0 = time.perf_counter()
        keep = bundle.off <= width
        idx, pos, off = bundle.idx[keep], bundle.pos[keep], bundle.off[keep]
        price = store.price[idx]
        eff = effective_price(price, off, mpg, free_offset, g_ref)
        t1 = time.perf_counter()
        buys = plan_stops(pos, eff, bundle.distance_miles, mpg=mpg, max_range=max_range)
        corridor_s += t1 - t0
        optimize_s += time.perf_counter() - t1
        if buys is not None:
            stops = [Stop(int(idx[i]), float(pos[i]), float(off[i]), g) for i, g in buys]
            cheapest = int(idx[np.argmin(price)]) if len(idx) else None
            return PlanResult(
                width, int(len(idx)), stops, cheapest, corridor_s * 1e3, optimize_s * 1e3
            )
    raise Infeasible

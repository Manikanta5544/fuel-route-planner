"""Polyline decoding, EPSG:5070 projection and the US territory checks. Coordinates: (lat, lng)."""

import threading

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, shape

METRES_PER_MILE = 1609.344
_local = threading.local()


def _transformers() -> tuple[Transformer, Transformer]:
    if not hasattr(_local, "t"):
        _local.t = (
            Transformer.from_crs(4326, 5070, always_xy=True),
            Transformer.from_crs(5070, 4326, always_xy=True),
        )
    return _local.t


def to_xy(lon, lat) -> tuple[np.ndarray, np.ndarray]:
    return _transformers()[0].transform(lon, lat)


def to_lonlat(x, y) -> tuple[np.ndarray, np.ndarray]:
    return _transformers()[1].transform(x, y)


def decode_polyline(encoded: str, precision: int = 5) -> np.ndarray:
    """Vectorised Google polyline decode -> float array of shape (n, 2) as (lat, lng)."""
    chars = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    ends = np.flatnonzero(chars < 32)
    starts = np.concatenate(([0], ends[:-1] + 1))
    number = np.repeat(np.arange(len(ends)), ends - starts + 1)
    shift = (np.arange(len(chars)) - starts[number]) * 5
    raw = np.add.reduceat((chars & 31) << shift, starts)
    deltas = np.where(raw & 1, ~(raw >> 1), raw >> 1).reshape(-1, 2)
    return np.cumsum(deltas, axis=0) / 10.0**precision


class UsaArea:
    """Where requests may start/end (contiguous US) and what counts as leaving the country."""

    def __init__(self, us_geojson: dict, foreign_geojson: dict, buffer_deg: float = 0.02):
        self._us = shape(us_geojson).buffer(buffer_deg)
        foreign = shape(foreign_geojson)
        self._foreign = shapely.transform(
            foreign, lambda c: np.column_stack(to_xy(c[:, 0], c[:, 1]))
        )
        shapely.prepare(self._us)
        shapely.prepare(self._foreign)
        # Prepared geometries build their index on first use; do it here, before worker threads.
        self.contains(39.0, -98.0)
        shapely.intersects(self._foreign, shapely.points(0.0, 0.0))

    def contains(self, lat: float, lng: float) -> bool:
        return bool(shapely.contains_xy(self._us, lng, lat))

    def foreign_miles(self, line_xy: LineString) -> float:
        """Longest unbroken stretch of the route (EPSG:5070) inside Canada or Mexico, in miles."""
        if not shapely.intersects(self._foreign, line_xy):
            return 0.0
        inside = shapely.intersection(line_xy, self._foreign)
        return float(max(shapely.length(g) for g in shapely.get_parts(inside))) / METRES_PER_MILE

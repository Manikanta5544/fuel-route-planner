"""Polyline decoding, EPSG:5070 projection and the USA polygon check. Coordinates: (lat, lng)."""

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
    """Contiguous-US polygon buffered by ~0.1 degrees."""

    def __init__(self, geojson: dict, buffer_deg: float = 0.1):
        self._ll = shape(geojson).buffer(buffer_deg)
        shapely.prepare(self._ll)
        self._xy = shapely.transform(self._ll, lambda c: np.column_stack(to_xy(c[:, 0], c[:, 1])))
        shapely.prepare(self._xy)

    def contains(self, lat: float, lng: float) -> bool:
        return bool(shapely.contains_xy(self._ll, lng, lat))

    def miles_outside(self, line_xy: LineString) -> float:
        if shapely.contains(self._xy, line_xy):
            return 0.0
        return line_xy.difference(self._xy).length / METRES_PER_MILE

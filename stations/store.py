"""Immutable in-memory station store: numpy arrays plus an STRtree in EPSG:5070."""

import json
from pathlib import Path

import numpy as np
import shapely
from shapely import STRtree

from routing.geometry import UsaArea, to_xy
from routing.resolver import Places


class StationStore:
    def __init__(self, arrays: dict[str, np.ndarray]):
        self.id, self.name, self.address = arrays["id"], arrays["name"], arrays["address"]
        self.city, self.state, self.price = arrays["city"], arrays["state"], arrays["price"]
        self.lat, self.lon = arrays["lat"], arrays["lon"]
        self.xy = np.column_stack(to_xy(self.lon, self.lat))
        self.points = shapely.points(self.xy)
        self.tree = STRtree(self.points)

    def __len__(self) -> int:
        return len(self.price)

    @classmethod
    def load(cls, build_dir: Path) -> "StationStore | None":
        path = build_dir / "stations.npz"
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as npz:
            return cls({k: npz[k] for k in npz.files})


def load_places(build_dir: Path) -> Places | None:
    path = build_dir / "places.json"
    return Places(json.loads(path.read_text())) if path.exists() else None


def load_usa(build_dir: Path) -> UsaArea | None:
    path = build_dir / "usa.geojson"
    return UsaArea(json.loads(path.read_text())) if path.exists() else None

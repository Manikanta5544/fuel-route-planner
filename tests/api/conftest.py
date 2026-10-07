import json
from pathlib import Path

import pytest
import respx
from django.conf import settings
from django.test import AsyncClient, override_settings

import api.apps
import api.middleware
from api.apps import build_runtime
from routing.resolver import Places
from stations.store import load_usa
from tests.helpers import line_route, make_store

ORS, OSRM = "https://ors.test", "https://osrm.test"
LAT = 33.0  # synthetic east-west highway at lat 33 between lon -100 and -80
STATIONS = [
    (LAT, lon, price)
    for lon, price in [(-97, 3.50), (-94, 3.20), (-91, 3.60), (-88, 3.00), (-85, 3.40), (-82, 3.10)]
]
PLACES = {"TX|westville": (LAT, -100.0), "SC|eastville": (LAT, -80.0), "TX|midville": (LAT, -96.0)}
ROUTE = line_route([(LAT, -100.0), (LAT, -80.0)], road_factor=1.1)
SHORT = line_route([(LAT, -100.0), (LAT, -96.0)], road_factor=1.1)
FIXTURES = Path(__file__).parent.parent / "fixtures"


def ors_json(route):
    summary = {"distance": route.distance_miles * 1609.344, "duration": route.duration_s}
    return {"routes": [{"geometry": route.polyline, "summary": summary}]}


def osrm_json(route):
    top = {
        "geometry": route.polyline,
        "distance": route.distance_miles * 1609.344,
        "duration": route.duration_s,
    }
    return {"code": "Ok", "routes": [top]}


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.ors = m.post(f"{ORS}/v2/directions/driving-car").respond(json=ors_json(ROUTE))
        m.osrm = m.get(url__regex=rf"{OSRM}/route/v1/driving/.*").respond(json=osrm_json(ROUTE))
        yield m


@pytest.fixture
def client(mock, monkeypatch):
    api.middleware._windows.clear()
    with override_settings(
        ALLOWED_HOSTS=["testserver"],
        ORS_API_KEY="k",
        ORS_BASE_URL=ORS,
        OSRM_BASE_URL=OSRM,
        RATE_LIMIT_PER_MIN=1000,
        REDIS_URL="",
    ):
        runtime = build_runtime(make_store(STATIONS), Places(PLACES), load_usa(settings.BUILD_DIR))
        monkeypatch.setattr(api.apps, "_runtime", runtime)
        yield AsyncClient()


@pytest.fixture
def real_client(mock, monkeypatch):
    """Real station artifact + OSRM stub carrying the synthetic Dallas -> New York route."""
    fx = json.loads((FIXTURES / "dallas_ny_route.json").read_text())
    from routing.providers import Route

    route = Route(fx["polyline"], fx["distance_miles"], fx["duration_s"], "osrm")
    mock.osrm.respond(json=osrm_json(route))
    api.middleware._windows.clear()
    with override_settings(
        ALLOWED_HOSTS=["testserver"],
        ORS_API_KEY="",
        OSRM_BASE_URL=OSRM,
        RATE_LIMIT_PER_MIN=1000,
        REDIS_URL="",
    ):
        monkeypatch.setattr(api.apps, "_runtime", None)
        yield AsyncClient()
        monkeypatch.setattr(api.apps, "_runtime", None)

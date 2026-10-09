"""Eight cross-country trips through the real station artifact (routes are synthetic, see routes.py)."""

import re

import httpx
import pytest
import respx
from django.test import AsyncClient, override_settings

import api.apps
import api.middleware
from tests.api.conftest import OSRM, osrm_json
from tests.api.test_route_api import check_plan, post
from tests.helpers import haversine_miles
from tests.routes import REGION_ROUTES, WIGGLE, wiggled_route


@pytest.fixture
def regions(monkeypatch):
    """OSRM stub that answers each trip with its fixture route, chosen by the requested ends."""
    api.middleware._windows.clear()
    monkeypatch.setattr(api.apps, "_runtime", None)
    with (
        respx.mock(assert_all_called=False) as m,
        override_settings(
            ALLOWED_HOSTS=["testserver"],
            ORS_API_KEY="",
            OSRM_BASE_URL=OSRM,
            RATE_LIMIT_PER_MIN=0,
            REDIS_URL="",
        ),
    ):
        rt = api.apps.get_runtime()
        by_ends = {}
        for name, (a, b, miles, waypoints) in REGION_ROUTES.items():
            ends = (rt.places.resolve(a), rt.places.resolve(b))
            # a provider snaps the requested point to a road, so the route starts/ends there
            waypoints = [(ends[0].lat, ends[0].lng), *waypoints[1:-1], (ends[1].lat, ends[1].lng)]
            route = wiggled_route(waypoints, miles, wiggle=WIGGLE.get(name, 1.0))
            by_ends[tuple((round(e.lng, 4), round(e.lat, 4)) for e in ends)] = route

        def handler(request):
            nums = [float(x) for x in re.findall(r"-?\d+\.\d+", request.url.path)]
            key = ((round(nums[0], 4), round(nums[1], 4)), (round(nums[2], 4), round(nums[3], 4)))
            return httpx.Response(200, json=osrm_json(by_ends[key]))

        m.osrm = m.get(url__regex=rf"{OSRM}/route/v1/driving/.*").mock(side_effect=handler)
        m.client = AsyncClient()
        yield m
    monkeypatch.setattr(api.apps, "_runtime", None)


FEASIBLE = [n for n in REGION_ROUTES if n != "san-francisco-las-vegas"]


@pytest.mark.parametrize("name", FEASIBLE)
async def test_region_trip(regions, name):
    start, finish, real_miles, _ = REGION_ROUTES[name]
    r = await post(regions.client, {"start": start, "finish": finish})
    assert r.status_code == 200, r.content
    j = r.json()
    check_plan(j)
    assert j["route"]["distance_miles"] == pytest.approx(real_miles, rel=0.01)
    routing = j["meta"]["routing"]
    assert (routing["routing_calls"], routing["us_only_reroute"]) == (1, False)

    lng0, lat0 = j["route"]["geometry"]["coordinates"][0]  # GeoJSON order is [lng, lat]
    lng1, lat1 = j["route"]["geometry"]["coordinates"][-1]
    loc = j["meta"]["locations"]
    assert haversine_miles((lat0, lng0), (loc["start"]["lat"], loc["start"]["lng"])) < 2
    assert haversine_miles((lat1, lng1), (loc["finish"]["lat"], loc["finish"]["lng"])) < 2
    assert -125 < lng0 < -66 and 24 < lat0 < 50

    width = j["meta"]["planning"]["corridor_miles_used"]
    assert all(s["off_route_miles"] <= width + 0.05 for s in j["fuel_stops"])
    assert all(1.5 < s["price_per_gallon"] < 6 for s in j["fuel_stops"])
    assert len({s["station_id"] for s in j["fuel_stops"]}) == len(j["fuel_stops"])
    need = real_miles / 10
    if real_miles > 500:
        assert j["fuel_stops"] and j["fuel"]["cash_spent_at_stops_usd"] > 0
        assert j["fuel"]["gallons_purchased"] >= need - 50 - 0.1
    else:
        assert j["fuel_stops"] == [] and j["fuel"]["estimated_total_fuel_cost_usd"] > 0

    warm = (await post(regions.client, {"start": start, "finish": finish})).json()
    assert warm["meta"]["routing"]["routing_calls"] == 0 and warm["fuel_stops"] == j["fuel_stops"]


async def test_california_coverage_gap_is_reported_precisely(regions):
    """The supplied CSV has 8 California stations (Imperial Valley only), so SF -> Las Vegas has
    no station inside the first 500 miles; the API must say so instead of inventing a plan."""
    start, finish, _, _ = REGION_ROUTES["san-francisco-las-vegas"]
    r = await post(regions.client, {"start": start, "finish": finish})
    err = r.json()["error"]
    assert (r.status_code, err["code"]) == (422, "NO_FEASIBLE_FUEL_PLAN")
    assert "no station within 50 miles of the route between mile" in err["message"]
    assert "500-mile range" in err["message"]

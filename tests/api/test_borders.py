"""US-only behaviour: a route that enters Canada/Mexico is rerouted (ORS) or refused, never planned."""

import json

import httpx
import pytest
import respx
from django.test import AsyncClient, override_settings

import api.apps
import api.middleware
from api.apps import build_runtime
from routing.resolver import Places
from tests.api.conftest import ORS, OSRM, ors_json, osrm_json
from tests.api.test_route_api import check_plan, post
from tests.helpers import make_store, us_area
from tests.routes import REGION_ROUTES, VIA_ONTARIO, WIGGLE, wiggled_route

PLACES = {"MI|detroit": (42.33, -83.05), "NY|buffalo": (42.89, -78.88)}
STATIONS = [(41.65, -83.54, 3.20), (41.50, -81.69, 3.10), (42.13, -80.09, 3.30)]
BODY = {"start": "Detroit, MI", "finish": "Buffalo, NY"}
US_ONLY = wiggled_route(
    REGION_ROUTES["detroit-buffalo-us-only"][3],
    340,
    "ors",
    wiggle=WIGGLE["detroit-buffalo-us-only"],
)
CANADA = wiggled_route(VIA_ONTARIO, 250, "ors")


def uses_border_avoidance(request) -> bool:
    return json.loads(request.content).get("options", {}).get("avoid_borders") == "all"


@pytest.fixture
def world(monkeypatch):
    """ORS (key set) returns Ontario normally and a US route when avoid_borders is requested."""
    with respx.mock(assert_all_called=False) as m:
        m.ors = m.post(f"{ORS}/v2/directions/driving-car")
        m.ors.mock(
            side_effect=lambda req: httpx.Response(
                200, json=ors_json(US_ONLY if uses_border_avoidance(req) else CANADA)
            )
        )
        m.osrm = m.get(url__regex=rf"{OSRM}/route/v1/driving/.*").respond(json=osrm_json(CANADA))
        api.middleware._windows.clear()
        with override_settings(
            ALLOWED_HOSTS=["testserver"],
            ORS_API_KEY="k",
            ORS_BASE_URL=ORS,
            OSRM_BASE_URL=OSRM,
            RATE_LIMIT_PER_MIN=0,
            REDIS_URL="",
        ):
            monkeypatch.setattr(
                api.apps, "_runtime", build_runtime(make_store(STATIONS), Places(PLACES), us_area())
            )
            m.client = AsyncClient()
            yield m


async def test_ors_reroutes_around_the_border_and_caches_the_us_route(world):
    j = (await post(world.client, BODY)).json()
    assert world.ors.call_count == 2 and world.osrm.call_count == 0
    first, second = (json.loads(c.request.content) for c in world.ors.calls)
    assert "options" not in first and second["options"] == {"avoid_borders": "all"}
    routing = j["meta"]["routing"]
    assert (routing["routing_calls"], routing["us_only_reroute"], routing["provider"]) == (
        2,
        True,
        "ors",
    )
    assert j["route"]["distance_miles"] == 340.0  # the US route, not the 250-mile Ontario one
    check_plan(j)
    warm = (await post(world.client, BODY)).json()
    assert warm["meta"]["routing"]["routing_calls"] == 0 and world.ors.call_count == 2
    assert warm["meta"]["routing"]["us_only_reroute"] is True


async def test_without_ors_a_border_crossing_route_is_refused_not_planned(world):
    with override_settings(ORS_API_KEY=""):
        monkey_runtime = build_runtime(make_store(STATIONS), Places(PLACES), us_area())
        api.apps._runtime = monkey_runtime
        r = await post(world.client, BODY)
        again = await post(world.client, BODY)
    err = r.json()["error"]
    assert (r.status_code, err["code"]) == (422, "ROUTE_LEAVES_SUPPORTED_AREA")
    assert "fuel_stops" not in r.json() and err["request_id"]
    assert world.osrm.call_count == 2 and again.status_code == 422  # refusals are not cached


async def test_reroute_that_still_leaves_the_us_is_refused(world):
    world.ors.mock(side_effect=lambda req: httpx.Response(200, json=ors_json(CANADA)))
    r = await post(world.client, BODY)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "ROUTE_LEAVES_SUPPORTED_AREA")
    assert world.ors.call_count == 2


async def test_provider_with_no_border_free_route_is_refused(world):
    world.ors.mock(
        side_effect=lambda req: (
            httpx.Response(200, json=ors_json(CANADA))
            if not uses_border_avoidance(req)
            else httpx.Response(404, json={"error": {"code": 2010}})
        )
    )
    r = await post(world.client, BODY)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "ROUTE_LEAVES_SUPPORTED_AREA")


async def test_transient_failure_during_reroute_is_503_and_nothing_is_cached(world):
    flaky = {"on": True}

    def handler(req):
        if not uses_border_avoidance(req):
            return httpx.Response(200, json=ors_json(CANADA))
        return httpx.Response(503) if flaky["on"] else httpx.Response(200, json=ors_json(US_ONLY))

    world.ors.mock(side_effect=handler)
    r = await post(world.client, BODY)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ROUTING_UNAVAILABLE")
    flaky["on"] = False
    assert (await post(world.client, BODY)).status_code == 200


async def test_fallback_route_through_canada_is_rerouted_within_the_three_call_cap(world):
    world.ors.mock(
        side_effect=lambda req: (
            httpx.Response(200, json=ors_json(US_ONLY))
            if uses_border_avoidance(req)
            else httpx.Response(503)
        )
    )
    j = (await post(world.client, BODY)).json()  # ORS 503 -> OSRM (Ontario) -> ORS border-free
    assert j["meta"]["routing"]["routing_calls"] == 3 and j["meta"]["routing"]["us_only_reroute"]
    assert (world.ors.call_count, world.osrm.call_count) == (2, 1)
    assert j["meta"]["routing"]["fallback_used"] is False  # final route came from the primary


async def test_lower_call_cap_turns_the_reroute_into_503(world):
    with override_settings(MAX_ROUTING_CALLS=1):
        r = await post(world.client, BODY)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ROUTING_UNAVAILABLE")

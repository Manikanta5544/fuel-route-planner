import gzip
import json

import pytest
from django.test import override_settings

import api.views
from tests.api.conftest import SHORT, ors_json

GZIP = {"Accept-Encoding": "gzip"}


async def post(client, body, query="", **kw):
    data = body if isinstance(body, str) else json.dumps(body)
    return await client.post(
        f"/api/v1/route{query}", data=data, content_type="application/json", **kw
    )


def coords(lat, lng):
    return {"lat": lat, "lng": lng}


def check_plan(j, max_range=500.0):
    """Invariants every successful response must satisfy."""
    marks = [s["mile_marker"] for s in j["fuel_stops"]]
    assert marks == sorted(marks) and [s["order"] for s in j["fuel_stops"]] == list(
        range(1, len(marks) + 1)
    )
    edges = [0.0, *marks, j["route"]["distance_miles"]]
    assert all(b - a <= max_range + 0.1 for a, b in zip(edges, edges[1:], strict=False))
    f = j["fuel"]
    assert f["gallons_purchased"] == pytest.approx(
        sum(s["gallons_purchased"] for s in j["fuel_stops"]), abs=0.05
    )
    assert f["cash_spent_at_stops_usd"] == pytest.approx(
        sum(s["cost_usd"] for s in j["fuel_stops"]), abs=0.05
    )
    start_tank = f["initial_fuel"]["gallons"]
    assert f["gallons_purchased"] + start_tank == pytest.approx(f["gallons_consumed"], abs=0.05)
    if f["estimated_total_fuel_cost_usd"] is not None:
        ref = f["initial_fuel"]["reference_price_per_gallon"] or 0
        expected = f["cash_spent_at_stops_usd"] + start_tank * ref
        assert f["estimated_total_fuel_cost_usd"] == pytest.approx(expected, abs=0.1)


async def test_cold_then_warm_with_city_and_coordinate_inputs(client, mock):
    cold = await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})
    assert cold.status_code == 200
    j = cold.json()
    assert j["meta"]["routing"] | {} == {
        "provider": "ors",
        "routing_calls": 1,
        "geocode_calls": 0,
        "external_calls": 1,
        "fallback_used": False,
        "us_only_reroute": False,
    }
    assert (j["meta"]["cache"]["route"], j["meta"]["cache"]["layer"]) == ("miss", None)
    check_plan(j)
    assert len(j["fuel_stops"]) >= 2
    warm = await post(client, {"start": coords(33.0, -100.0), "finish": coords(33.0, -80.0)})
    w = warm.json()
    assert (w["meta"]["routing"]["routing_calls"], w["meta"]["cache"]["layer"]) == (0, "l1")
    assert w["fuel_stops"] == j["fuel_stops"]
    assert mock.ors.call_count == 1


async def test_response_shape_headers_and_gzip(client):
    r = await post(
        client, {"start": "Westville, TX", "finish": "Eastville, SC", "mpg": 12}, headers=GZIP
    )
    assert r["Content-Encoding"] == "gzip" and r["X-Request-ID"]
    assert all(
        k in r["Server-Timing"] for k in ("route", "corridor", "optimize", "serialize", "total")
    )
    j = json.loads(gzip.decompress(r.content))
    assert set(j) == {"route", "fuel_stops", "fuel", "map_url", "meta"}
    assert set(j["route"]) == {
        "distance_miles",
        "duration_seconds",
        "geometry",
        "geometry_detail",
        "bbox",
    }
    assert j["route"]["geometry"]["type"] == "LineString" and len(j["route"]["bbox"]) == 4
    assert set(j["meta"]) == {
        "request_id",
        "routing",
        "cache",
        "planning",
        "performance",
        "assumptions",
        "locations",
        "warnings",
    }
    assert j["meta"]["assumptions"]["mpg"] == 12 and "mpg=12" in j["map_url"]
    stop = j["fuel_stops"][0]
    assert {
        "order",
        "station_id",
        "name",
        "address",
        "city",
        "state",
        "lat",
        "lng",
        "location_precision",
        "mile_marker",
        "off_route_miles",
        "price_per_gallon",
        "gallons_purchased",
        "cost_usd",
    } <= set(stop)
    assert set(j["fuel"]) == {
        "gallons_consumed",
        "gallons_purchased",
        "cash_spent_at_stops_usd",
        "average_purchase_price_per_gallon",
        "initial_fuel",
        "estimated_total_fuel_cost_usd",
        "estimate_basis",
    }
    check_plan(j)


async def test_request_id_is_echoed_and_unsafe_ids_replaced(client):
    ok = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    bad = await client.get("/healthz", headers={"X-Request-ID": "bad id!"})
    assert ok["X-Request-ID"] == "abc-123" and bad["X-Request-ID"] != "bad id!"


async def test_geometry_detail_full_has_more_points(client):
    simple = (await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})).json()
    full = (
        await post(
            client, {"start": "Westville, TX", "finish": "Eastville, SC"}, "?geometry_detail=full"
        )
    ).json()
    assert full["route"]["geometry_detail"] == "full"
    assert len(full["route"]["geometry"]["coordinates"]) >= len(
        simple["route"]["geometry"]["coordinates"]
    )
    bad = await post(
        client, {"start": "Westville, TX", "finish": "Eastville, SC"}, "?geometry_detail=x"
    )
    assert bad.status_code == 400


async def test_short_trip_costs_a_real_figure(client, mock):
    mock.ors.respond(json=ors_json(SHORT))
    j = (await post(client, {"start": "Westville, TX", "finish": "Midville, TX"})).json()
    assert j["fuel_stops"] == [] and j["fuel"]["cash_spent_at_stops_usd"] == 0
    f = j["fuel"]
    assert f["initial_fuel"]["valuation"] == "cheapest_corridor_station"
    assert f["initial_fuel"]["reference_station"]["station_id"]
    assert f["estimated_total_fuel_cost_usd"] == pytest.approx(
        f["gallons_consumed"] * f["initial_fuel"]["reference_price_per_gallon"], abs=0.02
    )


async def test_start_equals_finish_makes_no_provider_call(client, mock):
    j = (await post(client, {"start": "Westville, TX", "finish": coords(33.0, -100.0)})).json()
    assert j["route"]["distance_miles"] == 0 and j["fuel_stops"] == []
    assert j["meta"]["routing"]["routing_calls"] == 0 and mock.ors.call_count == 0
    assert j["fuel"]["estimated_total_fuel_cost_usd"] == 0


async def test_start_tank_billing_excluded(client):
    with override_settings(START_TANK_BILLING="excluded"):
        j = (await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})).json()
    f = j["fuel"]
    assert (
        f["estimated_total_fuel_cost_usd"] is None
        and not f["initial_fuel"]["cost_included_in_estimate"]
    )
    assert f["cash_spent_at_stops_usd"] > 0
    check_plan(j)


async def test_reserve_shrinks_usable_range(client):
    with override_settings(RESERVE_MILES=100):
        j = (await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})).json()
    check_plan(j, max_range=400.0)
    assert j["meta"]["assumptions"]["reserve_miles"] == 100


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"start": coords(43.65, -79.38), "finish": "Eastville, SC"},
            "LOCATION_OUTSIDE_SUPPORTED_AREA",
        ),
        ({"start": "Westville, TX", "finish": "Toronto, ON"}, "LOCATION_OUTSIDE_SUPPORTED_AREA"),
        ({"start": "Nowhere, TX", "finish": "Eastville, SC"}, "LOCATION_OUTSIDE_SUPPORTED_AREA"),
        (
            {"start": "1 Main St, Dallas, TX", "finish": "Eastville, SC"},
            "LOCATION_OUTSIDE_SUPPORTED_AREA",
        ),
    ],
)
async def test_outside_supported_area_is_rejected_before_any_upstream_call(
    client, mock, body, code
):
    r = await post(client, body)
    err = r.json()["error"]
    assert (r.status_code, err["code"]) == (422, code) and err["request_id"] and err["message"]
    assert mock.ors.call_count == 0 and mock.osrm.call_count == 0


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        {"start": "Westville, TX"},
        {"start": "", "finish": "Eastville, SC"},
        {"start": "Westville, TX", "finish": "Eastville, SC", "mpg": 0},
        {"start": "Westville, TX", "finish": "Eastville, SC", "max_range_miles": 10},
        {"start": "Westville, TX", "finish": "Eastville, SC", "max_range_miles": 501},
        {"start": "Westville, TX", "finish": "Eastville, SC", "max_range_miles": 2000},
        {"start": "Westville, TX", "finish": "Eastville, SC", "extra": 1},
        {"start": coords(95, 0), "finish": "Eastville, SC"},
        {"start": "Westville, TX", "finish": "Eastville, SC", "pad": "x" * 5000},
    ],
)
async def test_invalid_requests_return_400(client, mock, body):
    r = await post(client, body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST"
    assert mock.ors.call_count == 0


async def test_not_routable_maps_to_422(client, mock):
    mock.ors.respond(404, json={"error": {"code": 2010, "message": "secret internals"}})
    r = await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "LOCATION_NOT_ROUTABLE")
    assert "secret" not in r.content.decode() and mock.osrm.call_count == 0


async def test_infeasible_plan_maps_to_422(client):
    r = await post(
        client, {"start": "Westville, TX", "finish": "Eastville, SC", "max_range_miles": 100}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "NO_FEASIBLE_FUEL_PLAN")


async def test_provider_outage_maps_to_503_and_fallback_works(client, mock):
    mock.ors.respond(503)
    ok = (await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})).json()
    assert ok["meta"]["routing"]["fallback_used"] and ok["meta"]["routing"]["routing_calls"] == 2
    assert (
        ok["meta"]["routing"]["provider"] == "osrm" and ok["meta"]["routing"]["external_calls"] == 2
    )
    mock.osrm.respond(502)
    r = await post(client, {"start": "Westville, TX", "finish": coords(33.0, -81.0)})
    assert (r.status_code, r.json()["error"]["code"]) == (503, "ROUTING_UNAVAILABLE")


async def test_unexpected_errors_are_generic(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database password is hunter2")

    monkeypatch.setattr(api.views, "plan_route", boom)
    r = await post(client, {"start": "Westville, TX", "finish": "Eastville, SC"})
    assert r.status_code == 500 and "hunter2" not in r.content.decode()
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"


async def test_rate_limit(client):
    with override_settings(RATE_LIMIT_PER_MIN=2):
        codes = [(await client.get("/api/v1/route/map")).status_code for _ in range(3)]
        health = (await client.get("/healthz")).status_code
    assert codes == [200, 200, 429] and health == 200


async def test_rate_limit_error_format(client):
    with override_settings(RATE_LIMIT_PER_MIN=1):
        await client.get("/api/v1/route/map")
        r = await client.get("/api/v1/route/map")
    assert r.json()["error"]["code"] == "RATE_LIMITED" and r["Retry-After"]


async def test_health_ready_map_and_methods(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}
    page = await client.get("/api/v1/route/map?start=a&finish=b")
    assert page.status_code == 200 and b"leaflet" in page.content.lower()
    assert b"/api/v1/route" in page.content
    assert (await client.get("/api/v1/route")).status_code == 405


async def test_hundred_concurrent_identical_api_requests_make_one_routing_call(client, mock):
    import asyncio

    body = {"start": "Westville, TX", "finish": "Eastville, SC"}
    results = await asyncio.gather(*(post(client, body) for _ in range(100)))
    assert all(r.status_code == 200 for r in results)
    assert mock.ors.call_count == 1
    assert sum(r.json()["meta"]["routing"]["routing_calls"] for r in results) == 1


async def test_mpg_and_range_variants_reuse_the_cached_route(client, mock):
    """The route cache key is independent of mpg and max range: one provider call in total."""
    base = {"start": "Westville, TX", "finish": "Eastville, SC"}
    first = (await post(client, base)).json()
    assert first["meta"]["assumptions"]["max_range_miles"] == 500
    for extra in ({"mpg": 8}, {"mpg": 25}, {"max_range_miles": 500}, {"max_range_miles": 350}):
        r = await post(client, base | extra)
        assert r.status_code == 200
        j = r.json()
        assert j["meta"]["routing"]["routing_calls"] == 0
        check_plan(j, max_range=extra.get("max_range_miles", 500))
    assert mock.ors.call_count == 1
    assert (await post(client, base | {"mpg": 25})).json()["fuel"]["gallons_consumed"] < first[
        "fuel"
    ]["gallons_consumed"]


async def test_location_metadata_and_precision_are_exposed(client):
    j = (await post(client, {"start": "Westville, TX", "finish": coords(33.0, -80.0)})).json()
    loc = j["meta"]["locations"]
    assert set(loc) == {"start", "finish"}
    assert loc["start"]["lat"] and loc["finish"]["lat"] == 33.0
    assert j["meta"]["assumptions"]["station_location_precision"] == "city_centroid"

"""Smoke + golden test on the committed station artifact. The route is a SYNTHETIC fixture."""

import json

from tests.api.conftest import FIXTURES
from tests.api.test_route_api import check_plan, post


async def test_dallas_to_new_york_on_real_artifact(real_client):
    r = await post(real_client, {"start": "Dallas, TX", "finish": "New York, NY"})
    assert r.status_code == 200
    j = r.json()
    check_plan(j)
    assert j["meta"]["routing"]["provider"] == "osrm"
    assert j["meta"]["routing"]["routing_calls"] == 1
    assert j["route"]["distance_miles"] == 1550.0 and not j["meta"]["routing"]["us_only_reroute"]
    golden = json.loads((FIXTURES / "golden_dallas_ny.json").read_text())
    got = {
        "stops": [
            (s["station_id"], s["mile_marker"], s["gallons_purchased"], s["cost_usd"])
            for s in j["fuel_stops"]
        ],
        "fuel": {
            k: j["fuel"][k]
            for k in (
                "gallons_consumed",
                "gallons_purchased",
                "cash_spent_at_stops_usd",
                "estimated_total_fuel_cost_usd",
            )
        },
    }
    assert json.loads(json.dumps(got)) == golden


async def test_real_artifact_coordinates_and_warm_request(real_client):
    body = {"start": {"lat": 32.7767, "lng": -96.797}, "finish": {"lat": 40.7128, "lng": -74.006}}
    cold, warm = await post(real_client, body), await post(real_client, body)
    assert cold.status_code == warm.status_code == 200
    assert warm.json()["meta"]["routing"]["routing_calls"] == 0
    assert warm.json()["meta"]["cache"]["layer"] == "l1"

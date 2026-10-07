"""Runs only with REDIS_URL set (e.g. a local redis-server); otherwise skipped."""

import asyncio
import os

import pytest
import redis.asyncio as aioredis
import respx

from planner.corridor import build_bundle
from routing.budget import CallBudget
from routing.cache import FALLBACK_TTL, ROUTE_TTL, RouteCache, route_key
from routing.providers import build_router
from tests.api.conftest import ORS, OSRM, ROUTE, STATIONS, ors_json, osrm_json
from tests.helpers import make_store

URL = os.environ.get("REDIS_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="REDIS_URL not set")
START, FINISH = (33.0, -100.0), (33.0, -80.0)
STORE = make_store(STATIONS)


def worker(client):
    router = build_router("auto", ORS, "k", OSRM)
    return RouteCache(router, lambda r: build_bundle(r, STORE, None), lambda: client, lock_wait=3)


@pytest.fixture
async def client():
    c = aioredis.from_url(URL)
    await c.flushdb()
    yield c
    await c.aclose()


async def test_two_workers_share_one_upstream_call_and_ttls(client):
    with respx.mock(assert_all_called=False) as m:
        slow = m.post(f"{ORS}/v2/directions/driving-car")

        async def delayed(_req):
            await asyncio.sleep(0.3)
            import httpx

            return httpx.Response(200, json=ors_json(ROUTE))

        slow.mock(side_effect=delayed)
        a, b = worker(client), worker(client)  # separate L1 + singleflight = two processes
        budgets = [CallBudget(), CallBudget()]
        ra, rb = await asyncio.gather(
            a.get(START, FINISH, budgets[0]), b.get(START, FINISH, budgets[1])
        )
    assert slow.call_count == 1 and sum(x.routing_calls for x in budgets) == 1
    assert {ra.layer, rb.layer} == {None, "l2"}
    key = route_key(START, FINISH)
    assert 0 < await client.ttl(key) <= ROUTE_TTL and await client.ttl(key) > ROUTE_TTL - 60
    assert await client.exists(f"lock:{key}") == 0


async def test_fallback_route_gets_short_l2_ttl(client):
    with respx.mock(assert_all_called=False) as m:
        m.post(f"{ORS}/v2/directions/driving-car").respond(503)
        m.get(url__regex=rf"{OSRM}/route/v1/driving/.*").respond(json=osrm_json(ROUTE))
        await worker(client).get(START, FINISH, CallBudget())
    assert 0 < await client.ttl(route_key(START, FINISH)) <= FALLBACK_TTL

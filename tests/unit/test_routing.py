import asyncio

import httpx
import orjson
import pytest
import respx

from planner.corridor import build_bundle
from routing.budget import BudgetExceeded, CallBudget
from routing.cache import FALLBACK_TTL, ROUTE_TTL, RouteCache
from routing.providers import Breaker, NotRoutable, RoutingUnavailable, build_router
from tests.helpers import line_route, make_store

ORS, OSRM = "https://ors.test", "https://osrm.test"
START, FINISH = (35.0, -100.0), (35.0, -98.0)
ROUTE = line_route([START, FINISH])
STORE = make_store([(35.0, -99.0, 3.0)])


def ors_payload(provider="ors"):
    summary = {"distance": ROUTE.distance_miles * 1609.344, "duration": ROUTE.duration_s}
    return {"routes": [{"geometry": ROUTE.polyline, "summary": summary}]}


def osrm_payload():
    r = {
        "geometry": ROUTE.polyline,
        "distance": ROUTE.distance_miles * 1609.344,
        "duration": ROUTE.duration_s,
    }
    return {"code": "Ok", "routes": [r]}


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        m.ors = m.post(f"{ORS}/v2/directions/driving-car")
        m.osrm = m.get(url__regex=rf"{OSRM}/route/v1/driving/.*")
        m.ors.respond(json=ors_payload())
        m.osrm.respond(json=osrm_payload())
        yield m


def make_cache(redis=None, **kw):
    router = build_router("auto", ORS, "secret", OSRM)
    return RouteCache(router, lambda r: build_bundle(r, STORE, None), redis, **kw)


class FakeRedis:
    def __init__(self, *, locked=False, broken=False):
        self.data, self.locked, self.broken = {}, locked, broken

    async def get(self, key):
        if self.broken:
            raise ConnectionError
        return self.data.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if self.broken:
            raise ConnectionError
        if nx:
            return None if self.locked else self.data.setdefault(key, value) and True
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)


async def test_cold_request_is_one_call_and_warm_is_zero(mock):
    cache, cold, warm = make_cache(), CallBudget(), CallBudget()
    first = await cache.get(START, FINISH, cold)
    second = await cache.get(START, FINISH, warm)
    assert mock.ors.call_count == 1 and mock.osrm.call_count == 0
    assert (cold.routing_calls, cold.external_calls, warm.routing_calls) == (1, 1, 0)
    assert (first.source, second.source, second.layer) == ("miss", "hit", "l1")
    assert first.bundle.ttl == ROUTE_TTL and not first.bundle.fallback


async def test_provider_requests_use_lng_lat_and_miles(mock):
    lookup = await make_cache().get(START, FINISH, CallBudget())
    req = mock.ors.calls.last.request
    body = orjson.loads(req.content)
    assert body["coordinates"] == [[-100.0, 35.0], [-98.0, 35.0]]
    assert body["radiuses"] == [-1, -1] and req.headers["Authorization"] == "secret"
    assert lookup.bundle.distance_miles == pytest.approx(ROUTE.distance_miles)  # metres -> miles


async def test_osrm_request_shape(mock):
    router = build_router("auto", ORS, "", OSRM)  # no key: OSRM is the primary
    route, fallback = await router.route(START, FINISH, CallBudget())
    url = str(mock.osrm.calls.last.request.url)
    assert "/driving/-100.0,35.0;-98.0,35.0" in url and "overview=full" in url
    assert "geometries=polyline" in url and route.provider == "osrm" and not fallback
    assert mock.ors.call_count == 0


async def test_hundred_concurrent_identical_requests_make_one_call(mock):
    cache, budgets = make_cache(), [CallBudget() for _ in range(100)]
    results = await asyncio.gather(*(cache.get(START, FINISH, b) for b in budgets))
    assert mock.ors.call_count == 1
    assert sum(b.routing_calls for b in budgets) == 1
    assert sorted(r.source for r in results).count("coalesced") == 99


async def test_primary_failure_falls_back_with_short_ttl(mock):
    mock.ors.respond(503)
    budget = CallBudget()
    lookup = await make_cache().get(START, FINISH, budget)
    assert (mock.ors.call_count, mock.osrm.call_count, budget.routing_calls) == (1, 1, 2)
    assert lookup.bundle.fallback and lookup.bundle.ttl == FALLBACK_TTL
    assert lookup.bundle.provider == "osrm"


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout("t"), httpx.ConnectError("c")])
async def test_timeouts_and_connect_errors_fall_back(mock, failure):
    mock.ors.mock(side_effect=failure)
    lookup = await make_cache().get(START, FINISH, CallBudget())
    assert lookup.bundle.provider == "osrm"


async def test_not_routable_is_not_retried_or_cached(mock):
    mock.ors.respond(404, json={"error": {"code": 2010}})
    cache = make_cache()
    for _ in range(2):
        with pytest.raises(NotRoutable):
            await cache.get(START, FINISH, CallBudget())
    assert mock.ors.call_count == 2 and mock.osrm.call_count == 0


async def test_osrm_no_route_is_not_routable(mock):
    mock.osrm.respond(400, json={"code": "NoRoute"})
    with pytest.raises(NotRoutable):
        await build_router("osrm", ORS, "", OSRM).route(START, FINISH, CallBudget())


async def test_all_providers_down_is_unavailable_and_failure_is_not_cached(mock):
    mock.ors.respond(500)
    mock.osrm.respond(502)
    cache = make_cache()
    with pytest.raises(RoutingUnavailable):
        await cache.get(START, FINISH, CallBudget())
    mock.ors.respond(json=ors_payload())
    assert (await cache.get(START, FINISH, CallBudget())).source == "miss"


async def test_budget_cap_is_enforced(mock):
    mock.ors.respond(503)
    with pytest.raises(BudgetExceeded):
        await make_cache().get(START, FINISH, CallBudget(max_routing=1))
    assert mock.ors.call_count == 1 and mock.osrm.call_count == 0


async def test_leader_cancellation_does_not_cancel_the_shared_call(mock):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=ors_payload())

    mock.ors.mock(side_effect=slow)
    cache = make_cache()
    leader = asyncio.create_task(cache.get(START, FINISH, CallBudget()))
    await started.wait()
    follower = asyncio.create_task(cache.get(START, FINISH, CallBudget()))
    await asyncio.sleep(0)
    leader.cancel()
    release.set()
    assert (await follower).source == "coalesced"
    assert mock.ors.call_count == 1


async def test_l2_hit_rebuilds_bundle_without_a_call(mock):
    redis = FakeRedis()
    await make_cache(lambda: redis).get(START, FINISH, CallBudget())
    budget = CallBudget()
    lookup = await make_cache(lambda: redis).get(START, FINISH, budget)
    assert (lookup.source, lookup.layer, budget.routing_calls) == ("hit", "l2", 0)
    assert mock.ors.call_count == 1 and lookup.bundle.ttl > ROUTE_TTL - 60


async def test_held_redis_lock_fails_open_after_bounded_wait(mock):
    cache = make_cache(lambda: FakeRedis(locked=True), lock_wait=0.1)
    budget = CallBudget()
    lookup = await cache.get(START, FINISH, budget)
    assert lookup.source == "miss" and budget.routing_calls == 1 and mock.ors.call_count == 1


async def test_redis_failure_degrades_to_l1_only(mock):
    cache = make_cache(lambda: FakeRedis(broken=True))
    await cache.get(START, FINISH, CallBudget())
    assert (await cache.get(START, FINISH, CallBudget())).layer == "l1"
    assert mock.ors.call_count == 1


def test_circuit_breaker_opens_then_probes_after_cooldown():
    now = [0.0]
    breaker = Breaker(threshold=5, cooldown=30, clock=lambda: now[0])
    for _ in range(5):
        assert breaker.allow()
        breaker.failure()
    assert not breaker.allow()
    now[0] = 31
    assert breaker.allow() and not breaker.allow()  # one probe per cooldown
    breaker.success()
    assert breaker.allow()

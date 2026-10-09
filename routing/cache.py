"""Route lookup: L1 bundle cache -> L2 Redis -> coalesced, budgeted upstream call."""

import asyncio
import hashlib
import time
from dataclasses import dataclass, replace

import orjson
from cachetools import TLRUCache

from planner.corridor import Bundle
from routing.providers import Coordinate, Route, RouteLeavesUSA
from routing.singleflight import SingleFlight

ROUTE_TTL = 7 * 24 * 3600.0
FALLBACK_TTL = 15 * 60.0
REDIS_COOLDOWN = 10.0


def route_key(start: Coordinate, finish: Coordinate) -> str:
    """Rounded to 4 decimals (about 11 m). v2: only routes that stay in the US are stored."""
    ends = f"{start[0]:.4f},{start[1]:.4f}:{finish[0]:.4f},{finish[1]:.4f}"
    return hashlib.sha1(f"route:v2:driving-car:us-only:{ends}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Lookup:
    bundle: Bundle
    source: str  # "miss" | "hit" | "coalesced"
    layer: str | None  # "l1" | "l2" | None


class RouteCache:
    def __init__(self, router, build, redis_factory=None, *, l1_size=256, lock_wait=2.0):
        self.router, self.build, self.redis_factory, self.lock_wait = (
            router,
            build,
            redis_factory,
            lock_wait,
        )
        self.l1 = TLRUCache(l1_size, ttu=lambda _k, bundle, now: now + bundle.ttl)
        self.flight = SingleFlight()
        self._redis_down_until = 0.0

    async def get(self, start: Coordinate, finish: Coordinate, budget) -> Lookup:
        key = route_key(start, finish)
        if bundle := self.l1.get(key):
            return Lookup(bundle, "hit", "l1")
        lookup, leader = await self.flight.run(key, lambda: self._load(key, start, finish, budget))
        return lookup if leader else replace(lookup, source="coalesced")

    async def _load(self, key, start, finish, budget) -> Lookup:
        if bundle := self.l1.get(key):
            return Lookup(bundle, "hit", "l1")
        if bundle := await self._from_l2(key):
            return Lookup(bundle, "hit", "l2")
        lock = await self._lock(key)
        try:
            if lock == "held":  # another worker is fetching: wait for its result, then fail open
                deadline = time.monotonic() + self.lock_wait
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                    if bundle := await self._from_l2(key):
                        return Lookup(bundle, "hit", "l2")
            route, fallback = await self.router.route(start, finish, budget)
            bundle = await asyncio.to_thread(self.build, route)
            avoided = bundle.leaves_usa
            if avoided:  # the fuel dataset is US-only: reroute around the border or refuse
                route = await self.router.route_us_only(start, finish, budget)
                bundle = await asyncio.to_thread(self.build, route)
                if bundle.leaves_usa:
                    raise RouteLeavesUSA
                fallback = False
            bundle.fallback, bundle.border_avoided = fallback, avoided
            bundle.ttl = FALLBACK_TTL if fallback else ROUTE_TTL
            self.l1[key] = bundle
            await self._to_l2(key, route, bundle)
            return Lookup(bundle, "miss", None)
        finally:
            if lock == "acquired":
                await self._redis_call("delete", f"lock:{key}")

    def _redis(self):
        if self.redis_factory is None or time.monotonic() < self._redis_down_until:
            return None
        return self.redis_factory()

    async def _redis_call(self, method: str, *args, **kwargs):
        """Run a Redis command; any failure disables Redis briefly and returns None (fail open)."""
        client = self._redis()
        if client is None:
            return None
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception:
            self._redis_down_until = time.monotonic() + REDIS_COOLDOWN
            return None

    async def _lock(self, key: str) -> str:
        client = self._redis()
        if client is None:
            return "unavailable"
        try:
            return "acquired" if await client.set(f"lock:{key}", "1", nx=True, ex=15) else "held"
        except Exception:
            self._redis_down_until = time.monotonic() + REDIS_COOLDOWN
            return "unavailable"

    async def _from_l2(self, key: str) -> Bundle | None:
        raw = await self._redis_call("get", key)
        if not raw:
            return None
        try:
            data = orjson.loads(raw)
            remaining = data["expires"] - time.time()
            if remaining <= 0:
                return None
            bundle = await asyncio.to_thread(self.build, Route(**data["route"]))
            if bundle.leaves_usa:
                return None
            bundle.created, bundle.ttl = data["created"], remaining
            bundle.fallback, bundle.border_avoided = data["fallback"], data["border_avoided"]
        except (ValueError, KeyError, TypeError):
            return None
        self.l1[key] = bundle
        return bundle

    async def _to_l2(self, key: str, route: Route, bundle: Bundle) -> None:
        if self._redis() is None:
            return
        payload = {
            "route": {
                "polyline": route.polyline,
                "distance_miles": route.distance_miles,
                "duration_s": route.duration_s,
                "provider": route.provider,
            },
            "created": bundle.created,
            "expires": bundle.created + bundle.ttl,
            "fallback": bundle.fallback,
            "border_avoided": bundle.border_avoided,
        }
        await self._redis_call("set", key, orjson.dumps(payload), ex=int(bundle.ttl))

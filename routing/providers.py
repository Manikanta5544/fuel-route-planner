"""Routing providers (ORS, OSRM), shared client, circuit breaker and fallback policy.

Providers speak [lng, lat]; everything else uses (lat, lng). Metres become miles here, once.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from routing.budget import CallBudget
from routing.geometry import METRES_PER_MILE

log = logging.getLogger("routing")
USER_AGENT = "fuel-route-planner/1.0 (assessment project)"
Coordinate = tuple[float, float]  # (lat, lng)
_read_timeout_s = 5.0


class NotRoutable(Exception):
    """The provider cannot route between the points (no road near, no path)."""


class TransientError(Exception):
    """Timeout, connection error, 429 or 5xx: the other provider may still work."""


class RoutingUnavailable(Exception):
    """Every configured provider failed or is circuit-open."""


class RouteLeavesUSA(Exception):
    """The driving route enters Canada or Mexico and no border-free route could be obtained."""


@dataclass(frozen=True, slots=True)
class Route:
    polyline: str
    distance_miles: float
    duration_s: float
    provider: str


class Provider(Protocol):
    name: str
    supports_border_avoidance: bool

    async def route(
        self,
        start: Coordinate,
        finish: Coordinate,
        *,
        budget: CallBudget,
        avoid_borders: bool = False,
    ) -> Route: ...


_client: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def get_client() -> httpx.AsyncClient:
    """Pooled keep-alive client, created lazily for the running event loop."""
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(_read_timeout_s, connect=2.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            headers={"User-Agent": USER_AGENT},
        )
        _client = (loop, client)
    return _client[1]


async def _send(request_fn) -> httpx.Response:
    try:
        return await request_fn(get_client())
    except httpx.HTTPError as exc:  # timeouts and transport errors
        raise TransientError(type(exc).__name__) from exc


def _classify(resp: httpx.Response) -> None:
    if resp.status_code in (400, 404):
        raise NotRoutable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise TransientError(f"HTTP {resp.status_code}")


def _parse(provider: str, polyline, metres, seconds) -> Route:
    if not isinstance(polyline, str) or not polyline:
        raise TransientError("malformed response")
    return Route(polyline, float(metres) / METRES_PER_MILE, float(seconds), provider)


class ORSProvider:
    name = "ors"
    supports_border_avoidance = True

    def __init__(self, base_url: str, api_key: str):
        self.url = f"{base_url.rstrip('/')}/v2/directions/driving-car"
        self.headers = {"Authorization": api_key}

    async def route(
        self,
        start: Coordinate,
        finish: Coordinate,
        *,
        budget: CallBudget,
        avoid_borders: bool = False,
    ) -> Route:
        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "radiuses": [-1, -1],
            "instructions": False,
        }
        if avoid_borders:
            body["options"] = {"avoid_borders": "all"}
        budget.spend_routing()
        resp = await _send(lambda c: c.post(self.url, json=body, headers=self.headers))
        _classify(resp)
        try:
            top = resp.json()["routes"][0]
            return _parse(
                self.name, top["geometry"], top["summary"]["distance"], top["summary"]["duration"]
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TransientError("malformed response") from exc


class OSRMProvider:
    name = "osrm"
    supports_border_avoidance = False

    def __init__(self, base_url: str):
        self.base = f"{base_url.rstrip('/')}/route/v1/driving"

    async def route(
        self,
        start: Coordinate,
        finish: Coordinate,
        *,
        budget: CallBudget,
        avoid_borders: bool = False,
    ) -> Route:
        if avoid_borders:
            raise NotRoutable("OSRM cannot avoid borders")
        url = f"{self.base}/{start[1]},{start[0]};{finish[1]},{finish[0]}"
        params = {"overview": "full", "geometries": "polyline", "steps": "false"}
        budget.spend_routing()
        resp = await _send(lambda c: c.get(url, params=params))
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if payload.get("code") in ("NoRoute", "NoSegment"):
            raise NotRoutable(payload["code"])
        _classify(resp)
        try:
            top = payload["routes"][0]
            return _parse(self.name, top["geometry"], top["distance"], top["duration"])
        except (KeyError, IndexError, TypeError) as exc:
            raise TransientError("malformed response") from exc


class Breaker:
    """CLOSED -> OPEN after `threshold` consecutive failures -> one probe per cooldown."""

    def __init__(self, threshold: int = 5, cooldown: float = 30.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures, self.opened_at = 0, None

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if self.clock() - self.opened_at >= self.cooldown:
            self.opened_at = self.clock()  # half-open: let one probe through
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


class Router:
    """Primary first, then the fallback on transient failure only (no same-provider retry)."""

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.breakers = {p.name: Breaker() for p in providers}

    async def route(self, start: Coordinate, finish: Coordinate, budget: CallBudget):
        """Return (Route, fallback_used); raises NotRoutable, RoutingUnavailable, BudgetExceeded."""
        for position, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                log.warning("routing provider %s skipped: circuit open", provider.name)
                continue
            try:
                route = await provider.route(start, finish, budget=budget)
            except TransientError as exc:
                breaker.failure()
                log.warning("routing provider %s failed: %s", provider.name, exc)
                continue
            except NotRoutable:
                breaker.success()
                raise
            breaker.success()
            return route, position > 0
        raise RoutingUnavailable

    async def route_us_only(self, start: Coordinate, finish: Coordinate, budget: CallBudget):
        """Ask a border-avoiding provider for a route that stays in the US.

        Raises RouteLeavesUSA when no configured provider can promise that (OSRM cannot) or
        the provider finds none, and RoutingUnavailable when the attempt failed transiently.
        """
        for provider in self.providers:
            if not provider.supports_border_avoidance:
                continue
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                raise RoutingUnavailable
            try:
                route = await provider.route(start, finish, budget=budget, avoid_borders=True)
            except TransientError as exc:
                breaker.failure()
                log.warning(
                    "routing provider %s failed on border avoidance: %s", provider.name, exc
                )
                raise RoutingUnavailable from exc
            except NotRoutable as exc:
                breaker.success()
                raise RouteLeavesUSA from exc
            breaker.success()
            return route
        raise RouteLeavesUSA


def build_router(
    provider: str,
    ors_base_url: str,
    ors_api_key: str,
    osrm_base_url: str,
    read_timeout_s: float = 5.0,
) -> Router:
    """ORS first when a key exists (and is not disabled), OSRM otherwise or as the fallback."""
    global _read_timeout_s
    _read_timeout_s = read_timeout_s
    osrm = OSRMProvider(osrm_base_url)
    if provider != "osrm" and ors_api_key:
        return Router([ORSProvider(ors_base_url, ors_api_key), osrm])
    return Router([osrm])

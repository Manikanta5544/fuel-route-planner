"""Routing providers (ORS, OSRM), shared client, circuit breaker and fallback policy.

Providers speak [lng, lat]; everything else uses (lat, lng). Metres become miles here, once.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from routing.budget import CallBudget
from routing.geometry import METRES_PER_MILE

USER_AGENT = "fuel-route-planner/1.0 (assessment project)"
Coordinate = tuple[float, float]  # (lat, lng)


class NotRoutable(Exception):
    """The provider cannot route between the points (no road near, no path)."""


class TransientError(Exception):
    """Timeout, connection error, 429 or 5xx: the other provider may still work."""


class RoutingUnavailable(Exception):
    """Every configured provider failed or is circuit-open."""


@dataclass(frozen=True, slots=True)
class Route:
    polyline: str
    distance_miles: float
    duration_s: float
    provider: str


class Provider(Protocol):
    name: str

    async def route(
        self, start: Coordinate, finish: Coordinate, *, budget: CallBudget
    ) -> Route: ...


_client: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def get_client() -> httpx.AsyncClient:
    """Pooled keep-alive client, created lazily for the running event loop."""
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
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

    def __init__(self, base_url: str, api_key: str):
        self.url = f"{base_url.rstrip('/')}/v2/directions/driving-car"
        self.headers = {"Authorization": api_key}

    async def route(self, start: Coordinate, finish: Coordinate, *, budget: CallBudget) -> Route:
        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
            "radiuses": [-1, -1],
            "instructions": False,
        }
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

    def __init__(self, base_url: str):
        self.base = f"{base_url.rstrip('/')}/route/v1/driving"

    async def route(self, start: Coordinate, finish: Coordinate, *, budget: CallBudget) -> Route:
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
                continue
            try:
                route = await provider.route(start, finish, budget=budget)
            except TransientError:
                breaker.failure()
                continue
            except NotRoutable:
                breaker.success()
                raise
            breaker.success()
            return route, position > 0
        raise RoutingUnavailable


def build_router(provider: str, ors_base_url: str, ors_api_key: str, osrm_base_url: str) -> Router:
    """ORS first when a key exists (and is not disabled), OSRM otherwise or as the fallback."""
    osrm = OSRMProvider(osrm_base_url)
    if provider != "osrm" and ors_api_key:
        return Router([ORSProvider(ors_base_url, ors_api_key), osrm])
    return Router([osrm])

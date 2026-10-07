import asyncio
import json
from dataclasses import dataclass

from django.apps import AppConfig
from django.conf import settings

from planner.corridor import build_bundle
from routing.cache import RouteCache
from routing.providers import build_router
from routing.resolver import Places
from stations.store import StationStore, load_places, load_usa


@dataclass
class Runtime:
    store: StationStore | None
    places: Places | None
    usa: object | None
    cache: RouteCache
    redis_factory: object | None
    price_rule: str = "min"


def make_redis_factory(url: str):
    """One Redis client per running event loop, created lazily."""
    import redis.asyncio as aioredis

    state: dict = {}

    def factory():
        loop = asyncio.get_running_loop()
        if state.get("loop") is not loop:
            state["loop"] = loop
            state["client"] = aioredis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.5)
        return state["client"]

    return factory


def build_runtime(store, places, usa, price_rule: str = "min") -> Runtime:
    router = build_router(
        settings.ROUTING_PROVIDER,
        settings.ORS_BASE_URL,
        settings.ORS_API_KEY,
        settings.OSRM_BASE_URL,
    )
    redis_factory = make_redis_factory(settings.REDIS_URL) if settings.REDIS_URL else None
    cache = RouteCache(router, lambda route: build_bundle(route, store, usa), redis_factory)
    return Runtime(store, places, usa, cache, redis_factory, price_rule)


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Load artifacts once per process (at startup via ready(), or lazily on first use)."""
    global _runtime
    if _runtime is None:
        build_dir = settings.BUILD_DIR
        report = build_dir / "build_report.json"
        rule = json.loads(report.read_text()).get("price_rule", "min") if report.exists() else "min"
        _runtime = build_runtime(
            StationStore.load(build_dir), load_places(build_dir), load_usa(build_dir), rule
        )
    return _runtime


class ApiConfig(AppConfig):
    name = "api"

    def ready(self):
        get_runtime()  # tolerates missing artifacts so build_stations can run first

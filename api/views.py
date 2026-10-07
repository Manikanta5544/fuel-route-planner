import logging
import time
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode

import orjson
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.http import require_GET, require_POST

from api.apps import get_runtime
from api.errors import ApiError, api_view
from api.schemas import Point, RouteRequest
from planner.corridor import full_geometry_json, plan_route, trivial_bundle
from planner.cost import summarize
from routing.budget import CallBudget
from routing.cache import Lookup

log = logging.getLogger("api")
MAP_HTML = (Path(__file__).parent / "map.html").read_bytes()
SLOT = b'"@@GEOMETRY@@"'
PRICE_RULE_NAMES = {"min": "minimum", "median": "median", "mean": "mean"}
OUTSIDE = "LOCATION_OUTSIDE_SUPPORTED_AREA"


def _json(payload: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(orjson.dumps(payload), status=status, content_type="application/json")


def _r(value, digits):
    return None if value is None else round(value, digits)


def _resolve(rt, location, label: str) -> tuple[float, float]:
    if isinstance(location, Point):
        point = (location.lat, location.lng)
    else:
        point = rt.places.resolve(location)
        if point is None:
            raise ApiError(
                422,
                OUTSIDE,
                f"Could not resolve {label} {location!r}; "
                "use 'City, ST' in the contiguous USA, or lat/lng coordinates",
            )
    if not rt.usa.contains(*point):
        raise ApiError(422, OUTSIDE, f"The {label} location is outside the contiguous USA")
    return point


def _label(location) -> str:
    return f"{location.lat},{location.lng}" if isinstance(location, Point) else location


@require_POST
@api_view
async def route(request):
    started = perf_counter()
    rt = get_runtime()
    if rt.store is None or rt.places is None or rt.usa is None:
        raise RuntimeError("station artifacts missing; run `make etl`")
    detail = request.GET.get("geometry_detail", "simplified")
    if detail not in ("simplified", "full"):
        raise ApiError(400, "INVALID_REQUEST", "geometry_detail must be 'simplified' or 'full'")
    try:
        data = orjson.loads(request.body)
    except orjson.JSONDecodeError as exc:
        raise ApiError(400, "INVALID_REQUEST", "Body must be valid JSON") from exc
    req = RouteRequest.model_validate(data)
    start, finish = _resolve(rt, req.start, "start"), _resolve(rt, req.finish, "finish")

    budget = CallBudget(settings.MAX_ROUTING_CALLS)
    t0 = perf_counter()
    if (round(start[0], 4), round(start[1], 4)) == (round(finish[0], 4), round(finish[1], 4)):
        lookup = Lookup(trivial_bundle(*start), "bypass", None)
    else:
        lookup = await rt.cache.get(start, finish, budget)
    routing_ms = (perf_counter() - t0) * 1e3
    bundle, store = lookup.bundle, rt.store

    plan = plan_route(
        bundle,
        store,
        mpg=req.mpg,
        max_range=max(0.0, req.max_range_miles - settings.RESERVE_MILES),
        free_offset=settings.FREE_OFFSET_MILES,
        g_ref=settings.G_REF_GALLONS,
    )
    t1 = perf_counter()
    stops, purchases = [], []
    for order, s in enumerate(plan.stops, 1):
        i, price = s.idx, float(store.price[s.idx])
        purchases.append((s.gallons, price))
        stops.append(
            {
                "order": order,
                "station_id": str(store.id[i]),
                "name": str(store.name[i]),
                "address": str(store.address[i]),
                "city": str(store.city[i]),
                "state": str(store.state[i]),
                "lat": round(float(store.lat[i]), 5),
                "lng": round(float(store.lon[i]), 5),
                "location_precision": "city_centroid",
                "mile_marker": round(s.mile, 1),
                "off_route_miles": round(s.off, 1),
                "price_per_gallon": round(price, 3),
                "gallons_purchased": round(s.gallons, 2),
                "cost_usd": round(s.gallons * price, 2),
            }
        )
    if plan.stops:
        reference = (purchases[0][1], None)
    elif plan.cheapest_idx is not None:
        c = plan.cheapest_idx
        reference = (
            float(store.price[c]),
            {
                "station_id": str(store.id[c]),
                "name": str(store.name[c]),
                "city": str(store.city[c]),
                "state": str(store.state[c]),
            },
        )
    else:
        reference = None
    fuel = summarize(
        purchases, bundle.distance_miles, req.mpg, reference, settings.START_TANK_BILLING
    )
    initial = fuel["initial_fuel"]
    fuel["initial_fuel"] = {
        **initial,
        "gallons": round(initial["gallons"], 2),
        "reference_price_per_gallon": _r(initial["reference_price_per_gallon"], 3),
    }
    for key, digits in (
        ("gallons_consumed", 2),
        ("gallons_purchased", 2),
        ("cash_spent_at_stops_usd", 2),
        ("average_purchase_price_per_gallon", 3),
        ("estimated_total_fuel_cost_usd", 2),
    ):
        fuel[key] = _r(fuel[key], digits)

    query = {"start": _label(req.start), "finish": _label(req.finish)}
    query |= {k: getattr(req, k) for k in ("mpg", "max_range_miles") if k in req.model_fields_set}
    geometry = full_geometry_json(bundle) if detail == "full" else bundle.geometry_json
    planning_ms = plan.corridor_ms + plan.optimize_ms + (perf_counter() - t1) * 1e3
    payload = {
        "route": {
            "distance_miles": round(bundle.distance_miles, 1),
            "duration_seconds": round(bundle.duration_s),
            "geometry": {"type": "LineString", "coordinates": "@@GEOMETRY@@"},
            "geometry_detail": detail,
            "bbox": bundle.bbox,
        },
        "fuel_stops": stops,
        "fuel": fuel,
        "map_url": f"/api/v1/route/map?{urlencode(query)}",
        "meta": {
            "request_id": request.request_id,
            "routing": {
                "provider": bundle.provider,
                "routing_calls": budget.routing_calls,
                "geocode_calls": budget.geocode_calls,
                "external_calls": budget.external_calls,
                "fallback_used": bundle.fallback,
                "route_leaves_usa": bundle.leaves_usa,
            },
            "cache": {
                "route": lookup.source,
                "layer": lookup.layer,
                "age_seconds": round(max(0.0, time.time() - bundle.created), 1),
            },
            "planning": {
                "candidate_stations": plan.candidates,
                "selected_stops": len(stops),
                "corridor_miles_used": plan.corridor_miles,
                "corridor_widened": plan.corridor_miles > 15,
                "algorithm": "next_cheaper_greedy",
                "model": "fixed_route_detour_adjusted_ranking_price",
            },
            "performance": {
                "routing_ms": round(routing_ms, 1),
                "planning_ms": round(planning_ms, 1),
                "total_ms": round((perf_counter() - started) * 1e3, 1),
            },
            "assumptions": {
                "mpg": req.mpg,
                "max_range_miles": req.max_range_miles,
                "reserve_miles": settings.RESERVE_MILES,
                "initial_fuel_full": True,
                "start_tank_billing": settings.START_TANK_BILLING,
                "station_price_rule": PRICE_RULE_NAMES.get(rt.price_rule, rt.price_rule),
                "geo_precision": "city_centroid",
            },
        },
    }
    t2 = perf_counter()
    body = orjson.dumps(payload).replace(SLOT, geometry, 1)
    request.timings |= {
        "route": routing_ms,
        "corridor": plan.corridor_ms,
        "optimize": plan.optimize_ms,
        "serialize": (perf_counter() - t2) * 1e3,
    }
    log.info(
        "route_completed %s",
        orjson.dumps(
            {
                "request_id": request.request_id,
                "cache": lookup.source,
                "provider": bundle.provider,
                "routing_calls": budget.routing_calls,
                "candidates": plan.candidates,
                "selected": len(stops),
                "routing_ms": round(routing_ms, 1),
                "total_ms": payload["meta"]["performance"]["total_ms"],
            }
        ).decode(),
    )
    return HttpResponse(body, content_type="application/json")


@require_GET
async def route_map(request):
    return HttpResponse(MAP_HTML, content_type="text/html; charset=utf-8")


@require_GET
async def healthz(request):
    return _json({"status": "ok"})


@require_GET
async def readyz(request):
    rt = get_runtime()
    ready = rt.store is not None and rt.store.tree is not None and rt.places and rt.usa
    return _json({"status": "ready" if ready else "not_ready"}, 200 if ready else 503)

# Fuel Route Planner

Async Django 6.1 API: given a start and finish in the contiguous USA it returns the route, cost-optimal fuel
stops (500-mile range, 10 MPG) and the fuel spend. Prices come from `data/raw/fuel-prices-for-be-assessment.csv`.
Design: [ARCHITECTURE.md](ARCHITECTURE.md) (frozen). No database, DRF or Celery; Redis is optional.

## Setup and run
```
make setup etl test     # venv + deps, build station artifacts, ruff + pytest
make run                # uvicorn, 2 workers, http://localhost:8000   (cp .env.example .env to configure)
```
Routing uses OpenRouteService when `ORS_API_KEY` is set, otherwise the public OSRM demo automatically
(and as the fallback when ORS fails). `docker compose up` starts the app plus Redis (not built here, see Unverified).
`make bench` runs `scripts/bench.py` (stub provider). `REDIS_URL=redis://... pytest` also runs the live-Redis tests.

## API
`POST /api/v1/route` with `{"start": "Dallas, TX", "finish": {"lat": 40.7128, "lng": -74.006}, "mpg": 10, "max_range_miles": 500}`
(`start`/`finish` accept `City, ST` or coordinates; `?geometry_detail=full` for the unsimplified line).
`GET /api/v1/route/map?start=...&finish=...` Leaflet page, `GET /healthz`, `GET /readyz`. Postman: `docs/postman_collection.json`.
Errors: `{"error": {"code", "message", "request_id"}}` with 400 `INVALID_REQUEST`, 422 `LOCATION_OUTSIDE_SUPPORTED_AREA` /
`LOCATION_NOT_ROUTABLE` / `NO_FEASIBLE_FUEL_PLAN`, 429 `RATE_LIMITED`, 503 `ROUTING_UNAVAILABLE`, 500 `INTERNAL_ERROR`.

Dallas, TX to New York, NY (abridged; **route is a synthetic fixture**, stations are the real artifact):
```
{"route": {"distance_miles": 1550.0, "geometry": {"type": "LineString", "coordinates": [[-96.79,33.0], ...]}, ...},
 "fuel_stops": [{"order": 1, "station_id": "68256", "city": "Hooks", "state": "TX", "mile_marker": 165.2,
                 "off_route_miles": ..., "price_per_gallon": 2.817, "gallons_purchased": 16.52, "cost_usd": 46.54}, ...5 stops],
 "fuel": {"gallons_consumed": 155.0, "gallons_purchased": 105.0, "cash_spent_at_stops_usd": 301.73,
          "initial_fuel": {"gallons": 50.0, "valuation": "first_purchase_price", "reference_price_per_gallon": 2.817, ...},
          "estimated_total_fuel_cost_usd": 442.6},
 "map_url": "/api/v1/route/map?start=Dallas%2C+TX&finish=New+York%2C+NY",
 "meta": {"routing": {"provider": "osrm", "routing_calls": 1, "geocode_calls": 0, "external_calls": 1, ...},
          "cache": {"route": "miss", "layer": null}, "planning": {...}, "performance": {...}, "assumptions": {...}}}
```

## Assumptions
- Cash spent at stops is fact. `estimated_total_fuel_cost_usd` is an estimate: cash + the start tank valued at the first
  purchase price (`START_TANK_BILLING=reference`; `excluded` returns `null`). The tank starts full; `RESERVE_MILES` shrinks usable range.
- Station coordinates are **city centroids** (the CSV has none), so mile markers and off-route distances are approximate.
- Stations are ranked by a detour-adjusted price proxy on the fixed route; this is not global physical-cost optimality.
  The greedy buys only what reaches the next cheaper station, so small top-ups (e.g. 0.4 gal) can appear.
- Station price per OPIS id is the minimum over conflicting rows (`STATION_PRICE_RULE`).

## Design summary
Pure-Python planner (`planner/`, `routing/geometry.py`, `stations/`) behind thin async Django views. A route is fetched once
(1 provider call), projected to EPSG:5070, simplified, and turned into a cached bundle (display geometry + candidate stations with
mile marker and offset). L1 holds bundles in-process, L2 Redis holds the raw route (primary 7 days, fallback 15 min); failures are
never cached. Identical in-flight requests coalesce via a shielded detached task plus a Redis lock that fails open. A per-request
`CallBudget` caps provider calls at 3. The optimizer is a next-cheaper greedy with the destination as a price-0 node.

## Verification (this environment)
- ETL on the supplied CSV: 8,151 rows, 620 Canadian dropped, 26 identical duplicates, 6,626 unique US stops, 487 conflicting-price stops
  (all match the architecture). Gazetteer join: 6,614 exact + 1 fuzzy = 6,615 stations written (99.83%), 11 listed with a reason in `data/build/unmatched.csv`.
- `pytest`: 74 passed + 2 live-Redis tests (76 passed with a local redis-server). Includes 3,000 Hypothesis examples
  (greedy vs DP oracle, plain and effective prices, infeasible agreement), 100 concurrent identical requests = 1 provider call
  (unit and API level), and two real separate-worker caches sharing 1 call through Redis. `ruff check` is clean.
- Server smoke: `make run` (2 workers, Redis, stub OSRM): 200 on `/healthz`, `/readyz`, cold and warm POST; response gzip 27.5 KB -> 9.5 KB.

## Measured performance
Environment: Linux x86_64, **1 CPU shared by benchmark client, stub provider and server**, Python 3.13.13, 1 uvicorn worker, no Redis.
Provider was a **STUB OSRM** returning the synthetic Dallas->NY route (1,550 mi), so no real network latency is included except the
stated delay. 300 requests per sequential/concurrency-20 scenario, 100 for the burst scenarios (`scripts/bench.py`).

| scenario | stub delay | p50 ms | p95 ms | p99 ms | req/s | routing calls/req | cache hit ratio | errors |
|---|---|---|---|---|---|---|---|---|
| warm (c=20) | 0 | 103.5 | 284.2 | 386.0 | 150.9 | 0.00 | 1.00 | 0 |
| cold (sequential) | 0 | 25.6 | 33.6 | 36.6 | 38.0 | 1.00 | 0.00 | 0 |
| cold (sequential) | 150 ms | 180.7 | 191.0 | 203.3 | 5.5 | 1.00 | 0.00 | 0 |
| 100 identical concurrent | 0 | 585.1 | 755.8 | 763.4 | 127.2 | 0.01 (1 upstream call) | 0.99 | 0 |
| 100 distinct concurrent | 0 | 2167.6 | 3831.1 | 3899.8 | 25.4 | 1.00 | 0.00 | 0 |

In-process (Server-Timing, same machine): warm request total about 1-1.5 ms (corridor 0.1, optimize 0.2); cold bundle build about 22 ms
(offloaded to a worker thread). The warm and burst figures are dominated by CPU contention on the single shared core and by
serializing/compressing a 27 KB body, so they are not a capacity claim for a dedicated multi-core server.

## Deviations
1. Gazetteer: the Census and GeoNames hosts were unreachable, so places come from the GitHub-hosted US-Cities-Database CSV (gap filler role);
   the Census loader exists and is used first when its download succeeds.
2. US polygon: Natural Earth 50 m admin-0 (GitHub) simplified to about 1 km instead of the Census nation boundary; the Florida Keys fall partly outside it.
3. ETL station validation uses a contiguous-US bounding box (not the polygon), so Keys stations are kept.
4. `build_stations` lives in `api/management/commands/` because `stations/` is not an installed Django app.
5. The optional geocoder and ORS `avoid_borders` re-route are not implemented: free-text addresses return 422, `geocode_calls` is always 0,
   and `route_leaves_usa` is reported without re-routing.
6. L1 uses `cachetools.TLRUCache` (per-entry TTL) because primary and fallback routes need different TTLs.
7. The golden Dallas->NY test pins a synthetic route (no network to record a real response).

## Unverified
Live ORS and OSRM calls (request/response shapes taken from their documentation only); Census gazetteer URL and file layout (loader tested on a
synthetic sample); Docker build and compose; the map page in a real browser (Leaflet and tiles load from unpkg/OpenStreetMap at view time);
accuracy against real road geometry (the benchmark route is synthetic).

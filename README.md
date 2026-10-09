# Fuel Route Planner

An async Django 6.1 API. Give it a start and a finish inside the USA; it returns the driving route (GeoJSON plus a Leaflet map page),
the cheapest fuel stops along it for a vehicle with a **500-mile maximum range at 10 MPG**, and the money spent on fuel. Fuel prices come
from the supplied `data/raw/fuel-prices-for-be-assessment.csv`. Routing uses a free provider (OpenRouteService or OSRM) and is called
**once on a cold request and zero times when the route is cached**. No database, DRF or Celery; Redis is optional.

Design: [ARCHITECTURE.md](ARCHITECTURE.md) · What changed in the final review: [FINAL_REVIEW.md](FINAL_REVIEW.md)

## Quick start
```
make setup test      # venv + dependencies (Python 3.12+), ruff + pytest. Data artifacts in data/build are committed.
make run             # http://localhost:8000   (WEB_CONCURRENCY=N for more workers)
docker compose up    # app + Redis (see "Docker": written, not built in the authoring environment)
```
```
curl -s localhost:8000/api/v1/route -H 'content-type: application/json' \
     -d '{"start": "Dallas, TX", "finish": "New York, NY"}'
# map:  http://localhost:8000/api/v1/route/map?start=Dallas,%20TX&finish=New%20York,%20NY
```
`make run` starts `scripts/serve.py`. Use it instead of `uvicorn --workers N`, whose shared listener skips TCP_NODELAY and stalls every
keep-alive response about 40 ms (measured 44 ms vs 2 ms).

## Configuration (all optional; copy `.env.example`)
| variable | default | meaning |
|---|---|---|
| `ORS_API_KEY` | empty | when set, OpenRouteService is the primary provider (free key: openrouteservice.org). Never commit it. |
| `ORS_BASE_URL` | `https://api.heigit.org/openrouteservice` | ORS endpoint (`/v2/directions/driving-car`) |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | keyless provider: the default without an ORS key, and the fallback when ORS fails |
| `ROUTING_PROVIDER` | `auto` | `auto` / `ors` / `osrm` |
| `ROUTING_TIMEOUT_S` | 5 | read timeout per provider call |
| `MAX_ROUTING_CALLS` | 3 | hard cap of provider calls per request |
| `REDIS_URL` | empty | shared L2 cache + cross-worker lock; without it each process caches in memory |
| `START_TANK_BILLING` | `reference` | `reference` or `excluded` (see fuel cost) |
| `RESERVE_MILES` | 0 | safety margin removed from usable range |
| `RATE_LIMIT_PER_MIN` | 0 (off) | per client IP |
| `ALLOWED_HOSTS`, `LOG_LEVEL`, `WEB_CONCURRENCY` | | service settings |

## API
`POST /api/v1/route`
```json
{"start": "Dallas, TX", "finish": {"lat": 40.7128, "lng": -74.006}, "mpg": 10, "max_range_miles": 500}
```
`mpg` (default 10) and `max_range_miles` (default 500, **maximum 500**, minimum 50) are optional. `?geometry_detail=full` returns the unsimplified line.
Other endpoints: `GET /api/v1/route/map`, `GET /healthz`, `GET /readyz`. Postman collection: `docs/postman_collection.json`.

Response (abridged): `route` (distance, duration, GeoJSON `LineString`), `fuel_stops[]` (order, station id/name/address/city/state, `lat`/`lng`,
`mile_marker`, `off_route_miles`, `price_per_gallon`, `gallons_purchased`, `cost_usd`), `fuel` (below), `map_url`, and `meta`
(provider, `routing_calls`, cache hit/miss and layer, planning details, timings, assumptions, resolved `locations`, `warnings`).

Errors are `{"error": {"code", "message", "request_id"}}`: 400 `INVALID_REQUEST`; 422 `LOCATION_OUTSIDE_SUPPORTED_AREA`,
`LOCATION_NOT_ROUTABLE`, `ROUTE_LEAVES_SUPPORTED_AREA`, `NO_FEASIBLE_FUEL_PLAN` (the message names the uncovered stretch); 429 `RATE_LIMITED`;
503 `ROUTING_UNAVAILABLE`; 500 `INTERNAL_ERROR`.

### Locations and their precision
Inputs are `{lat, lng}` (exact) or text: `City, ST`, `City, Texas`, `City ST`, `Washington, D.C.`; accents are ignored and a street address ending
in `..., City, ST` resolves to that city. There is **no per-request geocoder**: text resolves to the *city centre* from an offline table of
29,738 places (`data/build/places.json`). `meta.locations.start/finish` states what was used (`input`, `lat`, `lng`, `precision`, `matched`), and `meta.warnings`
says when a street address was reduced to its city. For an exact start or finish, send coordinates. The table comes from a public
US-cities dataset, not the Census gazetteer (unreachable when built); against 48 well-known city centres its median error is 1.4 mi,
p90 12.5 mi, max 15.2 mi (largest for Dallas, Jacksonville, Columbus, Austin, Houston).

### Fuel cost semantics
- `fuel.cash_spent_at_stops_usd`: money actually spent at the chosen stops (fact).
- `fuel.estimated_total_fuel_cost_usd`: an estimate of the whole trip's fuel value: cash plus the full starting tank (50 gal) valued at the
  reference price in `fuel.initial_fuel` (`valuation` says how: `first_purchase_price`, `cheapest_corridor_station`, ...).
  `START_TANK_BILLING=excluded` returns `null` instead. A trip under 500 miles has no stops: cash is 0 and the estimate is the fuel burned at the reference price.
- 10 MPG, 50-gallon tank (500 miles), tank full at the start, no reserve unless configured.

### Optimality, stated precisely
The plan is **cost-optimal among candidate stations within `corridor_miles_used` of the provider's route (8 mi, widened 15/30/50 only if infeasible),
using a detour-adjusted ranking price**: `price + detour penalty` (`FREE_OFFSET_MILES=5`, `G_REF_GALLONS=25`). The detour model is a heuristic that
turns "off-route distance" into dollars; the greedy ("buy just enough to reach the next cheaper station") is proved against an exact
DP oracle on 3,000 random instances. It is not a global optimum over every possible road. Small top-ups (under 2 gal) occur in about one in five
stops (6 of 32 in the region sweep) because buying less is cheaper when the next stop is cheaper; the objective was deliberately left unchanged.

### Station coordinates
The CSV has no coordinates. Each station is placed at its **city centroid**, so mile markers and `off_route_miles` are approximate and several
stations of one city share a point. `meta.assumptions.station_location_precision` says `city_centroid`. Only 5 of 6,626 CSV addresses are
street-level, so address geocoding was judged not worth a geocoder dependency. 6,615 of 6,626 stations are matched (99.83%); 11 are listed in
`data/build/unmatched.csv`.

### Routing calls, caching, borders
- Cold: 1 provider call. Warm: 0. Same route with a different `mpg` or `max_range_miles`: 0 (the route cache is independent of both).
  100 concurrent identical requests: 1 call (in-process single-flight, plus a Redis lock across workers).
- L1 in-process cache of the derived bundle (7 days; 15 min for fallback routes); L2 Redis stores the raw route. Failures are never cached.
- Provider policy: ORS (when keyed) → OSRM on timeout/429/5xx; circuit breaker after 5 failures for 30 s; per-request budget of 3 calls.
- **US-only rule.** A route that spends 2+ unbroken miles inside Canada or Mexico is never planned. If the provider supports it (ORS) the app
  re-routes once with `avoid_borders: all`; otherwise, or if that route still crosses, it answers 422 `ROUTE_LEAVES_SUPPORTED_AREA`.
  `meta.routing.us_only_reroute` shows when the reroute happened.

## Verification status
Verified here: `ruff` clean; **106 tests pass** with a live Redis (104 + 2 skipped without `REDIS_URL`), including Hypothesis greedy-vs-DP (3,000 examples),
seven cross-country trips through the real station artifact with provider-shaped mock routes, the border reroute/refuse flow, 100-way
coalescing, two real separate-worker caches sharing one call via Redis, 2-worker `serve.py` + Redis smoke, and the map page in headless Chromium
(Leaflet served from the npm package under the real SRI hashes; tile failure and total CDN failure both degrade to a message + stops table).
**Not verified (network blocked here):** live ORS and OSRM calls (request/response shapes follow their docs), real road geometry (test routes are
synthetic polylines with the real distances), Docker build and `compose up`, real OSM tiles. See FINAL_REVIEW.md.

## Performance (stub provider; 2 shared CPUs; client, stub and server on one machine)
| scenario | p50 ms | p95 ms | routing calls/req | server planning ms | note |
|---|---|---|---|---|---|
| cold, sequential | 24.9 | 32.1 | 1.00 | 0.3 | includes ~16 ms bundle build |
| warm, concurrency 20 | 66.3 | 129.0 | 0 | 0.2 | server total 0.4 ms; rest is client/CPU contention |
| same route, 100 different MPG | 69.1 | 92.0 | 0 | 0.2 | |
| same route, 100 different range | 63.3 | 138.6 | 0 | 0.2 | |
| 100 identical concurrent | 346.7 | 418.4 | 0.01 (1 upstream call) | 0.2 | |
| 100 distinct concurrent | 1352 | 1957 | 1.00 | 0.3 | CPU-bound bundle builds on 2 cores |

Cold bundle build: ~16 ms for a 2.9k-point polyline, ~95 ms for a 46k-point one (dense real routes); it runs in a worker thread. Real provider
latency is not included. These are not capacity claims for dedicated hardware. Reproduce: `make bench`.

## Docker
`docker compose up` builds the image (deps read from `pyproject.toml`, non-root user, `HEALTHCHECK` on `/healthz`) and starts Redis with a
healthcheck. Config comes from `.env` (optional). *Written and statically reviewed only: no Docker daemon was available to build it.*

## Known limitations
- **California coverage:** the CSV contains 8 California stations (Imperial Valley). Trips whose first 500 miles have no station, e.g. San Francisco →
  Las Vegas, get a clear 422 naming the gap rather than an invented plan. Other states are well covered (see the region tests).
- Station positions are city centroids and place lookup is centroid-accurate only (above). Use coordinates for precise endpoints.
- Fuel prices are a snapshot from the CSV; the estimate ignores traffic, elevation and partial-tank starts.
- The map page needs internet for Leaflet (unpkg) and OSM tiles; without them it shows the summary and stops table.
- The border rule is polygon-based (Natural Earth 10 m): routes hugging the border within ~1 mi are not flagged.
- The public OSRM demo is rate-limited and not for production; set `ORS_API_KEY` for real use.

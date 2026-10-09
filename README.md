# Fuel Route Planner

An async Django 6.1 API. Give it a start and a finish inside the USA; it returns the driving route (GeoJSON plus a Leaflet map page),
the cheapest fuel stops along it for a vehicle with a **500-mile maximum range at 10 MPG**, and the money spent on fuel. Fuel prices come
from the supplied `data/raw/fuel-prices-for-be-assessment.csv`. Routing uses a free provider (OpenRouteService or OSRM). A cold request normally
makes **one** routing call and a cache hit makes **zero**; a provider failure (fallback) or a route that crosses the border (re-route) can add
calls, with a hard cap of **three per request**. No database, DRF or Celery; Redis is optional.

Design: [ARCHITECTURE.md](ARCHITECTURE.md) · Review history and verification log: [FINAL_REVIEW.md](FINAL_REVIEW.md)

## Quick start

**macOS / Linux** (Python 3.12+; the data artifacts in `data/build` are committed)
```
make setup test      # venv + dependencies, ruff + pytest
make run             # http://localhost:8000   (WEB_CONCURRENCY=N for more workers)
```

**Windows (PowerShell)**
```
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python scripts/serve.py             # http://localhost:8000
```
On Windows `scripts/serve.py` runs a single worker (multi-worker mode forks processes, which Windows lacks); use Docker for several workers.
To rebuild the station artifacts from the CSV (not needed to run): `python manage.py build_stations --download`.

**Docker** (app + Redis, 2 workers)
```
docker compose up --build
```

**Try it**
```
curl -s localhost:8000/api/v1/route -H 'content-type: application/json' \
     -d '{"start": "Dallas, TX", "finish": "New York, NY"}'
# map:  http://localhost:8000/api/v1/route/map?start=Dallas,%20TX&finish=New%20York,%20NY
```
```
# PowerShell
$body = @{ start = "Dallas, TX"; finish = "New York, NY" } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8000/api/v1/route -Method Post -ContentType "application/json" -Body $body
```
`make run` / `scripts/serve.py` is preferred over `uvicorn --workers N`, whose shared listener skips TCP_NODELAY and stalls every
keep-alive response about 40 ms (measured 44 ms vs 2 ms).

## Configuration (all optional)
Settings are **environment variables**. A local run (`make run`, `python scripts/serve.py`) reads the real environment only, e.g. in PowerShell
`$env:ORS_API_KEY = "<your key>"` before starting the server. A `.env` file (copy `.env.example`) is read by **Docker Compose** only. Never commit a key.

| variable | default | meaning |
|---|---|---|
| `ORS_API_KEY` | empty | when set, OpenRouteService is the primary provider (free key: openrouteservice.org). |
| `ORS_BASE_URL` | `https://api.heigit.org/openrouteservice` | ORS endpoint (`/v2/directions/driving-car`). The old `api.openrouteservice.org` host is deprecated and scheduled to shut down in early November 2026. |
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
says when a street address was reduced to its city. For an exact start or finish, send coordinates.

**Accuracy of the place table.** It was built from a public US-cities dataset, because the Census gazetteer host was unreachable when the artifacts were
built. It is approximate: spot checks against well-known city centres show errors from under 1 mile to roughly 15 miles for several large cities
(Dallas, Jacksonville, Columbus, Houston, Austin), and at least one outlier of about 23 miles (North Las Vegas resolves toward Moapa). Earlier
summary statistics for this table have been withdrawn because the sample missed that outlier. The ETL can rebuild the table from the Census gazetteer
(`python manage.py build_stations --download`), which should tighten these positions; until that is done and re-measured, treat text locations and
station positions as city-level.

### Fuel cost semantics
- `fuel.cash_spent_at_stops_usd`: money actually spent at the chosen stops (fact).
- `fuel.estimated_total_fuel_cost_usd`: an estimate of the whole trip's fuel value: cash plus the full starting tank (50 gal) valued at the
  reference price in `fuel.initial_fuel` (`valuation` says how: `first_purchase_price`, `cheapest_corridor_station`, ...).
  `START_TANK_BILLING=excluded` returns `null` instead. A trip under 500 miles has no stops: cash is 0 and the estimate is the fuel burned at the reference price.
- 10 MPG, 50-gallon tank (500 miles), tank full at the start, no reserve unless configured.

### Optimality, stated precisely
The plan is **optimal for the modeled fixed-route planning problem**: cost-optimal among candidate stations within `corridor_miles_used` of the provider's
route (8 mi, widened 15/30/50 only if infeasible), using a detour-adjusted ranking price: `price + detour penalty` (`FREE_OFFSET_MILES=5`,
`G_REF_GALLONS=25`). The detour model is a heuristic that turns "off-route distance" into dollars; the greedy ("buy just enough to reach the next
cheaper station") is checked against an exact DP oracle on 3,000 random instances. It is not a global optimum over every possible road. Small top-ups
(under 2 gal) occur in about one in five stops (6 of 32 in the region sweep) because buying less is cheaper when the next stop is cheaper; the objective
was deliberately left unchanged, since an extra stop does not raise the total fuel cost the assignment asks for.

### Station coordinates
The CSV has no coordinates. Each station is placed at its **city centroid**, so mile markers and `off_route_miles` are approximate and several
stations of one city share a point. `meta.assumptions.station_location_precision` says `city_centroid`. Only 5 of 6,626 CSV addresses are
street-level, so address geocoding was judged not worth a geocoder dependency. 6,615 of 6,626 stations are matched (99.83%); 11 are listed in
`data/build/unmatched.csv`. The accuracy caveat above applies to these positions too.

### Routing calls, caching, borders
- **Calls per request:** cold normally 1; warm (cache hit) 0; a primary-provider failure adds one fallback call; a route that crosses the border adds one
  re-route call; the hard cap is 3 (`meta.routing.routing_calls` reports the actual number). The same route with a different `mpg` or
  `max_range_miles` costs 0 (the route cache is independent of both). 100 concurrent identical requests make 1 call (in-process single-flight, plus a
  Redis lock across workers).
- L1 in-process cache of the derived bundle (7 days; 15 min for fallback routes); L2 Redis stores the raw route. Failures are never cached.
- Provider policy: ORS (when keyed) → OSRM on timeout/429/5xx; circuit breaker after 5 failures for 30 s; per-request budget of 3 calls.
- **US-only rule.** A route that spends 2+ unbroken miles inside Canada or Mexico is never planned. If the provider supports it (ORS) the app
  re-routes once with `avoid_borders: all`; otherwise, or if that route still crosses, it answers 422 `ROUTE_LEAVES_SUPPORTED_AREA`.
  `meta.routing.us_only_reroute` shows when the reroute happened.

## Verification status
Verification happened in two stages; details and dates are in [FINAL_REVIEW.md](FINAL_REVIEW.md).

**Automated (original build environment, no internet to providers):** `ruff` clean; **106 tests pass** with a live Redis (104 + 2 skipped without
`REDIS_URL`), including Hypothesis greedy-vs-DP (3,000 examples), seven cross-country trips through the real station artifact with provider-shaped
mock routes (real city pairs and distances, synthetic polylines), the border reroute/refuse flow, 100-way coalescing, two separate-worker caches sharing
one call via Redis, a 2-worker `serve.py` + Redis smoke test, and the map page in headless Chromium (tile failure and total CDN failure both degrade to
a message + stops table).

**Manual, on a developer machine (October 2026), with live services:**
- Docker Compose (app with `WEB_CONCURRENCY=2` + Redis) started healthy; `/healthz`, `/readyz` and Redis `PONG` passed.
- A cached route survived an app restart and was served from Redis (`cache: hit`, `layer: l2`, `external_calls: 0`).
- Live routing worked through OSRM and ORS (ORS returned HTTP 200 on requests; see the caveat below). Real-route trips returned plausible plans, e.g. Dallas → New York
  (1,558 mi, 7 stops; 8 MPG / 350 mi range gave 11 stops), Los Angeles → Las Vegas (276 mi, 0 stops), Los Angeles → Salt Lake City (693 mi, 2 stops) and
  Los Angeles → Dallas (1,432 mi, 7 stops); every reported leg stayed within the range.
- The map page rendered routes, stops and totals in a desktop browser; Postman checks of valid and invalid inputs (MPG 0, missing finish, range 501) behaved as specified.
- The full suite (106 tests) passed with Redis configured.

**Not established:** ORS `avoid_borders` behaviour against the live service, sustained provider reliability or load, accuracy of every route in the
USA, and production deployment. **Caveat:** some ORS requests timed out during manual testing; the fallback to OSRM engaged, but a cold request that
hits the timeout takes about `ROUTING_TIMEOUT_S` (5 s) longer, plus the fallback call. Warm requests are unaffected.

## Performance (stub provider; 2 shared CPUs; client, stub and server on one machine)
| scenario | p50 ms | p95 ms | routing calls/req | server planning ms | note |
|---|---|---|---|---|---|
| cold, sequential | 24.9 | 32.1 | 1.00 | 0.3 | includes ~16 ms bundle build |
| warm, concurrency 20 | 66.3 | 129.0 | 0 | 0.2 | server total 0.4 ms; rest is client/CPU contention |
| same route, 100 different MPG | 69.1 | 92.0 | 0 | 0.2 | |
| same route, 100 different range | 63.3 | 138.6 | 0 | 0.2 | |
| 100 identical concurrent | 346.7 | 418.4 | 0.01 (1 upstream call) | 0.2 | |
| 100 distinct concurrent | 1352 | 1957 | 1.00 | 0.3 | CPU-bound bundle builds on 2 cores |

These are **stub-provider** measurements; real provider latency is not included. Cold bundle build: ~16 ms for a 2.9k-point polyline, ~95 ms for a
46k-point one (dense real routes); it runs in a worker thread. For scale, one cold Los Angeles → Las Vegas request through the public OSRM demo
spent about 2.6 s in the routing call (single observation, not a benchmark); with the cache warm the same request makes no provider call. These are not
capacity claims for dedicated hardware. Reproduce: `make bench` or `python scripts/bench.py`.

## Docker
`docker compose up --build` builds the image (deps read from `pyproject.toml`, non-root user, `HEALTHCHECK` on `/healthz`) and starts the app with two
workers and Redis (with its own healthcheck, published on `127.0.0.1:6379` only). Config comes from `.env` (optional). Built and run locally; see
Verification status.

## Known limitations
- **Coordinate precision:** station and place positions are city centroids from an offline table that is approximate (see "Accuracy of the place table"). Use coordinates for precise endpoints.
- **California coverage:** the CSV contains 8 California stations (Imperial Valley). Trips whose first 500 miles have no station, e.g. San Francisco →
  Las Vegas, get a clear 422 naming the gap rather than an invented plan. Other states are well covered (see the region tests).
- **Provider latency and reliability:** a cold request depends on the routing provider; ORS timeouts were seen intermittently, and the public OSRM demo is rate-limited and not for production.
- Fuel prices are a snapshot from the CSV; the estimate ignores traffic, elevation and partial-tank starts.
- The map page needs internet for Leaflet (unpkg) and OSM tiles; without them it shows the summary and stops table.
- The border rule is polygon-based (Natural Earth 10 m): routes hugging the border within ~1 mi are not flagged.
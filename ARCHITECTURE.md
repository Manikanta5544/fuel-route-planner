# Fuel Route Planner: Architecture and Engineering Decisions (FINAL)

> **Goal:** a fast, deterministic Django API. Input: two US locations. Output: the driving route on a map, cost-optimal (for the modeled fixed-route problem) fuel stops respecting a 500-mile range, and total fuel spend at 10 MPG.
>
> **Principle:** keep the request path local and deterministic. The routing provider is the only expensive external dependency. The normal cold request makes **one** routing call and a cache hit makes **zero**. Exceptional paths add calls: a primary-provider failure adds one fallback call and a route that crosses the border adds one re-route call, under a **hard cap of 3 per request** (`MAX_ROUTING_CALLS`, reported as `meta.routing.routing_calls`).
>
> **Status:** this document describes the final implementation. Verification results (automated, and manual with live services) are in section 16 and `FINAL_REVIEW.md`.


> **Amendments from the final review (these supersede any conflicting text below).** The original design is unchanged except:
> 1. **US-only routes.** The polygon/`route_leaves_usa` flag is replaced by foreign-territory mileage: Natural Earth 10 m polygons for the US
>    (with islands) and for a 1° band of Canada/Mexico are built by the ETL (`usa.geojson`, `foreign.geojson`). A route with an unbroken
>    stretch of 2+ miles inside the foreign band leaves the US. `Router.route_us_only` then re-routes once with ORS `options.avoid_borders="all"`
>    (OSRM cannot), re-checks, and otherwise raises `ROUTE_LEAVES_SUPPORTED_AREA` (422). The call budget (3) covers primary + reroute + fallback;
>    bundles are cached with `border_avoided`; a cached L2 route that crosses the border is ignored. Cache key version bumped to v2.
> 2. **ORS endpoint** is `https://api.heigit.org/openrouteservice`; the key stays optional and OSRM remains the keyless default and fallback.
> 3. **`max_range_miles` is capped at 500** (50-500). `mpg` is optional (default 10). Neither is in the route cache key.
> 4. **Cost labels.** `cash_spent_at_stops_usd` is fact; `estimated_total_fuel_cost_usd` values the start tank (`START_TANK_BILLING`), with the
>    valuation basis reported (`first_purchase_price`, `cheapest_corridor_station`, `no_reference_station`, `not_applicable`, or `excluded` -> null).
> 5. **Locations** return `meta.locations` (precision, matched place) and `meta.warnings`; `D.C.` and similar forms parse. Station precision is
>    reported as `city_centroid`. No geocoder was added: only 5 of 6,626 CSV addresses are street-level, so the payoff is nil.
> 6. **Optimality wording:** cost-optimal among candidate stations in the selected corridor under the detour-adjusted ranking model (a documented
>    heuristic), not a global optimum. The objective is unchanged.
> 7. **Infeasible** errors name the uncovered stretch (e.g. California in the supplied CSV).
> 8. **Settings** validate choices and ranges at start-up; the map page has SRI hashes and degrades gracefully when Leaflet or tiles fail;
>    the container runs as non-root with health checks.

---

## 1. Executive summary and requirement traceability

| Requirement | Decision |
|---|---|
| Latest stable Django | **Django 6.1.2** (pin `>=6.1.2,<6.2`), Python 3.13 (6.1 supports 3.12–3.14) |
| Start and finish in the USA | Coordinates or `City, ST` (offline); contiguous-US polygon check |
| Return a map of the route | Route geometry in JSON **plus** a Leaflet map page driven by the same JSON |
| Optimal, cost-effective stops | Fixed-route next-cheaper greedy, verified against an exact DP (optimal for the modeled problem, see section 7.3) |
| 500-mile range, multiple stops | Capacity 50 gal; any number of stops |
| Total spend at 10 MPG | Cash spent at stops (fact) plus a labelled estimate of total trip cost (section 8) |
| Use the supplied CSV | Offline ETL into an immutable in-memory artifact |
| Free map/routing API | OpenRouteService primary, OSRM fallback |
| Few routing calls | 1 on a normal cold request, 0 warm; fallback or border re-route can add calls; hard cap 3, reported in `meta` |
| Fast, concurrent | ASGI, pooled async client, two-tier cache, request coalescing, no DB on the hot path |

---

## 2. Request flow

```text
Client ── POST /api/v1/route ──► Django 6.1.2 / ASGI
   1. Validate + normalise (pydantic)
   2. Resolve endpoints:  coords → direct │ "City, ST" → offline gazetteer
   3. USA polygon check
   4. Route lookup:  L1 TTL cache → L2 Redis → upstream
                     (coalesced, circuit-broken, call-budgeted)
   5. Decode polyline → project (EPSG:5070) → LineString
   6. Corridor query (STRtree) → project candidates → along-route position + offset
   7. Optimiser (next-cheaper greedy, destination = price-0 node)
   8. Cost model → response (orjson, gzip)
Response: JSON + Server-Timing + X-Request-ID
```

The routing provider is never called per station. One route call gives the geometry; everything after it is local.

---

## 3. Stack

| Concern | Choice |
|---|---|
| Framework | Django 6.1.2, native async views (no DRF) |
| Validation | pydantic v2 |
| Server | `scripts/serve.py` (uvicorn, TCP listener bound with `IPPROTO_TCP`); `WEB_CONCURRENCY` default 1; plain `uvicorn --workers N` avoided (40 ms keep-alive stall) |
| HTTP client | `httpx.AsyncClient`, pooled keep-alive, connect 2 s / read `ROUTING_TIMEOUT_S` (default 5 s) |
| Routing | ORS `driving-car` primary; OSRM fallback behind one interface |
| Geometry | shapely 2.x (`STRtree`, nearest-segment projection), pyproj, numpy |
| JSON | orjson |
| Cache | cachetools `TLRUCache` (L1, per-entry TTL) + Redis (L2, optional) |
| Station storage | `stations.npz` loaded at boot; **no database** (`DATABASES = {}`) |
| Tests | pytest, pytest-asyncio, hypothesis, respx; `scripts/bench.py` for latency |
| Delivery | Dockerfile, compose (app + Redis), Makefile, `.env.example`, Postman collection |

---

## 4. Location input

Accepted forms:

```json
{"start": "Dallas, TX", "finish": "New York, NY"}
{"start": {"lat": 32.7767, "lng": -96.7970}, "finish": {"lat": 40.7128, "lng": -74.0060}}
```

- `City, ST` resolves offline from the same gazetteer used by the ETL, with aliases ("New York City", "St."/"Saint", "Mt."/"Mount"). Ambiguity is resolved by the state.
- **Free text, as implemented:** there is no external geocoder. Text is parsed offline: `City, ST`, `City, Texas`, `City ST`, accents ignored. A street address ending in `..., City, ST` resolves to that city's centre (city-centroid precision, reported in `meta.locations.*.precision` and as a note in `meta.warnings`); `123 Main St, Dallas, TX` therefore routes from Dallas's centre, not the exact address. An unresolvable name returns 422. A real geocoder (counted in `geocode_calls`, off by default) is a possible extension and is **not implemented**; `geocode_calls` is always 0.
- Outside the supported area, return `422 LOCATION_OUTSIDE_SUPPORTED_AREA`. Validation happens **before** any upstream call.

---

## 5. Data and station store

### 5.1 Profile (from the supplied CSV)
- 8,151 rows, 7 columns, no nulls.
- 620 Canadian rows (AB, BC, MB, NB, NS, ON, QC, SK, YT) are dropped.
- **6,626 unique US stops** by OPIS ID; 4,179 unique city/state pairs.
- 26 fully identical duplicate rows.
- 487 US stops with conflicting prices under one ID (median spread about $0.10, max $0.90) and no timestamp.
- No latitude/longitude. Addresses are mostly highway/exit text.

### 5.2 ETL (`manage.py build_stations`)
```text
CSV → validate → drop non-US → drop exact duplicates → collapse by OPIS ID
    → price rule (min | median | mean; default min)
    → offline city/state join (normalise, fuzzy within state)
    → coordinate validation → stations.npz + unmatched.csv
```
- Unmatched stops go to `unmatched.csv`, never silently dropped. Result on the supplied CSV: 6,615 of 6,626 stations matched (99.83%), 11 unmatched.
- Station positions are city centroids by data limitation; every response states this (`location_precision` per stop, `meta.assumptions.station_location_precision`).
- Place table: the committed artifacts were built from a public US-cities dataset because the Census host was unreachable at build time. The Census loader exists and is preferred when its file is present (`build_stations --download`); rebuilding with it is expected to tighten positions but has not yet been run and measured.
- One display name per ID (longest variant).

### 5.3 Runtime store
At boot, `stations.npz` loads into numpy arrays plus a shapely `STRtree` in EPSG:5070. No relational query on the hot path. PostGIS is deliberately avoided for roughly 6.6k points.

---

## 6. Routing layer

### 6.1 Provider interface
```python
async def route(start: Coordinate, finish: Coordinate, *, budget: CallBudget) -> Route
```
`ORSProvider` and `OSRMProvider` implement it. The planner never sees provider specifics. The base URL and key come from environment variables.

### 6.2 ORS specifics
- Coordinates are `[lng, lat]`; distances are converted from metres to miles once, at the provider boundary.
- **Base URL:** `https://api.heigit.org/openrouteservice` (default of `ORS_BASE_URL`; the account portal is `account.heigit.org`). HeiGIT deprecated the old `api.openrouteservice.org` host and announced its shutdown for 2-6 November 2026; existing keys work on the new host. Request: `POST /v2/directions/driving-car`, key in the `Authorization` header, `instructions: false`.
- **Live status:** the request shape was first built from the ORS documentation and later exercised against the live service by manual testing (HTTP 200 responses with valid routes). ORS requests also timed out intermittently; the fallback to OSRM handled those (section 6.3). Per-day/minute quotas are **unverified**; check the dashboard. Documented limits (6,000 km maximum route distance, 50 waypoints for driving profiles) are taken from the ORS docs and not re-verified; coast to coast fits in one call.
- **Snapping:** ORS defaults to a 350 m snapping radius around each input coordinate, and city centroids can fall farther from a road. The adapter sends `radiuses: [-1, -1]` (no limit) and maps "no routable point" to `422 LOCATION_NOT_ROUTABLE`.
- **Borders:** `options.avoid_borders` (`all` or `controlled`, driving profiles only) is used only for the single re-route when a route leaves the US (amendment 1); if the provider cannot supply a border-free route the API returns 422 `ROUTE_LEAVES_SUPPORTED_AREA`. A forum thread reports error 2099 with `all` in some cases. The re-route path is covered by mock-provider tests; its behaviour against the live service has **not** been exercised.

### 6.3 Resilience and call budget
| Situation | Calls |
|---|---|
| Cold request (normal) | 1 |
| Route-cache hit | 0 |
| Primary transient failure (timeout, 429, 502–504) | 1 primary + 1 fallback |
| Optional border re-route | +1 |
| **Hard cap** | `MAX_ROUTING_CALLS=3` (counter raises beyond it) |

Three counters are reported in `meta.routing`:

| Counter | Counts | Core `City, ST` request | Cap |
|---|---|---|---|
| `routing_calls` | directions requests, including fallback and border re-route | 1 | 3 |
| `geocode_calls` | reserved for a future geocoder; not implemented | always 0 | n/a |
| `external_calls` | sum of the above | 1 | derived |

With no geocoder implemented, `external_calls` equals `routing_calls` (at most 2 with a primary failure and fallback). The counters are kept so a future geocoder can be counted separately; the guarantee is stated on `routing_calls`.

- Only transient failures retry or fall back.
- Circuit breaker: `CLOSED → OPEN (after repeated failures) → HALF_OPEN → CLOSED/OPEN`, lightweight and in-process.
- Provider outage with no fallback: `503 ROUTING_UNAVAILABLE`. Raw upstream errors are never exposed.
- **No ORS key configured:** the app uses OSRM automatically and says so in `meta`. A reviewer can run it with zero setup. The OSRM public demo is non-commercial, best-effort, requires a valid User-Agent and attribution, and blocks excessive use, so it is a convenience fallback, not production infrastructure.

### 6.4 Cache
- **Key:** SHA-1 of `route:v2:driving-car:us-only:{lat1:.4f},{lng1:.4f}:{lat2:.4f},{lng2:.4f}` (v2: only routes that stay in the US are stored). Rounding to 4 decimals (about 11 m) makes near-identical requests share an entry. Provider is **intentionally not** in the key: a route from the fallback provider is a valid route for the same origin and destination, and the planner does not care which engine produced it. The provider and the cache age are kept in `meta`.
- **TTL:** 7 days (road geometry changes slowly). Routes produced by the **fallback** provider get **15 minutes**, so traffic returns to the primary.
- `mpg` and `max_range_miles` are not in the key: they affect planning, not the route.
- Redis down: fall back to L1 only, no error.
- **L1 holds the derived route bundle** (display geometry, distance, duration, corridor candidates with position and offset), so a warm request runs only the optimizer and serialisation. **L2 holds the raw route** (encoded polyline, distance, duration, provider); an L2 hit rebuilds the bundle.
- Failures are never cached as results.

### 6.5 Request coalescing
- **In-process:** one in-flight task per key; followers await it. The leader runs the upstream call as a **detached, shielded task**, so a client disconnect on the leader's request does not cancel the call for everyone else.
- **Cross-worker:** a short Redis `SET key NX EX` lock. Followers wait on the cache with a bounded timeout, then **fail open** (make their own call, still budget-capped). This bounds duplicate calls to roughly one per worker even without Redis.

---

## 7. Planner

### 7.1 Corridor
Light simplification, buffer, `STRtree` query. Default corridor **8 miles**; adaptive widening 8 → 15 → 30 → 50 only when no feasible plan exists. If still infeasible: `422 NO_FEASIBLE_FUEL_PLAN`. The response always states `corridor_miles_used`; anything above 15 sets `meta.planning.corridor_widened=true`.

### 7.2 Projection
For each candidate compute `position_along_route` (projection onto the nearest segment of the simplified route, scaled so the total equals the router's reported distance) and `off_route_miles` (distance to that segment). The problem is now one-dimensional.

### 7.3 Detour-adjusted ranking price
Station coordinates are city centroids, so small offsets are noise, but large ones are real money (a 40-mile detour at 10 MPG costs about 8 gallons round trip). Each candidate gets:

```text
effective_price = price × (1 + 2 × max(0, off_route − FREE_OFFSET) / (mpg × G_REF))
FREE_OFFSET = 5 miles (absorbs centroid error),  G_REF = 25 gal (typical purchase)
```

`effective_price` is a **ranking proxy** that folds in estimated detour fuel. It is not an exact detour cost: station coordinates are city centroids and no extra routing calls are made per station. The optimizer ranks on this price; the response reports **real** prices and cash. Set `FREE_OFFSET` very large to disable it. Example: a $3.00 station 40 miles off-route ranks at about $3.84 and loses to a $3.10 station 2 miles off-route.

The optimality claim is therefore: **optimal for the modeled fixed-route planning problem**: the provider's single route, candidate stations within a corridor (city-centroid positions), and a detour-adjusted ranking price. It is not globally optimal real-world fuel cost including actual detours.

### 7.4 Optimizer (corrected)
Nodes: origin (cannot buy), candidate stations (sorted by position), destination (**price 0, cannot buy**). Capacity `G = max_range / mpg` (50 gal at defaults); start full.

At a station with current fuel `f`:
1. Find the **nearest station ahead within range with a strictly lower effective price** (the destination counts, since its price is 0).
2. If found: buy `max(0, (distance_to_it / mpg) − f)`. Buy nothing more.
3. If none: **fill the tank** (`G − f`).
4. Drive to the next node. Stations where nothing is bought are not reported as stops.

Tie rule: strictly lower only. On equal prices, fill here (deterministic, avoids chains of tiny buys).

Why it is correct (exchange argument):
- If a cheaper reachable station `j` exists, fuel beyond what is needed to reach `j` can be bought at `j` instead, at a lower price. Stations between here and `j` are priced at or above the current station, so buying here is no worse than buying there.
- If none is cheaper within range, every reachable station is at least as expensive, so filling here cannot raise future cost.
- Because the destination is a price-0 node, fuel is never over-bought at the end.

Complexity (n = indexed stations, k = corridor candidates):
- Tree query: the `STRtree` prefilters by bounding box, then an exact predicate runs on the survivors. For a long diagonal route the bounding box can cover most of the US, so the worst case is about O(n) with n of roughly 6.6k stations. Measured: the cold bundle build (decode, project, corridor query, projection of candidates, display geometry) takes about 16 ms for a 2.9k-point polyline and about 95 ms for a 46k-point one, and runs in a worker thread (`asyncio.to_thread`).
- Ordering and projection of candidates: O(k log k).
- Optimizer: O(k) with a monotonic stack once candidates are ordered.

**Verification:** a reference DP (fuel discretised in 0.5-gal units) runs in the test suite (`tests/property/`, Hypothesis). In the design-time check, 3,000 random instances (up to 25 stations, trips up to 2,400 miles, range 500; 2,354 feasible and 646 infeasible) gave the same cost for the corrected greedy and the DP on every feasible instance, and the two agreed on every infeasible one; the earlier draft ordering did not. The permanent tests cover both plain and effective-price instances.

---

## 8. Cost model

Defaults: start with a full tank; reserve 0 miles (`reserve_miles` configurable; it shrinks effective range).

**Facts** (always returned): `gallons_consumed`, `gallons_purchased`, `cash_spent_at_stops_usd`.

**Unknown from the assignment:** what the initial fuel cost. It is never presented as a fact.

```text
gallons_consumed          = route_miles / mpg
cash_spent_at_stops_usd   = Σ gallons_purchased × real station price
start_tank_used_gal       = max(0, gallons_consumed − gallons_purchased)
reference_price           = price at the first purchase; if no stop is needed,
                            the cheapest corridor candidate (named in the response)
estimated_total_fuel_cost_usd
  START_TANK_BILLING=reference (default) = cash_spent_at_stops_usd + start_tank_used_gal × reference_price
  START_TANK_BILLING=excluded            = null   (the response carries cash only)
```

**Why `reference` stays the default.** The assignment asks for "total money spent on fuel" for the trip. A cash-only figure reads $0 for a 300-mile trip and leaves out about one tank (50 gal) on a 2,800-mile trip, which a reviewer may read as a bug. Because cash is always returned beside a clearly named estimate, and `initial_fuel` states the valuation method, neither reading is hidden. If you prefer the strictly factual default, set `START_TANK_BILLING=excluded`; a short trip then returns `cash_spent_at_stops_usd: 0` and `estimated_total_fuel_cost_usd: null`, which is also legitimate.

---

## 9. API contract

### `POST /api/v1/route`
```json
{
  "start": "Dallas, TX",
  "finish": "New York, NY",
  "mpg": 10,
  "max_range_miles": 500
}
```
`mpg` (1–100, default 10) and `max_range_miles` (50–500, default 500) are optional with validated bounds. Request body size is bounded (4 KB).

### Response (illustrative values, not computed)
```json
{
  "route": {
    "distance_miles": 1550.2,
    "duration_seconds": 80000,
    "geometry": {"type": "LineString", "coordinates": []},
    "geometry_detail": "simplified",
    "bbox": []
  },
  "fuel_stops": [{
    "order": 1, "station_id": "135", "name": "LOVES TRAVEL STOP #766",
    "address": "I-80, EXIT 27", "city": "Atkinson", "state": "IL",
    "lat": 41.4, "lng": -90.0, "location_precision": "city_centroid",
    "mile_marker": 412.3, "off_route_miles": 1.8,
    "price_per_gallon": 3.389, "gallons_purchased": 31.2, "cost_usd": 105.74
  }],
  "fuel": {
    "gallons_consumed": 155.0,
    "gallons_purchased": 105.0,
    "cash_spent_at_stops_usd": 355.80,
    "average_purchase_price_per_gallon": 3.39,
    "initial_fuel": {
      "gallons": 50.0,
      "cost_included_in_estimate": true,
      "valuation": "first_purchase_price",
      "reference_price_per_gallon": 3.389,
      "reference_station": null
    },
    "estimated_total_fuel_cost_usd": 525.25,
    "estimate_basis": "cash_spent_at_stops_usd + initial_fuel.gallons x initial_fuel.reference_price_per_gallon"
  },
  "map_url": "/api/v1/route/map?start=...&finish=...",
  "meta": {
    "request_id": "...",
    "routing":  {"provider": "ors", "routing_calls": 1, "geocode_calls": 0, "external_calls": 1,
                 "fallback_used": false, "us_only_reroute": false},
    "cache":    {"route": "miss", "layer": null, "age_seconds": 0.0},
    "planning": {"candidate_stations": 42, "selected_stops": 5, "corridor_miles_used": 8,
                 "corridor_widened": false, "algorithm": "next_cheaper_greedy",
                 "model": "fixed_route_detour_adjusted_ranking_price", "optimality": "..."},
    "performance": {"routing_ms": 0, "planning_ms": 0, "total_ms": 0},
    "assumptions": {"mpg": 10, "max_range_miles": 500, "reserve_miles": 0,
                    "initial_fuel_full": true, "start_tank_billing": "reference",
                    "station_price_rule": "minimum", "station_location_precision": "city_centroid"},
    "locations": {"start": {"input": "Dallas, TX", "lat": 0, "lng": 0, "precision": "city_centroid",
                            "matched": "Dallas, TX"}, "finish": {"...": "..."}},
    "warnings": []
  }
}
```
Geometry is simplified by default, with `?geometry_detail=full` available, and responses are gzipped. Latency fields are measured at runtime, never hard-coded.

### Errors
`{"error": {"code": "...", "message": "...", "request_id": "..."}}`

| Status | Code |
|---|---|
| 400 | `INVALID_REQUEST` |
| 422 | `LOCATION_OUTSIDE_SUPPORTED_AREA`, `LOCATION_NOT_ROUTABLE`, `ROUTE_LEAVES_SUPPORTED_AREA`, `NO_FEASIBLE_FUEL_PLAN` |
| 429 | `RATE_LIMITED` |
| 503 | `ROUTING_UNAVAILABLE` |
| 500 | `INTERNAL_ERROR` (generic; details only in logs) |

---

## 10. Map endpoint and ASGI lifecycle

**Map:** `GET /api/v1/route/map?start=…&finish=…` serves a static Leaflet HTML shell. Its JavaScript calls `POST /api/v1/route` and renders the route, start/finish markers, and numbered stops with popups (price, gallons, cost). One code path, no duplicate logic. The default OSM tile server is for light use; swap the tile provider for real traffic.

**Lifecycle (decided).** No custom lifespan wrapper. The station store and place index load in `AppConfig.ready()` (tolerating missing artifacts so `build_stations` can run first) and lazily on first use; the `httpx.AsyncClient` is created lazily per running event loop. The check was done: no wrapper is needed (the app runs under uvicorn and the artifacts load in `ready()`).

**CPU work in async views:** the heavy step, building the route bundle on a cache miss (about 16-95 ms), runs in `asyncio.to_thread`. Per-request planning on a warm bundle (corridor filter, optimizer) takes well under 1 ms and runs inline.

---

## 11. Observability, health, security

- **Headers:** `X-Request-ID`; `Server-Timing: route;dur=…, corridor;dur=…, optimize;dur=…, serialize;dur=…, total;dur=…` (measured values only).
- **Structured logs:** `route_completed` with request id, cache result, provider, routing calls, candidate/selected counts, timings. No secrets.
- **Health:** `GET /healthz` (process alive); `GET /readyz` (`StationStore` loaded, `STRtree` built; Redis not required).
- **Security:** strict validation, bounded body size, keys only from environment, upstream timeouts, an optional fixed-window per-IP rate limit (off by default; Redis-backed when `REDIS_URL` is set, otherwise in-memory), no arbitrary outbound URLs, generic error bodies.

---

## 12. Testing and performance acceptance

- **Unit:** location normalisation, USA polygon, price rule, projection, corridor, cost model, range boundaries (exactly 500 miles reachable within tolerance).
- **Property (hypothesis):** greedy cost equals reference DP cost, on both plain and effective-price instances; infeasible cases agree.
- **API (respx mocks):** cold, warm, 100 concurrent identical requests produce 1 upstream call, primary failure → fallback, cross-worker lock fail-open, leader cancellation, outside USA, non-routable point, infeasible plan, counter semantics (`routing_calls` vs `geocode_calls` vs `external_calls`), start equals finish, short trip costs a real figure.
- **Golden:** one recorded real route response (Dallas to New York) pinning stops, gallons, cost; also lets reviewers run the suite offline.
- **Latency:** `scripts/bench.py` (httpx + asyncio): warm, cold, identical-concurrent, distinct-concurrent. It reports p50/p95/p99, requests/sec, error rate, routing calls per request and cache hit ratio. The README states **measured** numbers with environment details. The benchmark table there uses a **stub provider** and says so; real-provider latency is reported separately and only as single observations.

---

## 13. Project layout

```text
manage.py  pyproject.toml  Makefile  Dockerfile  docker-compose.yml  .env.example  README.md  ARCHITECTURE.md  FINAL_REVIEW.md
config/     settings.py  asgi.py  urls.py
api/        views.py  schemas.py  errors.py  middleware.py  apps.py  urls.py  map.html  management/commands/build_stations.py
stations/   etl.py  store.py
routing/    providers.py (interface, ORS, OSRM)  cache.py  singleflight.py  resolver.py  geometry.py  budget.py
planner/    corridor.py  optimizer.py  cost.py
data/       raw/fuel-prices-for-be-assessment.csv  gazetteer/ (gitignored)  build/ (committed: stations.npz, places.json, usa.geojson, foreign.geojson, unmatched.csv, build_report.json)
scripts/    serve.py  bench.py  stub_osrm.py
tests/      conftest.py  unit/  property/  api/  fixtures/
docs/       postman_collection.json
```
`stations/`, `routing/`, `planner/` are plain Python packages with no Django imports; `api/` is the only Django app. Build artifacts are committed so a reviewer can run the app without re-running the ETL.

---

## 14. Build order (as executed)

1. **Data:** ETL, gazetteer join, `stations.npz`, review `unmatched.csv`.
2. **Planner:** projection, corridor, effective price, optimizer, cost model.
3. **Correctness:** reference DP, hypothesis tests, edge cases.
4. **Routing:** interface, ORS + OSRM adapters, pooled client, timeouts, budget, fallback, breaker.
5. **Performance:** L1/L2 cache, coalescing (in-process then Redis lock), request IDs, `Server-Timing`.
6. **API:** schemas, endpoint, errors, middleware, map page; add an ASGI wrapper only if verification proves it is required.
7. **Delivery:** Docker, bench with measured numbers, README, Postman collection.

---

## 15. Trade-offs and evolution

| Choice | Over | Because |
|---|---|---|
| In-memory stations | PostGIS | About 6.6k immutable points, read-heavy, no DB hop |
| ORS primary, OSRM fallback | One provider | Resilience; OSRM demo is not production infrastructure |
| Native async views + pydantic | DRF | Two endpoints; thinner hot path; assignment requires Django, not DRF |
| Synchronous planning | Celery | Planning takes milliseconds |
| Modular monolith | Microservices | One bounded domain; provider and store interfaces give the isolation |

If this became a product: Django × N behind a load balancer, Redis cluster, PostGIS for dynamic stations, self-hosted OSRM, all behind the same provider and store interfaces, so the planner does not change.

---

## 16. Implementation-time checks: outcome

| Item | Outcome |
|---|---|
| ORS base URL, auth header, request body, `radiuses`, response format, quotas | URL, header, body and response parsing implemented per the ORS docs and exercised by live manual requests (HTTP 200). The old host's deprecation notice was checked against the HeiGIT announcement. **Quotas remain unverified**; check the dashboard. Intermittent ORS timeouts were observed; OSRM fallback handled them. |
| ORS `avoid_borders` behaviour (a forum thread reports error 2099 with `all`) | Used only for the one border re-route; covered by mock tests. **Not exercised live.** If ORS rejects it, the API answers 422 `ROUTE_LEAVES_SUPPORTED_AREA` instead of planning a route through Canada or Mexico. |
| Census and GeoNames download URLs and file layouts | Census loader written and unit-tested on a fixture; the Census host was unreachable when the artifacts were built, so the committed place table comes from a public US-cities dataset. GeoNames is not used. Rebuilding from Census is the open data-quality item (see `FINAL_REVIEW.md`). |
| Django 6.1 ASGI lifespan; uvicorn multi-worker flags | Resolved: no lifespan wrapper needed. `uvicorn --workers N` stalls keep-alive responses ~40 ms (listener without TCP protocol), so `scripts/serve.py` binds the socket itself. |
| Tree-query cost on long diagonal routes | Measured: cold bundle build about 16 ms (2.9k-point polyline) and about 95 ms (46k points); no chunking needed. |
| Gazetteer match rate | 99.83% (6,615 of 6,626 stations; 11 unmatched, listed in `unmatched.csv`). |
| Station precision | City-level by data limitation; stated in every response. |

---

## 17. Implementation decisions

**Scope**
- No database, models, admin, templates, CSRF or auth. `DATABASES = {}`, `INSTALLED_APPS = ["api"]`, middleware: `GZipMiddleware` plus one custom middleware (request id, `Server-Timing`, per-IP rate limit). The map page is a static HTML file read once.
- Dependencies: Django (`>=6.1.2,<6.2`), pydantic, httpx, numpy, shapely>=2, pyproj, orjson, cachetools, redis, uvicorn. Dev: pytest, pytest-asyncio, hypothesis, respx, ruff. ETL uses stdlib `csv` and `difflib` (no pandas, no rapidfuzz).

**Data**
- Gazetteer: the ETL loads the Census Gazetteer *places* national file first when present (strip the type suffix such as city/town/village/CDP; prefer non-CDP, then larger land area), then fills gaps from a public US-cities CSV. The committed artifacts currently come from the US-cities CSV alone (Census was unreachable at build time). Fuzzy match with `difflib` inside the state only, cutoff 0.88. Normalise `St.`→Saint, `Mt.`→Mount, `Ft.`→Fort, punctuation and case.
- Artifacts in `data/build/`: `stations.npz` (id, price, lat, lon, name, address, city, state), `places.json` (`"ST|normalised name"` → lat, lon), `usa.geojson` (contiguous-US land, Natural Earth 10 m, simplified), `foreign.geojson` (1° Canada/Mexico band), `unmatched.csv`, `build_report.json`.
- USA check: endpoints inside the US polygon buffered by 0.02°; route test per amendment 1 (supersedes the 0.1° / 3-mile rule).
- Expected profile (from the supplied CSV): 8,151 rows; 620 Canadian rows dropped; 6,626 unique US stops; 26 identical duplicate rows; 487 stops with conflicting prices. Investigate any difference.

**Routing and caching**
- Providers return one `Route(polyline, distance_miles, duration_s, provider)`; unit conversion happens at the provider boundary. Coordinates are `[lng, lat]`. ORS: `POST`, key in the `Authorization` header, `instructions: false`, widened `radiuses`. OSRM: `overview=full&geometries=polyline&steps=false`. Both polylines are precision 5 unless verification says otherwise.
- Policy: timeout, connect error, 429 and 5xx fall back to the other provider (no same-provider retry). A not-routable response maps to `422 LOCATION_NOT_ROUTABLE`. No fallback configured: `503 ROUTING_UNAVAILABLE`. Circuit breaker opens after 5 consecutive failures with a 30 s cooldown. Timeouts: connect 2 s, read `ROUTING_TIMEOUT_S` (default 5 s).
- Polyline decoding is vectorised numpy, tested against the standard vector ``_p~iF~ps|U_ulLnnqC_mqNvxq`@`` → (38.5, -120.2), (40.7, -120.95), (43.252, -126.453).
- Cache: L1 `TLRUCache` of bundles (max 256 entries, 7 days; 15 minutes for fallback routes); L2 Redis only when `REDIS_URL` is set.

**Planner**
- Project the route to EPSG:5070 once. Query the `STRtree` once at the maximum corridor (50 miles) on a route simplified to about 500 m, compute position and offset for every candidate, then filter per corridor width (8, 15, 30, 50). Feasible means every consecutive gap, including origin to first station and last station to destination, is within usable range (`max_range_miles − reserve_miles`).
- Scale positions so the route length equals the provider distance.
- Optimizer: nearest strictly cheaper node via one monotonic-stack pass over effective prices with the destination at price 0; tolerance 1e-6; zero-purchase stations are not reported.
- Display geometry: simplify at 150 m in EPSG:5070, convert back to lon/lat at 5 decimals, pre-serialise once with orjson and splice into the response.

**Runtime**
- Run with `python scripts/serve.py` (see Server row for why not `uvicorn --workers`).
- Rate limit: fixed window per IP, off by default (`RATE_LIMIT_PER_MIN=0`); Redis if configured, else in-memory.
- Environment: `ROUTING_PROVIDER`, `ORS_BASE_URL`, `ORS_API_KEY`, `OSRM_BASE_URL`, `REDIS_URL`, `WEB_CONCURRENCY`, `START_TANK_BILLING=reference`, `STATION_PRICE_RULE=min`, `MAX_ROUTING_CALLS=3`, `RATE_LIMIT_PER_MIN=0` (off), `ROUTING_TIMEOUT_S=5`, `RESERVE_MILES`, `FREE_OFFSET_MILES`, `G_REF_GALLONS` (see `.env.example`).
- Geocoder: not implemented (see section 4 and amendment 5); `geocode_calls` is reserved and always 0.

**Tests**
- `conftest.py` sets `DJANGO_SETTINGS_MODULE`, calls `django.setup()`, and uses `django.test.AsyncClient`, a small synthetic station artifact, and respx mocks. One smoke test runs against the real artifact when it exists.
- DP oracle: fuel discretised in 0.5-gal units with station positions on multiples of 5 miles (at 10 MPG, needs are then exact multiples of 0.5 gal). Compare cost against the greedy on plain and effective-price instances; infeasible cases must agree.
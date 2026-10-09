# Final review: changes, rationale, verification

**Timeline.** This file records two stages. **Part 1** is the review and build snapshot, made in an environment with no network access to the routing providers
and no Docker daemon; its statements about what was "not verified" describe that environment only and are kept as history. **Part 2** records what was
verified afterwards on a developer machine with live services (October 2026) and what is still open. Where the two differ, Part 2 is the current status.

---

# Part 1: Review snapshot (original build environment)

Every review item was judged against the code and the assignment, not applied blindly. "Verified" in this part means exercised in the authoring environment;
that environment blocked ORS, OSRM, Nominatim, Census, GeoNames, Docker Hub, unpkg and OSM tiles, and had no Docker daemon.

## Decisions on the review items
| item | decision | why |
|---|---|---|
| ORS endpoint | **Changed** default to `https://api.heigit.org/openrouteservice`; key optional; OSRM default/fallback; `.env.example` updated, no secrets | Documented endpoint; keeps the zero-config path working |
| Real-route tests (8 trips) | **Done with synthetic provider-shaped routes** (real city pairs and real distances, waypoint polylines) through the *real* station artifact. Live verification was pending at the time (see Part 2) | No network to ORS/OSRM |
| Border correctness | **Implemented** (see below) | The old "flag only" behaviour could silently plan a Detroit→Buffalo route through Ontario |
| Optimality wording | **Changed** in API (`meta.planning.optimality`), README, ARCHITECTURE; heuristic kept and explained | Honest; the objective is unchanged and DP-verified for the ranking model |
| Station location accuracy | **Kept city centroids, exposed precision** (`station_location_precision`) | Only 5 of 6,626 CSV addresses are street-level; no reliable offline geocoder; per-request geocoding is forbidden |
| Input coverage | **Strong offline contract**: parser widened (`D.C.`, state names, address tail), `meta.locations` precision + `warnings`; no network geocoder | A bounded geocoder could not be verified here |
| max_range ≤ 500 | **Enforced** (schema 50-500); mpg optional (default 10); route cache independent of both (tested: 1 provider call for 5 variants) | Assignment states 500 |
| Fuel-cost semantics | **Done**: `cash_spent_at_stops_usd` (fact) vs `estimated_total_fuel_cost_usd` with explicit valuation basis; no misleading $0 | |
| Small purchases | **Objective unchanged** (6 of 32 stops in the region sweep buy < 2 gal); documented | A change would alter proven optimality, and extra stops do not raise the total fuel cost |
| Latency / cache / failure matrix | Measured and tested (below); no change needed beyond the border flow and L2 safety | |
| Map | SRI hashes, graceful CDN/tile failure, stops table, error display; verified in headless Chromium | |
| Docker | Non-root, `HEALTHCHECK`, deps from `pyproject.toml`, compose healthchecks, `ALLOWED_HOSTS` override fixed. Not built at that time (see Part 2) | No daemon |

## Main changes
- **US-only routing** (`routing/geometry.py` `UsaArea`, `routing/providers.py`, `routing/cache.py`, `planner/corridor.py`): ETL now builds Natural Earth 10 m US
  (with islands) and a 1° Canada/Mexico band; a route with an unbroken 2+ mile stretch inside the band is "leaving the US". ORS gets one re-route with
  `avoid_borders=all`; OSRM or a still-crossing route gives 422 `ROUTE_LEAVES_SUPPORTED_AREA`. Counted in the 3-call budget; cached bundles record
  `border_avoided`; a stale cached L2 route that crosses is discarded. (Replaces the earlier coastline-polygon flag, which false-positived on bridges.)
- **Errors:** `NO_FEASIBLE_FUEL_PLAN` now names the uncovered stretch and range.
- **API:** `max_range_miles` ≤ 500; `meta.locations`, `meta.warnings`, `us_only_reroute`, `optimality`, `estimate_basis`, `station_location_precision`.
- **Config:** invalid enum/range values fail at start-up; `.env.example` lists every variable.
- **Redis bug fixed:** numpy scalars broke L2 serialisation (found by the border tests); fixed and covered.
- **Tests:** 74 → 106 (border flow, regions, schema cap, reuse of the route across mpg/range, locations, cost valuation, settings).
- Docs: README rewritten, ARCHITECTURE amended, Postman extended, this file.

## Verified in the original environment
- `ruff check` clean; `pytest`: 106 passed with a real `redis-server` (104 passed + 2 skipped without it).
- Hypothesis: greedy equals the DP oracle (3,000 examples, plain and effective prices, infeasible agreement).
- 100 concurrent identical requests → 1 provider call (unit, API, and across two real workers via Redis).
- `scripts/serve.py` with 2 workers + Redis + stub OSRM: `/readyz` 200; four requests, 1 upstream call; warm request 3.5-4.9 ms client-side.
- Region sweep (real station data, **synthetic routes**). cash / estimate in USD:
  Dallas→NY 1,550 mi 6 stops 301.62/441.65 · LA→Chicago 2,015 mi 6 stops 451.15/612.10 · Seattle→Miami 3,300 mi 13 stops 868.00/1,045.95 ·
  NY→Boston 0 stops · Detroit→Buffalo (US-only route) 0 stops · Houston→Atlanta 790 mi 5 stops 82.31/219.76 · Phoenix→Denver 840 mi 2 stops 108.34/268.04.
  All stops within the corridor, ≤ 500-mile gaps, plausible prices. SF→Las Vegas correctly returns 422 (CSV has 8 California stations).
- Map: Leaflet from the npm tarball served under the real SRI hashes (accepted); with tiles blocked a notice appears and the route/pins/table render;
  with Leaflet blocked the text summary and stops table render, no JS errors.
- Benchmarks: README table (**stub provider**). Cold bundle build ~16 ms (2.9k pts) / ~95 ms (46k pts).

## Not verified in that environment
Live ORS/OSRM (including ORS `avoid_borders` behaviour; request shape from docs); real road geometry; Docker build/up/healthcheck/restart; OSM tiles in a real
session; real-provider latency; behaviour under a dedicated multi-core host. *Several of these were later checked; see Part 2.*

---

# Part 2: Later verification (developer machine, live services, October 2026)

These are manual results reported by the author. They show that the tested workflows work; they are not an exhaustive or load test.

## Verified
| area | result |
|---|---|
| Docker Compose | App (`WEB_CONCURRENCY=2`) and Redis containers started healthy; `/healthz` and `/readyz` passed; Redis answered `PONG` |
| Cache persistence | A cached route survived an app restart and was served from Redis: `cache: hit`, `layer: l2`, `external_calls: 0` |
| Live routing | OSRM and ORS both returned routes. Candidate counts of 55, 301 and 242 produced plans with 2, 7 and 10 stops; repeated requests made no new provider calls |
| Real-route trips | Dallas → New York 1,558.2 mi, 7 stops, $443.14 estimated (8 MPG / 350 mi: 11 stops, $559.71). Los Angeles → Las Vegas 276.2 mi, 0 stops. Los Angeles → Salt Lake City 693.0 mi, 2 stops. Los Angeles → Dallas 1,431.7 mi, 7 stops, $274.96 cash / $435.91 estimated. Reported legs stayed within the 500-mile range in the LA→SLC and LA→Dallas checks |
| Map page | Opened in a desktop browser; route, stops and totals rendered |
| API (Postman) | City and coordinate inputs, changed MPG and range worked; MPG 0, missing finish and `max_range_miles=501` were rejected |
| Automated suite | 106 passed, 0 failed, 0 skipped with Redis configured (an earlier run without Redis skipped 2) |
| Provider latency | One cold Los Angeles → Las Vegas request through the public OSRM demo spent about 2.6 s in the routing call (single observation) |

## Open items
- **Intermittent ORS timeouts.** Some ORS requests timed out and the fallback to OSRM engaged. A cold request that hits the 5 s read timeout therefore costs
  roughly 5 s plus the fallback call; warm requests are unaffected. Sustained provider reliability and quotas have not been established. Options if this
  matters: lower `ROUTING_TIMEOUT_S` once typical successful ORS latency is known (see `meta.performance.routing_ms`), or hedge the fallback call.
- **ORS `avoid_borders`** has not been exercised against the live service (mock tests only).
- **Place and station coordinates.** The committed table comes from a public US-cities dataset because the Census gazetteer was unreachable at build time
  (the Census loader exists and is unit-tested but has never run on the real file). The earlier claim "median 1.4 mi, p90 12.5 mi, max 15.2 mi vs 48 reference
  city centres" is **withdrawn**: a later spot check found larger errors, for example North Las Vegas resolving about 23 miles from the city centre (toward
  Moapa), and 12-15 miles for Dallas, Jacksonville, Columbus, Houston and Austin. Next step: run `python manage.py build_stations --download` on a machine
  that can reach the Census host, check `build_report.json` lists the `census` source, re-measure, and then restate the accuracy figures.
- **Not established:** every route across the USA, behaviour under real-user load, and a production deployment.

## Known limitations
- Station and place locations are city centroids (see above); mile markers and off-route distances are approximate.
- California is nearly absent from the supplied CSV (8 stations): long CA trips can be infeasible (San Francisco → Las Vegas returns a clear 422).
- Detour-adjusted ranking is a heuristic proxy, not a road-network optimum; small top-ups occur.
- Border rule is polygon-based; routes grazing the border (< 2 mi or within ~1 mi of the line) are not flagged.
- Public OSRM demo is rate-limited; use an ORS key for production.
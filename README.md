# Fuel Route API

[![CI](https://github.com/S8r2j/routing-api/actions/workflows/ci.yml/badge.svg)](https://github.com/S8r2j/routing-api/actions/workflows/ci.yml)

A Django REST API that accepts a US start and end location and returns a cost-effective fuel stop plan for the route. The API fetches the driving route from OSRM, finds all fuel stations along the corridor using an in-memory spatial index, and runs a two-phase greedy algorithm that minimises total fuel cost by buying cheap fuel in bulk and avoiding expensive stations where possible. The response includes the full route geometry, an ordered list of fuel stops with per-stop cost breakdowns, and a summary of total spend.

---

## Tech Stack

| Concern | Choice | Why |
|---|---|---|
| Framework | Django 5.x + Django REST Framework | Requirement; DRF handles input validation and response serialisation |
| ASGI server | Uvicorn (multi-worker) | Runs Django in async mode so multiple route requests can be processed concurrently without blocking on the OSRM network call |
| Routing API | OSRM public demo | Free, no API key required; returns a full route polyline and distance in a single HTTP call |
| HTTP client | `httpx` (async) | Non-blocking HTTP so the OSRM call does not stall the event loop while other requests are being served |
| Spatial index | `scipy.spatial.KDTree` | Built once at startup from all station coordinates; all per-request station lookups are in-memory O(log n) with no database queries |
| Geocoding (stations) | `pgeocode` | Resolves city + state to lat/lon using bundled offline geonames data — no network calls, no rate limits |
| Geocoding (user input) | `pgeocode` (tier-1) + `geopy` Nominatim (tier-2) | pgeocode handles the common `City, ST` format instantly; Nominatim is only called when pgeocode cannot parse or match the input. Three input guards run before any geocoding: non-Latin script rejection, SQL/special-character sanitisation, and CONUS bounding-box validation after coordinates are resolved |
| Route cache | Django `LocMemCache` (dev) / Redis (prod) | Identical start/end pairs return a cached response in ~1ms, bypassing geocoding, OSRM, and the planner entirely |
| Throttle cache | `FileBasedCache` (dev) / Redis (prod) | Shared across all Uvicorn worker processes so the per-IP rate limit is enforced globally, not per-worker |
| Rate limiting | DRF `AnonRateThrottle` | 60 requests/minute per IP by default; returns `429` with `Retry-After` header; configurable via `THROTTLE_RATE_ANON` env var |
| CORS | `django-cors-headers` | Allows cross-origin requests from any origin by default; restrict to specific domains in production via `CORS_ALLOWED_ORIGINS` env var |
| Database | SQLite (dev) / PostgreSQL via `DATABASE_URL` env var | Stores the pre-geocoded station table; only read at startup to build the KD-tree |

---

## Code Architecture

The codebase is split into four distinct layers. Each layer has a single responsibility and no layer reaches past its immediate neighbour.

```
apps/routing/
├── views.py                      # HTTP layer — validate input, check cache, return JsonResponse
├── serializers.py                # Input validation (DRF serializer)
├── models.py                     # Django ORM model definition (FuelStation)
├── middleware.py                 # Request logging
├── exceptions.py                 # Shared exception types
└── services/
    ├── db.py                     # Database layer — all ORM queries and writes
    ├── route_service.py          # Orchestration layer — geocode → OSRM → index → planner
    ├── station_index.py          # Spatial index — KDTree build and lookup (reads from db.py)
    ├── geocoder.py               # Geocoding — pgeocode tier-1, Nominatim tier-2; input guards + CONUS validation
    ├── osrm.py                   # OSRM HTTP client
    └── planner.py                # Algorithm — pure functions, no I/O
```

| Layer | File | What it may call |
|---|---|---|
| HTTP | `views.py` | `route_service.plan_route()` only |
| Orchestration | `route_service.py` | `geocoder`, `osrm`, `station_index`, `planner` |
| Spatial index | `station_index.py` | `db.get_all_stations()` at startup only |
| Database | `db.py` | Django ORM (`FuelStation.objects.*`) |
| Algorithm | `planner.py` | No external calls — pure Python/NumPy |

`views.py` never imports the ORM, never calls OSRM directly, and never runs planner logic. `planner.py` has no knowledge of Django, HTTP, or databases. `db.py` is the only file that touches the ORM, making it the single point to update if the database backend or schema changes.

---

## Setup & Installation

**1. Configure environment variables**

Copy the example env file and edit it with your values. `python-decouple` reads `.env` automatically — no `export` or shell sourcing needed.

```bash
cp .env.example .env
```

Key variables to review before starting:

| Variable | Default | Description |
|---|---|---|
| `THROTTLE_RATE_ANON` | `60/minute` | Max requests per IP per minute |
| `CORS_ALLOW_ALL_ORIGINS` | `True` | Set to `False` and define `CORS_ALLOWED_ORIGINS` to restrict in production |
| `REDIS_URL` | _(unset)_ | Set to a Redis URL in production for shared caching across workers |
| `DATABASE_URL` | _(unset, uses SQLite)_ | Set to a PostgreSQL URL in production |
| `OSRM_BASE_URL` | public demo server | Replace with a self-hosted OSRM instance for reliability |
| `VEHICLE_TANK_RANGE_MILES` | `500` | Maximum driving range on a full tank in miles |
| `VEHICLE_MPG` | `10` | Vehicle fuel efficiency in miles per gallon |

**2. Install dependencies**

Installs all required Python packages including Django, DRF, httpx, scipy, pgeocode, geopy, django-cors-headers, and Uvicorn.

```bash
pip install -r requirements.txt
```

**3. Run database migrations**

Creates the `FuelStation` table (and any other Django-managed tables) in the database. Must be run before loading stations or starting the server.

```bash
python3 manage.py migrate
```

**4. Load fuel stations (run once)**

Reads `data/fuel-prices-for-be-assessment.csv`, geocodes each station's city and state to latitude/longitude coordinates, and saves the results to the `FuelStation` database table. This must be completed before starting the server — the spatial index is built from these rows at startup, and the API will return empty fuel stop plans if no stations are loaded.

The command is idempotent: re-running it updates existing records rather than creating duplicates, so it is safe to run again after updating the CSV.

```bash
python3 manage.py load_stations
```

Geocoding uses `pgeocode` offline data as the primary source. If a station's city/state cannot be resolved by pgeocode, the command falls back to Nominatim and waits 1 second between calls to respect the service's rate limit. For large CSVs where many stations fall through to the Nominatim fallback, increase this delay to avoid being throttled or blocked:

```bash
python3 manage.py load_stations --nominatim-delay 1.5
```

Stations that cannot be geocoded by either method are skipped and logged with a specific reason (e.g. `non-US state code (ON)`, `Nominatim returned no result`). The command prints a final count of loaded vs skipped rows.

**5. Start the server**

Starts the Uvicorn ASGI server with 4 worker processes. Each worker independently loads the KD-tree spatial index during its lifespan startup event before accepting any traffic. The `--workers 4` flag means up to 4 concurrent route requests can be handled simultaneously without blocking each other on the OSRM network call.

```bash
uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 4
```

---

## API Usage

### Endpoint

```
GET /api/v1/route/
```

Accepts a start and end location as query parameters. Both must be locations within the continental United States. Returns the driving route, an ordered list of recommended fuel stops, and a cost summary.

### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `start` | string | Yes | Origin location, e.g. `New York, NY` or a 5-digit ZIP code |
| `end` | string | Yes | Destination location, e.g. `Los Angeles, CA` or a 5-digit ZIP code |

### Example Request

```bash
curl "http://localhost:8000/api/v1/route/?start=New+York,+NY&end=Los+Angeles,+CA"
```

### Response Fields

**`route`** — Basic route metadata:
- `start` / `end` — the input location strings as provided
- `total_miles` — total driving distance in miles
- `geometry` — GeoJSON `LineString` of the full route, suitable for rendering on a map

**`fuel_stops`** — Ordered list of stops from origin to destination. Each stop includes:
- `name`, `address`, `city`, `state` — station identification
- `lat`, `lon` — coordinates
- `mile_marker` — how far along the route this stop is
- `price_per_gallon` — retail fuel price at this station
- `gallons_purchased` — how many gallons to buy here
- `stop_cost` — total spend at this stop (`gallons_purchased × price_per_gallon`)
- `fuel_remaining_on_arrival_gallons` — how much fuel is left in the tank when arriving at this stop
- `is_recommended` — `true` if this station was chosen as the cheapest reachable option; `false` if it was a forced stop because no other station was within range
- `distance_to_next_stop_miles` — miles to the next fuel stop, or to the destination if this is the last stop

**`summary`** — Aggregate totals:
- `total_gallons` — total fuel purchased across all stops
- `total_fuel_cost` — total spend in USD
- `num_stops` — number of fuel stops
- `avg_price_per_gallon` — spend-weighted average price paid

### Example Response

```json
{
  "fuel_stops": [
    {
      "name": "PILOT TRAVEL CENTER #123",
      "address": "I-80, EXIT 304",
      "city": "Cheyenne",
      "state": "WY",
      "mile_marker": 412.3,
      "lat": 41.139,
      "lon": -104.820,
      "price_per_gallon": 3.12,
      "gallons_purchased": 38.5,
      "stop_cost": 120.12,
      "fuel_remaining_on_arrival_gallons": 0.0,
      "is_recommended": true,
      "distance_to_next_stop_miles": 387.1
    }
  ],
  "summary": {
    "total_gallons": 278.9,
    "total_fuel_cost": 921.44,
    "num_stops": 6,
    "avg_price_per_gallon": 3.30
  },
  "route": {
    "start": "New York, NY",
    "end": "Los Angeles, CA",
    "total_miles": 2789.4,
    "geometry": {
      "type": "LineString",
      "coordinates": [[-74.006, 40.713], ["..."]]
    }
  }
}
```

### Error Responses

| Status | Scenario | Example message |
|---|---|---|
| `400` | Missing or blank `start` or `end` parameter | `"start query parameter is required"` |
| `422` | Input contains non-Latin script (CJK, Arabic, Cyrillic, etc.) | `"Please provide the location in English (e.g., 'New York, NY' or 'Los Angeles, CA')"` |
| `422` | Input contains SQL/injection characters (`'`, `;`, `=`) | `"Invalid location input"` |
| `422` | Location cannot be geocoded | `"Could not resolve location: 'Xyz City'"` |
| `422` | Resolved coordinates fall outside the CONUS bounding box | `"Location must be within the continental United States"` |
| `422` | Route is under 10 miles | `"Route too short to plan fuel stops"` |
| `422` | No station reachable within vehicle range | `"No fuel station within vehicle range between mile 340.0 and mile 840.0"` |
| `429` | Rate limit exceeded | `"Request was throttled. Expected available in X seconds."` |
| `502` | OSRM returned a non-200 response | `"OSRM returned 503"` |
| `504` | OSRM request timed out | `"OSRM request timed out"` |

### Input Validation & Geocoding Guards

The geocoder applies three checks in order before resolving any location string. All three raise a `422` and never reach the geocoding network tier.

| Order | Check | Catches |
|---|---|---|
| 1 | **SQL / injection characters** — rejects input containing `'`, `;`, or `=` | `'; DROP TABLE ...`, `' OR '1'='1` |
| 2 | **Non-Latin script detection** — rejects CJK, Arabic, Cyrillic, Devanagari, and similar Unicode script blocks | `北京市`, `مدينة نيويورك`, `Москва` |
| 3 | **CONUS bounding box** — after pgeocode or Nominatim returns coordinates, validates `24.4 ≤ lat ≤ 49.4` and `-125.0 ≤ lon ≤ -66.9` | Locations outside the continental USA that pass as ASCII text (e.g. `London, UK`, `Anchorage, AK`, `Honolulu, HI`) |

Inputs that pass all three checks but still cannot be geocoded (unknown city name, no Nominatim result) return `422 "Could not resolve location: '...'"`.

---

## Algorithm Design

The fuel stop planner converts a 2-D driving route into a 1-D optimisation problem, then solves it in three sequential phases. All processing is in-memory with no database queries on the hot path.

### Vehicle Parameters

All vehicle parameters are configurable via environment variables. The defaults match the assignment specification.

| Parameter | Default | Env variable | Notes |
|---|---|---|---|
| Maximum range on full tank | 500 miles | `VEHICLE_TANK_RANGE_MILES` | |
| Fuel efficiency | 10 mpg | `VEHICLE_MPG` | |
| Tank capacity | 50 gallons | — | Derived: `VEHICLE_TANK_RANGE_MILES ÷ VEHICLE_MPG` |
| Starting fuel | Sufficient to reach first stop | — | Vehicle arrives at the first stop with 0 gallons remaining |

---

### Phase 1 — Station Projection

The OSRM route is a polyline: a sequence of thousands of GPS coordinates forming the driving path. Fuel stations exist in 2-D space (latitude/longitude) and must be mapped onto this 1-D path before any cost reasoning can happen.

For every candidate station within ~24 miles of the route corridor, the algorithm finds the nearest point on the polyline using a KD-tree spatial index built on the route coordinates. Each station is assigned a **mile marker** — the cumulative driving distance from the origin to that nearest point. Stations more than 20 miles from the route are discarded as unreachable without a significant detour.

After projection, all stations are sorted by mile marker ascending, producing a 1-D array of `(mile, price)` tuples that represents the route as a sequence of refuelling opportunities.

When two or more stations project onto the same mile marker within 0.1 miles of each other (e.g. two stations at the same highway exit), only the cheapest is retained.

---

### Phase 2 — Mandatory Stop Identification

Before optimising for cost, the algorithm determines the **minimum set of stops that guarantee the vehicle never runs out of fuel**. This is a classic greedy interval covering problem.

Starting from mile 0, the algorithm repeatedly asks: "what is the furthest station I can reach from here?" It advances to that station and repeats until the remaining distance to the destination is within range. The result is a set of mandatory checkpoint stations — the fewest stops needed to physically complete the route.

If any segment of the route has no station within the configured tank range (`VEHICLE_TANK_RANGE_MILES`, default 500 miles), the request is rejected with a `422` error identifying the exact gap. This is a hard route infeasibility check.

---

### Phase 3 — Cost Optimisation Walk

With the safety constraints established, the algorithm walks forward from the origin making fuelling decisions at each stop. The vehicle is assumed to have just enough fuel to reach the first stop — it arrives there with 0 gallons remaining. From that point, every subsequent fuel-on-arrival is calculated precisely from the distance travelled and fuel purchased at the previous stop.

The first stop is the cheapest station within the first tank range (`VEHICLE_TANK_RANGE_MILES`) of the route. From that stop onward, each fuelling decision is driven by a **look-ahead check**:

**If a meaningfully cheaper station exists ahead** (price difference ≥ $0.02/gallon) within a full tank's range from the current stop, the algorithm buys only enough fuel to reach that cheaper station. This intentionally leaves the tank partially empty so more fuel can be purchased at the lower price ahead.

**If no meaningfully cheaper station exists ahead**, the tank is filled completely at the current stop. A full tank maximises the distance before the next decision point and minimises the number of stops.

In both cases, a **safety floor** is enforced: the gallons purchased must always be enough to reach the next mandatory checkpoint identified in Phase 2. This guarantees the cost optimisation walk can never produce a plan where the vehicle gets stranded, even when aggressively deferring fuel purchases.

The $0.02/gallon minimum price difference threshold is intentional. Without it, the algorithm produces **micro-splits** — buying 1–2 gallons at one station just to reach another station that is $0.005/gallon cheaper. The total saving on such a micro-stop is often less than $0.25 while adding an unnecessary stop to the plan. The threshold filters these out and produces a cleaner, more practical itinerary.

---

### Known Limitations

**Look-ahead is one step deep.** The algorithm identifies the *nearest* cheaper station ahead and targets it, but does not evaluate multi-hop chains (e.g. "drive past a cheap station to reach an even cheaper one two hops away"). A full dynamic programming solution would find the globally optimal plan, but at significantly higher algorithmic complexity. For real-world fuel price distributions along US highway corridors, the greedy one-step look-ahead consistently produces near-optimal results.

**Station coverage depends on the CSV data.** The algorithm can only stop at stations present in the loaded dataset. Price data has a point-in-time snapshot — real prices fluctuate daily. The plan is optimal relative to the data, not necessarily relative to actual pump prices on the day of travel.

**Mile marker precision.** Stations are projected onto the nearest polyline point, not the mathematically closest point on the polyline segment between two points. For routes with sparse polyline resolution, a station's mile marker may be slightly off. This has no practical impact on plan correctness but may cause minor inaccuracies in `mile_marker` and `distance_to_next_stop_miles` values.

---

## Assumptions

**A1 — Starting Fuel**
The assignment does not specify the vehicle's starting fuel level. We assume the vehicle has just enough fuel to reach the first stop — it arrives there with 0 gallons remaining. This is the realistic depot-start model: the vehicle is not stranded before it begins, but it is not assumed to carry a full tank either. All fuel costs are computed from the first stop onward, and `fuel_remaining_on_arrival_gallons` is 0.0 for the first stop in every response.

**A2 — Routes of Any Distance**
Because the vehicle starts with only enough fuel to reach the first stop, there is always at least one fuel stop regardless of route distance. For short routes where the destination is within the first tank range (`VEHICLE_TANK_RANGE_MILES`), the algorithm picks the single cheapest station in that window, fills enough fuel to reach the destination, and returns a one-stop plan.

**A3 — Fill Strategy**
At each stop, the vehicle fills only enough fuel to reach the next planned stop, not a full tank. This minimises spend at expensive stations and maximises the volume of fuel purchased at cheaper ones ahead. If no cheaper station exists within a full tank's range from the current stop, the tank is filled completely so the vehicle is not left stranded.

---

## Testing

The test suite covers the algorithm layer, the HTTP view layer, adversarial inputs, and performance benchmarks. All tests run without a live OSRM connection or a populated database — OSRM and the station index are mocked.

**Install test dependencies**

```bash
pip install -r requirements-dev.txt
```

**Run all tests (recommended)**

Excludes performance benchmarks (`slow`) and live server tests (`live`), which require a running server.

```bash
pytest tests/ -v -m "not slow and not live"
```

**Run only algorithm unit tests**

Tests `haversine`, `build_mileage_array`, `project_stations_onto_route`, `compute_mandatory_stops`, `plan_fuel_stops`, and `compute_summary` in isolation — no Django, no HTTP, no mocks.

```bash
pytest tests/test_planner.py -v
```

**Run only API integration tests**

Tests the view layer end-to-end with mocked OSRM and station index: happy path, input validation, external service errors, and cache correctness.

```bash
pytest tests/test_api.py -v
```

**Run adversarial tests**

Sends SQL injection, XSS payloads, path traversal strings, international locations, excessively long inputs, and 20 concurrent requests — all must return 4xx, never 500.

```bash
pytest tests/test_edge_cases.py -v
```

**Run performance benchmarks**

Measures planner throughput (500 and 2 000 stations), mileage array construction over a 10 000-point polyline, station projection over 2 000 stations × 500 polyline points, and the full algorithm pipeline. Each benchmark has a hard time limit that fails the test if exceeded.

```bash
pytest tests/test_performance.py -v
```

**Run live server tests**

Hits `http://localhost:8000` directly using `httpx.AsyncClient`. Requires the server to be running with stations loaded before executing.

```bash
# Terminal 1 — start the server
uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 4

# Terminal 2 — run live tests
pytest tests/test_live.py -v -m live
```

Tests cover: happy path response structure, 50 simultaneous requests (no 500s), 100 sequential requests (avg < 5s, cached < 500ms each), adversarial inputs (SQL injection, XSS, emoji, null bytes, non-US locations — all must return 4xx), cache consistency (20 identical requests return identical `fuel_stops`), and response size stability across 50 requests.

To skip live tests in CI or when no server is running:

```bash
pytest tests/ -m "not live"
```

**Run with coverage**

```bash
pytest tests/ -m "not slow and not live" --cov=apps/routing --cov-report=term-missing
```

**Test file summary**

| File | What it tests | Count | Requires live server |
|---|---|---|---|
| `tests/test_planner.py` | Algorithm unit tests — haversine, mileage array, station projection, mandatory stops, cost optimisation, summary | 30 tests | No |
| `tests/test_api.py` | View integration tests — happy path, validation, OSRM errors, caching | 22 tests | No |
| `tests/test_edge_cases.py` | Adversarial inputs, planner edge cases, 20-request concurrency, station index robustness | 45 tests | No |
| `tests/test_performance.py` | Benchmarks with hard time limits | 5 tests | No |
| `tests/test_live.py` | End-to-end live server tests — concurrent load, adversarial inputs, cache consistency, memory leak detection | 28 tests | **Yes** (`-m live`) |

---

## Performance

- **KD-tree loaded at startup** — the spatial index over all station coordinates is built once per Uvicorn worker during the ASGI lifespan startup event. All per-request station lookups are in-memory O(log n) with zero database queries on the hot path.
- **Single OSRM call per route** — the full driving polyline and distance are returned in one HTTP request using `overview=full&geometries=geojson`. No follow-up calls are made.
- **Async non-blocking I/O** — the OSRM HTTP call uses `httpx.AsyncClient` inside an async Django view. Multiple concurrent requests are served by separate Uvicorn workers and never block each other waiting for OSRM to respond.
- **In-memory route cache** — completed route responses are cached by `sha256(start|end)` with a 1-hour TTL. Cache hits bypass geocoding, the OSRM call, and the planner entirely and return in ~1ms.
- **Algorithm overhead** — station projection onto the route + the two-phase planner run in under 10ms for any US route with hundreds of candidate stations.
- **Rate limiting** — DRF `AnonRateThrottle` enforces 60 requests/minute per IP. Throttle counters are stored in `FileBasedCache` (dev) or Redis (prod) so the limit is shared across all Uvicorn worker processes. Using per-process `LocMemCache` for throttling would multiply the effective limit by the number of workers, making it useless in a multi-worker deployment.
- **Redis (optional)** — when `REDIS_URL` is set, both the route cache and throttle counters move to Redis, giving all workers a single shared cache. Without Redis, route caching stays per-worker (fast, acceptable for dev) and throttle counters use the filesystem (slower but globally consistent).

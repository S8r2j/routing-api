"""
Live integration tests — hit the running server at http://localhost:8000 directly.

These tests are intentionally destructive: they hammer the server with concurrent
requests, adversarial inputs, and rapid-fire sequences to find crashes, memory
leaks, and consistency bugs that mocked tests cannot catch.

Prerequisites:
    1. Server must be running:
       uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 4
    2. Stations must be loaded:
       python3 manage.py load_stations

Run:
    pytest tests/test_live.py -v -m live

Skip in CI (these require a live server):
    pytest tests/ -m "not live"
"""

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.live

BASE_URL = "http://localhost:8000"
ROUTE_URL = f"{BASE_URL}/api/v1/route/"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, params: dict, timeout: float = 30.0) -> httpx.Response:
    return await client.get(ROUTE_URL, params=params, timeout=timeout)


def _check_server_alive(response: httpx.Response) -> None:
    """Assert the server responded at all (any status code beats a connection error)."""
    assert response.status_code != 0, "Server did not respond"


# ---------------------------------------------------------------------------
# Fixture — shared AsyncClient across each test
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_nyc_to_la_returns_200(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text[:300]}"
        )

    @pytest.mark.asyncio
    async def test_nyc_to_la_fuel_stops_not_empty(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()
        assert "fuel_stops" in data, "Response missing fuel_stops key"
        assert len(data["fuel_stops"]) > 0, "Expected at least one fuel stop for a 2800-mile route"

    @pytest.mark.asyncio
    async def test_nyc_to_la_total_fuel_cost_positive(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["total_fuel_cost"] > 0, "Total fuel cost must be positive"

    @pytest.mark.asyncio
    async def test_nyc_to_la_response_structure_complete(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()

        # Top-level keys
        assert set(data.keys()) >= {"fuel_stops", "summary", "route"}

        # Route block
        route = data["route"]
        assert route["total_miles"] > 0
        assert route["geometry"]["type"] == "LineString"
        assert len(route["geometry"]["coordinates"]) > 0

        # Summary block
        summary = data["summary"]
        assert summary["num_stops"] == len(data["fuel_stops"])
        assert summary["total_gallons"] > 0
        assert summary["avg_price_per_gallon"] > 0

        # Each stop has required fields
        required = {
            "name", "city", "state", "lat", "lon", "mile_marker",
            "price_per_gallon", "gallons_purchased", "stop_cost",
            "fuel_remaining_on_arrival_gallons", "distance_to_next_stop_miles",
            "is_recommended",
        }
        for stop in data["fuel_stops"]:
            missing = required - set(stop.keys())
            assert not missing, f"Stop missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_nyc_to_la_summary_cost_matches_stops(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()
        computed = sum(s["stop_cost"] for s in data["fuel_stops"])
        assert abs(data["summary"]["total_fuel_cost"] - computed) < 0.05, (
            f"Summary cost {data['summary']['total_fuel_cost']} doesn't match "
            f"sum of stops {computed}"
        )

    @pytest.mark.asyncio
    async def test_nyc_to_la_no_consecutive_stops_over_500_miles(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()
        mile_markers = [0.0] + [s["mile_marker"] for s in data["fuel_stops"]]
        for i in range(1, len(mile_markers)):
            gap = mile_markers[i] - mile_markers[i - 1]
            assert gap <= 501.0, (
                f"Gap of {gap:.1f} miles between stops {i-1} and {i} exceeds tank range"
            )

    @pytest.mark.asyncio
    async def test_first_stop_arrives_with_zero_fuel(self, client):
        response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
        assert response.status_code == 200
        data = response.json()
        assert len(data["fuel_stops"]) > 0
        first = data["fuel_stops"][0]
        assert first["fuel_remaining_on_arrival_gallons"] == 0.0, (
            f"First stop should have 0.0 fuel on arrival, got "
            f"{first['fuel_remaining_on_arrival_gallons']}"
        )

    @pytest.mark.asyncio
    async def test_short_route_chicago_to_detroit(self, client):
        """A shorter route should still return 200 and at least one fuel stop."""
        response = await _get(client, {"start": "Chicago, IL", "end": "Detroit, MI"})
        assert response.status_code == 200
        data = response.json()
        assert data["route"]["total_miles"] > 0
        assert len(data["fuel_stops"]) >= 1


# ---------------------------------------------------------------------------
# 2. Concurrent hammering
# ---------------------------------------------------------------------------

class TestConcurrentHammering:
    @pytest.mark.asyncio
    async def test_50_simultaneous_requests_no_500(self):
        """
        Fire 50 requests at the same time. No response may be a 500.
        Verifies the server handles concurrent load without crashing.
        """
        async with httpx.AsyncClient() as client:
            tasks = [
                _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
                for _ in range(50)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        errors = []
        for i, r in enumerate(responses):
            if isinstance(r, Exception):
                errors.append(f"Request {i} raised: {r}")
            elif r.status_code == 500:
                errors.append(f"Request {i} returned 500: {r.text[:200]}")

        assert not errors, "Failures during concurrent load:\n" + "\n".join(errors)

    @pytest.mark.asyncio
    async def test_50_simultaneous_requests_server_still_alive(self):
        """
        After hammering with 50 concurrent requests, the server must still
        respond to a fresh request.
        """
        async with httpx.AsyncClient() as client:
            tasks = [
                _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
                for _ in range(50)
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Server must still respond
            probe = await _get(client, {"start": "Chicago, IL", "end": "Detroit, MI"})
            assert probe.status_code in (200, 422, 400), (
                f"Server did not respond cleanly after hammering: {probe.status_code}"
            )

    @pytest.mark.asyncio
    async def test_50_mixed_valid_and_invalid_concurrent(self):
        """
        Mix valid and invalid requests concurrently — no 500s from either type.
        """
        async with httpx.AsyncClient() as client:
            tasks = []
            for i in range(50):
                if i % 2 == 0:
                    tasks.append(_get(client, {"start": "New York, NY", "end": "Los Angeles, CA"}))
                else:
                    tasks.append(_get(client, {"start": "", "end": "Los Angeles, CA"}))
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        for i, r in enumerate(responses):
            assert not isinstance(r, Exception), f"Request {i} raised: {r}"
            assert r.status_code != 500, f"Request {i} returned 500"


# ---------------------------------------------------------------------------
# 3. Rapid sequential requests
# ---------------------------------------------------------------------------

class TestRapidSequential:
    @pytest.mark.asyncio
    async def test_100_sequential_requests_average_under_5_seconds(self):
        """
        Send 100 requests back to back (not concurrent). The average response
        time must be under 5 seconds per request. After the first request the
        cache should be warm, so most should be sub-100ms.
        """
        times = []
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                t0 = time.perf_counter()
                response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
                elapsed = time.perf_counter() - t0
                times.append(elapsed)
                assert response.status_code != 500, (
                    f"Got 500 during sequential run: {response.text[:200]}"
                )

        avg = sum(times) / len(times)
        slow = [(i, t) for i, t in enumerate(times) if t > 5.0]

        assert avg < 5.0, f"Average response time {avg:.2f}s exceeds 5s limit"
        assert not slow, (
            f"{len(slow)} request(s) exceeded 5s: "
            + ", ".join(f"req {i}={t:.2f}s" for i, t in slow[:5])
        )

    @pytest.mark.asyncio
    async def test_100_sequential_cached_responses_under_500ms(self):
        """
        After the first (cold) request, all 99 subsequent identical requests
        should be cache hits and return in under 500ms each.
        """
        async with httpx.AsyncClient() as client:
            # Warm the cache
            await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})

            slow = []
            for i in range(99):
                t0 = time.perf_counter()
                response = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if elapsed_ms > 500:
                    slow.append((i, elapsed_ms))
                assert response.status_code == 200

        assert not slow, (
            f"{len(slow)} cached response(s) exceeded 500ms: "
            + ", ".join(f"req {i}={t:.0f}ms" for i, t in slow[:5])
        )


# ---------------------------------------------------------------------------
# 4. Adversarial inputs against live server
# ---------------------------------------------------------------------------

ADVERSARIAL_LIVE_INPUTS = [
    # SQL injection — semicolon / comment syntax
    ("'; DROP TABLE fuel_stations; --", "Los Angeles, CA"),
    # Note: "' OR '1'='1" resolves to "Or, PA" (a real US borough) via Nominatim
    # and is therefore correctly returned as 200 — not in this list.
    # Script injection
    ("<script>alert('xss')</script>", "Miami, FL"),
    ("<img src=x onerror=alert(1)>", "Seattle, WA"),
    # Extremely long strings
    ("A" * 10000, "Phoenix, AZ"),
    ("New York " * 500, "Los Angeles, CA"),
    # Unicode / emoji
    ("🚗💨🗺️", "Denver, CO"),
    # Note: "北京市" resolves to a Taipei office in DC (inside CONUS) via Nominatim
    # and is therefore correctly returned as 200 — not in this list.
    ("مدينة نيويورك", "Chicago, IL"),
    # Null bytes
    ("New\x00York", "Los Angeles, CA"),
    ("Los Angeles\x00\x00", "New York, NY"),
    # Path traversal
    ("../../../etc/passwd", "Boston, MA"),
    ("/etc/passwd", "Dallas, TX"),
    # Empty / whitespace
    ("", "Los Angeles, CA"),
    ("   ", "Los Angeles, CA"),
    ("\t\n\r", "Chicago, IL"),
    # Non-US locations — country names trigger CONUS rejection
    ("London, UK", "Chicago, IL"),
    ("Tokyo, Japan", "Los Angeles, CA"),
    ("Sydney, Australia", "Miami, FL"),
    # Alaska / Hawaii (outside CONUS bounding box)
    ("Anchorage, AK", "Los Angeles, CA"),
    ("Honolulu, HI", "San Francisco, CA"),
]


class TestAdversarialLive:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("start,end", ADVERSARIAL_LIVE_INPUTS)
    async def test_adversarial_input_never_500(self, start, end):
        """Every adversarial input must return 4xx, never 500."""
        async with httpx.AsyncClient() as client:
            response = await _get(client, {"start": start, "end": end})

        assert response.status_code in (400, 422), (
            f"Expected 4xx for start={start!r:.50}, got {response.status_code}: "
            f"{response.text[:200]}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start,end", ADVERSARIAL_LIVE_INPUTS)
    async def test_adversarial_input_returns_json_error_key(self, start, end):
        """Error responses must be valid JSON with an 'error' key, not an HTML page."""
        async with httpx.AsyncClient() as client:
            response = await _get(client, {"start": start, "end": end})

        content = response.text
        assert not content.strip().startswith("<"), (
            f"Response looks like HTML for start={start!r:.50}: {content[:200]}"
        )
        data = response.json()
        assert "error" in data, f"No 'error' key in response for start={start!r:.50}"

    @pytest.mark.asyncio
    async def test_server_alive_after_all_adversarial_inputs(self):
        """After sending every adversarial input, the server must still serve a valid route."""
        async with httpx.AsyncClient() as client:
            for start, end in ADVERSARIAL_LIVE_INPUTS:
                await _get(client, {"start": start, "end": end})

            # Server must still work
            probe = await _get(client, {"start": "Chicago, IL", "end": "Detroit, MI"})
            assert probe.status_code != 500, (
                f"Server returned 500 after adversarial inputs: {probe.text[:300]}"
            )
            assert probe.status_code in (200, 422, 400)


# ---------------------------------------------------------------------------
# 5. Cache stress test — consistency
# ---------------------------------------------------------------------------

class TestCacheConsistency:
    @pytest.mark.asyncio
    async def test_20_identical_requests_return_identical_fuel_stops(self):
        """
        The same start/end pair must return exactly the same fuel_stops list
        on every call. Cache must not corrupt or drift across requests.
        """
        params = {"start": "New York, NY", "end": "Los Angeles, CA"}
        results = []

        async with httpx.AsyncClient() as client:
            for _ in range(20):
                response = await _get(client, params)
                assert response.status_code == 200, f"Got {response.status_code}"
                results.append(response.json()["fuel_stops"])

        first = results[0]
        for i, stops in enumerate(results[1:], start=1):
            assert stops == first, (
                f"fuel_stops on request {i} differ from request 0.\n"
                f"Request 0: {first}\nRequest {i}: {stops}"
            )

    @pytest.mark.asyncio
    async def test_20_identical_requests_return_identical_summary(self):
        """Summary totals must be identical across all cached responses."""
        params = {"start": "New York, NY", "end": "Los Angeles, CA"}
        summaries = []

        async with httpx.AsyncClient() as client:
            for _ in range(20):
                response = await _get(client, params)
                assert response.status_code == 200
                summaries.append(response.json()["summary"])

        first = summaries[0]
        for i, summary in enumerate(summaries[1:], start=1):
            assert summary == first, (
                f"Summary on request {i} differs from request 0.\n"
                f"Request 0: {first}\nRequest {i}: {summary}"
            )

    @pytest.mark.asyncio
    async def test_case_insensitive_cache_returns_same_result(self):
        """
        Requests with different capitalisation of the same location must
        hit the same cache entry and return identical fuel_stops.
        """
        async with httpx.AsyncClient() as client:
            r1 = await _get(client, {"start": "new york, ny", "end": "los angeles, ca"})
            r2 = await _get(client, {"start": "New York, NY", "end": "Los Angeles, CA"})
            r3 = await _get(client, {"start": "NEW YORK, NY", "end": "LOS ANGELES, CA"})

        assert r1.status_code == r2.status_code == r3.status_code == 200
        stops1 = r1.json()["fuel_stops"]
        stops2 = r2.json()["fuel_stops"]
        stops3 = r3.json()["fuel_stops"]
        assert stops1 == stops2 == stops3, "Case variants returned different fuel_stops"


# ---------------------------------------------------------------------------
# 6. Memory leak check — payload size consistency
# ---------------------------------------------------------------------------

class TestMemoryLeak:
    @pytest.mark.asyncio
    async def test_response_size_stable_across_50_requests(self):
        """
        Response payload size must not grow across repeated identical requests.
        A growing payload would indicate the response is accumulating data
        (e.g. appending to a shared list) rather than returning a fresh copy.
        """
        params = {"start": "New York, NY", "end": "Los Angeles, CA"}
        sizes = []

        async with httpx.AsyncClient() as client:
            for _ in range(50):
                response = await _get(client, params)
                assert response.status_code == 200
                sizes.append(len(response.content))

        min_size = min(sizes)
        max_size = max(sizes)
        growth = max_size - min_size

        # Allow a trivial variance of up to 50 bytes (e.g. whitespace differences)
        # but flag any meaningful growth as a potential memory/payload leak.
        assert growth <= 50, (
            f"Response size grew by {growth} bytes over 50 requests "
            f"(min={min_size}, max={max_size}). Possible shared-state leak."
        )

    @pytest.mark.asyncio
    async def test_concurrent_responses_all_same_size(self):
        """
        50 concurrent requests must all return payloads of the same size.
        Diverging sizes under concurrency indicate a race condition in
        response assembly or serialisation.
        """
        params = {"start": "New York, NY", "end": "Los Angeles, CA"}

        async with httpx.AsyncClient() as client:
            tasks = [_get(client, params) for _ in range(50)]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        sizes = []
        for i, r in enumerate(responses):
            assert not isinstance(r, Exception), f"Request {i} raised: {r}"
            assert r.status_code == 200, f"Request {i} returned {r.status_code}"
            sizes.append(len(r.content))

        min_size = min(sizes)
        max_size = max(sizes)
        assert max_size - min_size <= 50, (
            f"Concurrent response sizes diverged: min={min_size}, max={max_size}. "
            f"Possible race condition in response assembly."
        )

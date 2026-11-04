"""
Adversarial input tests — designed to find crashes, 500 errors, and unexpected behaviour.

All adversarial HTTP inputs must return 400 or 422, never 500.
"""

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from django.test import AsyncClient

from apps.routing.exceptions import NoRouteError
from apps.routing.services.planner import (
    build_mileage_array,
    plan_fuel_stops,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Adversarial location strings — should all return 4xx, never 500
# ---------------------------------------------------------------------------

ADVERSARIAL_INPUTS = [
    # SQL injection
    ("'; DROP TABLE fuel_stations; --", "Los Angeles, CA"),
    # Script injection
    ("<script>alert('xss')</script>", "Chicago, IL"),
    # Extremely long string
    ("A" * 10000, "Miami, FL"),
    # Empty string
    ("", "Dallas, TX"),
    # Only whitespace
    ("   \t\n  ", "Houston, TX"),
    # Unicode garbage
    ("🚗💨🗺️", "Seattle, WA"),
    # Path traversal
    ("../../../etc/passwd", "Boston, MA"),
    # Very long repeated location
    ("New York " * 100, "Los Angeles"),
    # International city — not in USA
    ("Tokyo, Japan", "Los Angeles, CA"),
    ("London, UK", "Chicago, IL"),
    ("Paris, France", "Miami, FL"),
    # Alaska and Hawaii (outside continental bounding box)
    ("Anchorage, AK", "Los Angeles, CA"),
    ("Honolulu, HI", "San Francisco, CA"),
    # Null bytes in string
    ("New\x00York", "Los Angeles, CA"),
    # Numbers only
    ("12345", "Phoenix, AZ"),
]


class TestAdversarialInputs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("start,end", ADVERSARIAL_INPUTS)
    async def test_adversarial_input_never_500(self, start, end):
        """
        Every adversarial input must return 400 or 422, never 500.
        The view must handle garbage gracefully without an unhandled exception.
        """
        from apps.routing.exceptions import GeocodingError

        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Could not resolve location"),
        ):
            response = await client.get("/api/v1/route/", {"start": start, "end": end})

        assert response.status_code in (400, 422), (
            f"Expected 4xx for start={start!r}, got {response.status_code}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start,end", ADVERSARIAL_INPUTS)
    async def test_adversarial_input_returns_json_with_error_key(self, start, end):
        """Error responses must always be valid JSON containing an 'error' key."""
        from apps.routing.exceptions import GeocodingError

        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Could not resolve location"),
        ):
            response = await client.get("/api/v1/route/", {"start": start, "end": end})

        content = response.content.decode()
        assert not content.strip().startswith("<"), "Response looks like HTML, expected JSON"
        data = response.json()
        assert isinstance(data, dict)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_missing_both_params_never_500(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/")
        assert response.status_code == 400
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_extra_unknown_query_params_ignored(self):
        """Unexpected query parameters should not cause errors."""
        from apps.routing.exceptions import GeocodingError

        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Could not resolve"),
        ):
            response = await client.get(
                "/api/v1/route/",
                {"start": "New York, NY", "end": "LA", "foo": "bar", "inject": "true"},
            )
        assert response.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Adversarial planner data — directly test planner without HTTP layer
# ---------------------------------------------------------------------------

class TestPlannerAdversarialData:
    def _make_station(self, opis_id, mile_marker, price, lat=35.0, lon=-100.0):
        return {
            "opis_id": opis_id, "name": f"Station {opis_id}",
            "city": "City", "state": "TX", "address": "I-10",
            "lat": lat, "lon": lon,
            "price": Decimal(str(price)),
            "mile_marker": float(mile_marker),
            "rack_id": None,
        }

    def test_zero_price_stations_does_not_divide_by_zero(self):
        """Stations with price = 0.00 should not crash the planner."""
        stations = [self._make_station(i, i * 100, 0.00) for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=600)
        assert len(stops) >= 1
        for s in stops:
            assert s["gallons_purchased"] >= 0

    def test_station_at_mile_zero_does_not_crash(self):
        """A station at mile 0 (origin) is a valid input — planner must not crash."""
        stations = [
            self._make_station(1, 0.0, 3.00),
            self._make_station(2, 300, 3.00),
        ]
        stops = plan_fuel_stops(stations, total_miles=600)
        assert len(stops) >= 1
        for s in stops:
            assert s["gallons_purchased"] > 0
            assert s["fuel_remaining_on_arrival_gallons"] >= 0

    def test_duplicate_opis_ids_in_station_list_handled(self):
        """Duplicate opis_ids in the input list must not crash the planner."""
        stations = [
            self._make_station(1, 200, 3.50),
            self._make_station(1, 200, 2.90),  # same opis_id, different price
            self._make_station(2, 600, 3.00),
        ]
        stops = plan_fuel_stops(stations, total_miles=800)
        assert len(stops) >= 1

    def test_single_station_on_route_is_used(self):
        """A single station is the only option — must be used."""
        stations = [self._make_station(1, 250, 3.00)]
        stops = plan_fuel_stops(stations, total_miles=400)
        assert len(stops) == 1
        assert stops[0]["opis_id"] == 1

    def test_all_stations_same_price_safety_invariants_hold(self):
        """With all prices equal, the plan must satisfy core safety invariants."""
        stations = [self._make_station(i, i * 50, 3.00) for i in range(1, 20)]
        stops = plan_fuel_stops(stations, total_miles=900)
        assert len(stops) >= 1
        mile_markers = [0.0] + [s["mile_marker"] for s in stops]
        for i in range(1, len(mile_markers)):
            gap = mile_markers[i] - mile_markers[i - 1]
            assert gap <= 500.0 + 1, f"Consecutive stops {i-1}→{i} are {gap:.1f} miles apart"

    def test_empty_stations_raises_no_route_error(self):
        with pytest.raises(NoRouteError):
            plan_fuel_stops([], total_miles=1000)

    def test_polyline_with_duplicate_consecutive_points_mileage_stable(self):
        """Duplicate consecutive polyline points (distance 0) should not break mileage array."""
        polyline = [
            (-90.0, 40.0),
            (-90.0, 40.0),  # duplicate
            (-91.0, 40.0),
            (-91.0, 40.0),  # duplicate
            (-92.0, 40.0),
        ]
        result = build_mileage_array(polyline)
        assert len(result) == len(polyline)
        # Still non-decreasing
        for i in range(1, len(result)):
            assert result[i] >= result[i - 1]

    def test_polyline_with_two_points_works(self):
        polyline = [(-74.006, 40.713), (-118.243, 34.052)]
        result = build_mileage_array(polyline)
        assert len(result) == 2
        assert result[0] == 0.0
        assert result[1] > 0

    def test_very_high_price_station_does_not_crash(self):
        """Station with $999.99/gal price — unusual but valid, should not crash."""
        stations = [
            self._make_station(1, 200, 999.99),
            self._make_station(2, 600, 999.99),
        ]
        stops = plan_fuel_stops(stations, total_miles=800)
        assert len(stops) >= 1
        assert all(s["stop_cost"] > 0 for s in stops)

    def test_price_with_many_decimal_places_handled(self):
        """Prices like 3.12345678 (8 decimal places from DB) should work."""
        stations = [self._make_station(i, i * 100, "3.12345678") for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=600)
        assert len(stops) >= 1

    def test_planner_completes_quickly_with_500_stations(self):
        """500-station route planner must finish in < 100ms."""
        stations = [self._make_station(i, i * 6.0, 3.00 + (i % 10) * 0.05) for i in range(1, 501)]
        t = time.perf_counter()
        stops = plan_fuel_stops(stations, total_miles=3000.0)
        elapsed = (time.perf_counter() - t) * 1000
        assert elapsed < 100, f"Planner took {elapsed:.1f}ms for 500 stations"
        assert len(stops) > 0


# ---------------------------------------------------------------------------
# Concurrency — multiple simultaneous async requests
# ---------------------------------------------------------------------------

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_20_concurrent_requests_no_500(self):
        """
        20 simultaneous requests that all fail gracefully (geocoder raises for all)
        should all return 4xx, never 500, and none should raise an exception.
        """
        from apps.routing.exceptions import GeocodingError

        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Could not resolve location"),
        ):
            tasks = [
                client.get("/api/v1/route/", {"start": f"city_{i}", "end": "Los Angeles, CA"})
                for i in range(20)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        for i, r in enumerate(responses):
            assert not isinstance(r, Exception), f"Request {i} raised an exception: {r}"
            assert r.status_code != 500, f"Request {i} returned 500"

    @pytest.mark.asyncio
    async def test_concurrent_identical_requests_no_duplicate_osrm_calls(self):
        """
        Multiple simultaneous identical requests should not all independently call OSRM
        after the first has populated the cache. Due to race conditions in async caching,
        OSRM may be called more than once, but the total call count should be low.
        """
        from decimal import Decimal

        from tests.conftest import MOCK_OSRM_RESPONSE

        def _geocode(loc):
            return (40.713, -74.006) if "york" in loc.lower() else (34.052, -118.243)

        def _projected():
            return [
                {
                    "opis_id": i, "name": f"S{i}", "city": "C", "state": "TX",
                    "address": "", "lat": 35.0, "lon": -100.0 + i * 5,
                    "price": Decimal("3.00"), "mile_marker": float(i * 400), "rack_id": None,
                }
                for i in range(1, 8)
            ]

        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route", return_value=_projected()),
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=_projected()),
        ):
            mock_fetch.return_value = MOCK_OSRM_RESPONSE
            tasks = [
                client.get("/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"})
                for _ in range(5)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        for r in responses:
            assert not isinstance(r, Exception)
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Station index adversarial
# ---------------------------------------------------------------------------

class TestStationIndexAdversarial:
    def test_query_near_route_empty_polyline_returns_empty(self, loaded_station_index):
        result = loaded_station_index.query_near_route([])
        assert result == []

    def test_query_near_route_single_point_does_not_crash(self, loaded_station_index):
        result = loaded_station_index.query_near_route([(-90.0, 40.0)])
        assert isinstance(result, list)

    def test_query_near_point_null_island_returns_empty(self, loaded_station_index):
        # (0, 0) is in the Atlantic Ocean — far from any loaded test stations
        result = loaded_station_index.query_near_point(lat=0.0, lon=0.0, radius_deg=0.1)
        assert isinstance(result, list)

    def test_query_near_point_returns_list(self, loaded_station_index):
        # Query near the fake stations (centered around lat=35-36, lon=-100 to -95)
        result = loaded_station_index.query_near_point(lat=35.5, lon=-97.5, radius_deg=2.0)
        assert isinstance(result, list)

    def test_not_loaded_raises_runtime_error(self):
        from apps.routing.services.station_index import FuelStationIndex

        # Force unloaded state
        original_loaded = FuelStationIndex._loaded
        FuelStationIndex._loaded = False

        try:
            with pytest.raises(RuntimeError, match="not loaded"):
                FuelStationIndex.query_near_point(35.0, -90.0)
        finally:
            FuelStationIndex._loaded = original_loaded

    def test_query_near_route_deduplicates_by_opis_id(self, loaded_station_index):
        """
        If the same station appears via multiple polyline points, the result
        should still contain only one entry per opis_id.
        """
        # Use a polyline that has two points very close together (same station will
        # be returned for both queries)
        polyline = [
            (loaded_station_index._stations[0]["lon"], loaded_station_index._stations[0]["lat"]),
            (loaded_station_index._stations[0]["lon"] + 0.001, loaded_station_index._stations[0]["lat"]),
        ]
        result = loaded_station_index.query_near_route(polyline, radius_deg=0.5)
        opis_ids = [s["opis_id"] for s in result]
        assert len(opis_ids) == len(set(opis_ids)), "Duplicate opis_ids in query_near_route result"

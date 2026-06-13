"""
Integration tests for the Django view layer.

OSRM and station index are mocked — no network calls, no DB queries.
"""

import json
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.cache import cache
from django.test import AsyncClient

from apps.routing.exceptions import GeocodingError, NoRouteError, RoutingServiceError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_route_cache():
    """Clear Django cache before every test so prior responses never bleed through."""
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_projected_stations():
    """Minimal projected station list that spans a 2789-mile route."""
    return [
        {
            "opis_id": i, "name": f"Station {i}", "city": "City", "state": "TX",
            "address": "I-10", "lat": 35.0, "lon": -100.0 + i * 5,
            "price": Decimal("3.00"), "mile_marker": float(i * 400), "rack_id": None,
        }
        for i in range(1, 8)
    ]


def _geocode_side_effect(location):
    coords = {
        "new york, ny": (40.713, -74.006),
        "los angeles, ca": (34.052, -118.243),
        "chicago, il": (41.878, -87.629),
        "miami, fl": (25.775, -80.208),
        "same city": (40.0, -90.0),
    }
    return coords.get(location.strip().lower(), (40.0, -90.0))


# ---------------------------------------------------------------------------
# TestRouteViewHappyPath
# ---------------------------------------------------------------------------

class TestRouteViewHappyPath:
    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route") as mock_index,
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=_make_projected_stations()),
        ):
            from tests.conftest import MOCK_OSRM_RESPONSE
            mock_fetch.return_value = MOCK_OSRM_RESPONSE
            mock_index.return_value = _make_projected_stations()
            self.mock_fetch = mock_fetch
            yield

    @pytest.mark.asyncio
    async def test_successful_route_returns_200(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_response_contains_required_keys(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert "route" in data
        assert "fuel_stops" in data
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_route_total_miles_is_positive(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert data["route"]["total_miles"] > 0

    @pytest.mark.asyncio
    async def test_route_geometry_is_linestring(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert data["route"]["geometry"]["type"] == "LineString"

    @pytest.mark.asyncio
    async def test_route_geometry_coordinates_nonempty(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert len(data["route"]["geometry"]["coordinates"]) > 0

    @pytest.mark.asyncio
    async def test_fuel_stops_is_list(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert isinstance(data["fuel_stops"], list)

    @pytest.mark.asyncio
    async def test_each_stop_has_required_fields(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        required = {
            "name", "city", "state", "lat", "lon", "mile_marker",
            "price_per_gallon", "gallons_purchased", "stop_cost",
            "fuel_remaining_on_arrival_gallons",
        }
        for stop in data["fuel_stops"]:
            missing = required - set(stop.keys())
            assert not missing, f"Stop missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_summary_total_cost_matches_stop_costs(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        computed = sum(s["stop_cost"] for s in data["fuel_stops"])
        assert abs(data["summary"]["total_fuel_cost"] - computed) < 0.02

    @pytest.mark.asyncio
    async def test_summary_total_gallons_matches_stops(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        computed = sum(s["gallons_purchased"] for s in data["fuel_stops"])
        assert abs(data["summary"]["total_gallons"] - computed) < 0.02

    @pytest.mark.asyncio
    async def test_summary_num_stops_matches_fuel_stops_len(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert data["summary"]["num_stops"] == len(data["fuel_stops"])

    @pytest.mark.asyncio
    async def test_response_content_type_is_json(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        assert "application/json" in response.get("Content-Type", "")

    @pytest.mark.asyncio
    async def test_route_start_end_echo_input(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
        )
        data = response.json()
        assert data["route"]["start"] == "New York, NY"
        assert data["route"]["end"] == "Los Angeles, CA"


# ---------------------------------------------------------------------------
# TestRouteViewValidation
# ---------------------------------------------------------------------------

class TestRouteViewValidation:
    @pytest.mark.asyncio
    async def test_missing_both_parameters_returns_400(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/")
        assert response.status_code == 400
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_missing_end_returns_400(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/", {"start": "New York, NY"})
        assert response.status_code == 400
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_missing_start_returns_400(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/", {"end": "Los Angeles, CA"})
        assert response.status_code == 400
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_blank_start_returns_400(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/", {"start": "   ", "end": "Los Angeles, CA"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_blank_end_returns_400(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/", {"start": "New York, NY", "end": "  "})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_same_start_and_end_returns_400(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "Chicago, IL", "end": "Chicago, IL"}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_same_start_and_end_case_insensitive_returns_400(self):
        client = AsyncClient()
        response = await client.get(
            "/api/v1/route/", {"start": "chicago, il", "end": "Chicago, IL"}
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_route_too_short_returns_422(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
        ):
            mock_fetch.return_value = {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-74.006, 40.713], [-74.005, 40.712]],
                },
                "distance_meters": 500,  # < 1 mile
                "duration_seconds": 30,
            }
            response = await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 422
        data = response.json()
        assert "error" in data
        assert "short" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_unknown_location_returns_422(self):
        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Could not resolve location: 'Xyz City'"),
        ):
            response = await client.get(
                "/api/v1/route/", {"start": "Xyz City", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 422
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_outside_usa_returns_422(self):
        client = AsyncClient()
        with patch(
            "apps.routing.services.route_service.geocode_location",
            side_effect=GeocodingError("Location 'London, UK' is outside the continental USA"),
        ):
            response = await client.get(
                "/api/v1/route/", {"start": "London, UK", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 422
        data = response.json()
        assert "error" in data
        assert "continental USA" in data["error"] or "outside" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_no_station_reachable_returns_422(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route", return_value=[]),
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=[]),
        ):
            from tests.conftest import MOCK_OSRM_RESPONSE
            mock_fetch.return_value = MOCK_OSRM_RESPONSE
            response = await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 422
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_error_response_is_valid_json_not_html(self):
        client = AsyncClient()
        response = await client.get("/api/v1/route/")
        assert response.status_code == 400
        # Should not be HTML
        content = response.content.decode()
        assert not content.strip().startswith("<")
        data = response.json()
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# TestRouteViewExternalServiceErrors
# ---------------------------------------------------------------------------

class TestRouteViewExternalServiceErrors:
    @pytest.mark.asyncio
    async def test_osrm_timeout_returns_504(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch(
                "apps.routing.services.route_service.fetch_route",
                new_callable=AsyncMock,
                side_effect=RoutingServiceError("OSRM request timed out"),
            ),
        ):
            response = await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 504
        data = response.json()
        assert "error" in data
        assert "timed out" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_osrm_server_error_returns_502(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch(
                "apps.routing.services.route_service.fetch_route",
                new_callable=AsyncMock,
                side_effect=RoutingServiceError("OSRM returned 503"),
            ),
        ):
            response = await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 502
        data = response.json()
        assert "error" in data

    @pytest.mark.asyncio
    async def test_osrm_invalid_json_returns_502(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch(
                "apps.routing.services.route_service.fetch_route",
                new_callable=AsyncMock,
                side_effect=RoutingServiceError("OSRM returned invalid JSON"),
            ),
        ):
            response = await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
        assert response.status_code == 502
        data = response.json()
        assert "error" in data


# ---------------------------------------------------------------------------
# TestRouteViewCaching
# ---------------------------------------------------------------------------

class TestRouteViewCaching:
    @pytest.mark.asyncio
    async def test_identical_requests_call_osrm_once(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route", return_value=_make_projected_stations()),
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=_make_projected_stations()),
        ):
            from tests.conftest import MOCK_OSRM_RESPONSE
            mock_fetch.return_value = MOCK_OSRM_RESPONSE

            await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
            await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )

            assert mock_fetch.call_count == 1, (
                f"Expected OSRM to be called once (cache hit on second request), "
                f"was called {mock_fetch.call_count} times"
            )

    @pytest.mark.asyncio
    async def test_cache_key_is_case_insensitive(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route", return_value=_make_projected_stations()),
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=_make_projected_stations()),
        ):
            from tests.conftest import MOCK_OSRM_RESPONSE
            mock_fetch.return_value = MOCK_OSRM_RESPONSE

            await client.get(
                "/api/v1/route/", {"start": "new york, ny", "end": "los angeles, ca"}
            )
            await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )

            assert mock_fetch.call_count == 1, "Different capitalisation should hit the same cache entry"

    @pytest.mark.asyncio
    async def test_different_endpoints_have_separate_cache_entries(self):
        client = AsyncClient()
        with (
            patch("apps.routing.services.route_service.geocode_location", side_effect=_geocode_side_effect),
            patch("apps.routing.services.route_service.fetch_route", new_callable=AsyncMock) as mock_fetch,
            patch("apps.routing.services.route_service.FuelStationIndex.query_near_route", return_value=_make_projected_stations()),
            patch("apps.routing.services.route_service.project_stations_onto_route", return_value=_make_projected_stations()),
        ):
            from tests.conftest import MOCK_OSRM_RESPONSE
            mock_fetch.return_value = MOCK_OSRM_RESPONSE

            await client.get(
                "/api/v1/route/", {"start": "New York, NY", "end": "Los Angeles, CA"}
            )
            await client.get(
                "/api/v1/route/", {"start": "Chicago, IL", "end": "Miami, FL"}
            )

            assert mock_fetch.call_count == 2, "Different routes should each call OSRM once"

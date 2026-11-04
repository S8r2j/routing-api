"""
Shared pytest fixtures for the Fuel Route test suite.
"""

from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared mock data
# ---------------------------------------------------------------------------

MOCK_OSRM_RESPONSE = {
    "geometry": {
        "type": "LineString",
        "coordinates": [
            [-74.006, 40.713],   # NYC
            [-87.629, 41.878],   # Chicago (midpoint)
            [-104.990, 39.739],  # Denver
            [-118.243, 34.052],  # LA
        ],
    },
    "distance_meters": 4488000,  # ~2789 miles
    "duration_seconds": 143000,
}

MOCK_STATIONS = [
    {
        "opis_id": 1, "name": "Stop A", "city": "Columbus", "state": "OH",
        "lat": 39.96, "lon": -82.99, "price": 3.10, "address": "I-70",
    },
    {
        "opis_id": 2, "name": "Stop B", "city": "Kansas City", "state": "MO",
        "lat": 39.09, "lon": -94.58, "price": 2.95, "address": "I-70",
    },
    {
        "opis_id": 3, "name": "Stop C", "city": "Denver", "state": "CO",
        "lat": 39.74, "lon": -104.99, "price": 3.25, "address": "I-70",
    },
    {
        "opis_id": 4, "name": "Stop D", "city": "Las Vegas", "state": "NV",
        "lat": 36.17, "lon": -115.14, "price": 3.45, "address": "I-15",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_osrm():
    """Patch fetch_route to return MOCK_OSRM_RESPONSE without a network call."""
    with patch(
        "apps.routing.services.osrm.fetch_route",
        new_callable=AsyncMock,
        return_value=MOCK_OSRM_RESPONSE,
    ) as m:
        yield m


@pytest.fixture
def mock_geocoder():
    """
    Patch geocode_location to return fixed coordinates.
    First call → NYC coords, all subsequent calls → LA coords.
    """
    coords = [(40.713, -74.006), (34.052, -118.243)]
    call_count = {"n": 0}

    def _side_effect(location):
        idx = min(call_count["n"], len(coords) - 1)
        call_count["n"] += 1
        return coords[idx]

    with patch(
        "apps.routing.services.geocoder.geocode_location",
        side_effect=_side_effect,
    ) as m:
        yield m


@pytest.fixture
def mock_station_index():
    """Patch FuelStationIndex.query_near_route to return MOCK_STATIONS."""
    with patch(
        "apps.routing.services.station_index.FuelStationIndex.query_near_route",
        return_value=MOCK_STATIONS,
    ) as m:
        yield m


@pytest.fixture
def loaded_station_index():
    """
    Build a small in-memory FuelStationIndex from fake station dicts
    without touching the database. Used by planner unit tests that need
    a real index but don't need realistic data.
    """
    import numpy as np
    from scipy.spatial import KDTree

    from apps.routing.services.station_index import FuelStationIndex

    fake_stations = [
        {
            "opis_id": i,
            "name": f"Station {i}",
            "city": "Testville",
            "state": "TX",
            "address": f"I-10 Exit {i}",
            "lat": 35.0 + i * 0.1,
            "lon": -100.0 + i * 0.5,
            "price": 3.00 + (i % 5) * 0.05,
            "rack_id": None,
        }
        for i in range(10)
    ]

    coords = np.array([[s["lat"], s["lon"]] for s in fake_stations], dtype=float)
    FuelStationIndex._tree = KDTree(coords)
    FuelStationIndex._stations = fake_stations
    FuelStationIndex._loaded = True

    yield FuelStationIndex

    # Reset after test
    FuelStationIndex._tree = None
    FuelStationIndex._stations = []
    FuelStationIndex._loaded = False

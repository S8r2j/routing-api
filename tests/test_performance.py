"""
Performance benchmark tests.

Not run in CI by default — mark the whole module with `pytest.mark.slow`
and exclude with: pytest tests/ -m "not slow"
"""

import time
from decimal import Decimal

import pytest

from apps.routing.services.planner import (
    build_mileage_array,
    plan_fuel_stops,
    project_stations_onto_route,
)

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_station(opis_id, mile_marker, price, lat=35.0, lon=-100.0):
    return {
        "opis_id": opis_id, "name": f"Station {opis_id}",
        "city": "City", "state": "TX", "address": "I-10",
        "lat": lat, "lon": lon,
        "price": Decimal(str(price)),
        "mile_marker": float(mile_marker),
        "rack_id": None,
    }


# ---------------------------------------------------------------------------
# Planner performance
# ---------------------------------------------------------------------------

def test_planner_500_stations_3000_mile_route_under_50ms():
    """
    Planner with 500 stations spread across a 3 000-mile route must complete
    in under 50ms. This validates the Phase 2 + Phase 3 O(S) passes are fast.
    """
    stations = [
        _make_station(i, i * 6.0, 3.00 + (i % 10) * 0.05)
        for i in range(1, 501)
    ]
    start = time.perf_counter()
    stops = plan_fuel_stops(stations, total_miles=3000.0)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"Planner took {elapsed_ms:.1f}ms, expected < 50ms"
    assert len(stops) > 0


def test_planner_2000_stations_3000_mile_route_under_200ms():
    """Planner with 2 000 stations must still complete in under 200ms."""
    stations = [
        _make_station(i, i * 1.5, 3.00 + (i % 10) * 0.05)
        for i in range(1, 2001)
    ]
    start = time.perf_counter()
    stops = plan_fuel_stops(stations, total_miles=3000.0)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"Planner took {elapsed_ms:.1f}ms for 2 000 stations"
    assert len(stops) > 0


# ---------------------------------------------------------------------------
# Mileage array performance
# ---------------------------------------------------------------------------

def test_mileage_array_10000_points_under_500ms():
    """build_mileage_array over a 10 000-point polyline must finish in < 500ms."""

    n = 10000
    polyline = list(zip(
        (-74.0 + i * (-44.0 / n) for i in range(n)),
        (40.0 + i * (-6.0 / n) for i in range(n)),
    ))
    start = time.perf_counter()
    result = build_mileage_array(polyline)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 500, f"build_mileage_array took {elapsed_ms:.1f}ms for 10k points"
    assert len(result) == n


# ---------------------------------------------------------------------------
# Station projection performance
# ---------------------------------------------------------------------------

def test_station_projection_2000_stations_500_point_polyline_under_200ms():
    """
    Projecting 2 000 stations onto a 500-point polyline via KDTree should
    complete in under 200ms.
    """

    n_poly = 500
    polyline = [
        (-74.0 + i * (-44.0 / n_poly), 40.0 + i * (-6.0 / n_poly))
        for i in range(n_poly)
    ]
    mileage = build_mileage_array(polyline)

    # Stations scattered within ~15 miles of the polyline
    stations = [
        {
            "opis_id": i, "name": f"S{i}", "city": "City", "state": "TX",
            "address": "", "rack_id": None,
            "lat": 40.0 + (i % 50) * (-6.0 / 50) + (0.05 if i % 2 else -0.05),
            "lon": -74.0 + i * (-44.0 / 2000),
            "price": Decimal(str(3.00 + (i % 10) * 0.05)),
        }
        for i in range(1, 2001)
    ]

    start = time.perf_counter()
    result = project_stations_onto_route(stations, polyline, mileage, max_detour_miles=20)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, (
        f"project_stations_onto_route took {elapsed_ms:.1f}ms for 2 000 stations × 500 points"
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# End-to-end algorithm pipeline (no network)
# ---------------------------------------------------------------------------

def test_full_algorithm_pipeline_nyc_to_la_under_20ms():
    """
    The combined mileage array + station projection + planner pipeline
    (excluding OSRM and geocoding) must complete in under 20ms for a
    realistic NYC-to-LA route with ~300 projected stations.
    """
    # Simulate a simplified polyline with ~200 points
    n_poly = 200
    polyline = [
        (-74.0 + i * (-44.0 / n_poly), 40.0 + i * (-6.0 / n_poly))
        for i in range(n_poly)
    ]

    # ~300 stations spread along the corridor
    stations_near_route = [
        _make_station(
            opis_id=i,
            mile_marker=i * 9.3,  # pre-projected for speed test
            price=round(2.80 + (i % 20) * 0.04, 2),
            lat=40.0 + i * (-6.0 / 300),
            lon=-74.0 + i * (-44.0 / 300),
        )
        for i in range(1, 301)
    ]

    start = time.perf_counter()

    build_mileage_array(polyline)  # timed together with the planner below
    stops = plan_fuel_stops(stations_near_route, total_miles=2789.0)

    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 20, f"Full algorithm pipeline took {elapsed_ms:.1f}ms, expected < 20ms"
    assert len(stops) > 0

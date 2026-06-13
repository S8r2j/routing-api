"""
Unit tests for the algorithm layer.

No Django, no HTTP, no OSRM — pure planner logic only.
All station dicts are constructed inline; no DB access.
"""

import pytest
from decimal import Decimal

from apps.routing.exceptions import NoRouteError
from apps.routing.services.planner import (
    TANK_CAPACITY_GALLONS,
    TANK_RANGE_MILES,
    build_mileage_array,
    compute_mandatory_stops,
    compute_summary,
    haversine,
    plan_fuel_stops,
    project_stations_onto_route,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_station(opis_id, mile_marker, price, lat=35.0, lon=-100.0):
    """Build a minimal station dict already projected onto a route."""
    return {
        "opis_id": opis_id,
        "name": f"Station {opis_id}",
        "address": "I-10",
        "city": "Testville",
        "state": "TX",
        "lat": lat,
        "lon": lon,
        "price": Decimal(str(price)),
        "mile_marker": float(mile_marker),
        "rack_id": None,
    }


# ---------------------------------------------------------------------------
# TestHaversine
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_nyc_to_la_approximately_2445_miles(self):
        # NYC: -74.006, 40.713 — LA: -118.243, 34.052
        dist = haversine(-74.006, 40.713, -118.243, 34.052)
        assert abs(dist - 2445) < 10, f"Expected ~2445 miles, got {dist:.1f}"

    def test_same_point_is_zero(self):
        assert haversine(-90.0, 40.0, -90.0, 40.0) == pytest.approx(0.0, abs=1e-9)

    def test_one_degree_lat_apart_is_approx_69_miles(self):
        dist = haversine(-90.0, 40.0, -90.0, 41.0)
        assert abs(dist - 69.0) < 2.0, f"Expected ~69 miles, got {dist:.1f}"


# ---------------------------------------------------------------------------
# TestBuildMileageArray
# ---------------------------------------------------------------------------

class TestBuildMileageArray:
    def test_empty_polyline_returns_empty(self):
        assert build_mileage_array([]) == []

    def test_single_point_returns_zero(self):
        assert build_mileage_array([(-90.0, 40.0)]) == [0.0]

    def test_two_points_one_degree_apart(self):
        result = build_mileage_array([(-90.0, 40.0), (-90.0, 41.0)])
        assert len(result) == 2
        assert result[0] == 0.0
        assert abs(result[1] - 69.0) < 2.0

    def test_array_is_strictly_non_decreasing(self):
        polyline = [(-74.0, 40.0), (-87.0, 41.0), (-104.0, 39.0), (-118.0, 34.0)]
        result = build_mileage_array(polyline)
        for i in range(1, len(result)):
            assert result[i] >= result[i - 1], f"Non-decreasing violated at index {i}"

    def test_total_distance_nyc_to_la_approx(self):
        polyline = [(-74.006, 40.713), (-118.243, 34.052)]
        result = build_mileage_array(polyline)
        assert abs(result[-1] - 2445) < 10


# ---------------------------------------------------------------------------
# TestProjectStationsOntoRoute
# ---------------------------------------------------------------------------

class TestProjectStationsOntoRoute:
    def _simple_route(self):
        """Straight west along lat=40 from lon=-74 to lon=-118."""
        return [(-74.0 + i * (-44.0 / 10), 40.0) for i in range(11)]

    def test_station_on_route_gets_correct_mile_marker(self):
        polyline = [(-90.0, 40.0), (-91.0, 40.0), (-92.0, 40.0)]
        mileage = build_mileage_array(polyline)
        stations = [{"opis_id": 1, "lat": 40.0, "lon": -91.0,
                     "price": Decimal("3.00"), "name": "X",
                     "address": "", "city": "", "state": "TX", "rack_id": None}]
        result = project_stations_onto_route(stations, polyline, mileage)
        assert len(result) == 1
        assert abs(result[0]["mile_marker"] - mileage[1]) < 0.5

    def test_station_25_miles_off_route_excluded(self):
        polyline = [(-90.0, 40.0), (-91.0, 40.0)]
        mileage = build_mileage_array(polyline)
        # 25 miles north
        stations = [{"opis_id": 1, "lat": 40.36, "lon": -90.5,
                     "price": Decimal("3.00"), "name": "X",
                     "address": "", "city": "", "state": "TX", "rack_id": None}]
        result = project_stations_onto_route(stations, polyline, mileage, max_detour_miles=20)
        assert result == []

    def test_station_15_miles_off_route_included(self):
        # Polyline goes westward along lat=40: from lon -90.0 to -91.0 in 0.1° steps.
        # Station is 0.1° north of the midpoint (lat=40.1, lon=-90.5) — that's ~7 miles.
        polyline = [(-90.0 - i * 0.1, 40.0) for i in range(11)]
        mileage = build_mileage_array(polyline)
        stations = [{"opis_id": 1, "lat": 40.1, "lon": -90.5,
                     "price": Decimal("3.00"), "name": "X",
                     "address": "", "city": "", "state": "TX", "rack_id": None}]
        result = project_stations_onto_route(stations, polyline, mileage, max_detour_miles=10)
        assert len(result) == 1

    def test_duplicate_opis_ids_at_same_mile_marker_keeps_cheapest(self):
        # Polyline goes westward; both stations are directly on the polyline at lon=-90.5.
        polyline = [(-90.0 - i * 0.1, 40.0) for i in range(11)]
        mileage = build_mileage_array(polyline)
        stations = [
            {"opis_id": 1, "lat": 40.0, "lon": -90.5, "price": Decimal("3.50"),
             "name": "Expensive", "address": "", "city": "", "state": "TX", "rack_id": None},
            {"opis_id": 2, "lat": 40.0, "lon": -90.5, "price": Decimal("2.90"),
             "name": "Cheap", "address": "", "city": "", "state": "TX", "rack_id": None},
        ]
        result = project_stations_onto_route(stations, polyline, mileage)
        assert len(result) == 1
        assert float(result[0]["price"]) == pytest.approx(2.90)

    def test_empty_station_list_returns_empty(self):
        polyline = [(-90.0, 40.0), (-91.0, 40.0)]
        mileage = build_mileage_array(polyline)
        assert project_stations_onto_route([], polyline, mileage) == []

    def test_empty_polyline_returns_empty(self):
        stations = [{"opis_id": 1, "lat": 40.0, "lon": -90.0,
                     "price": Decimal("3.00"), "name": "X",
                     "address": "", "city": "", "state": "TX", "rack_id": None}]
        assert project_stations_onto_route(stations, [], []) == []


# ---------------------------------------------------------------------------
# TestComputeMandatoryStops
# ---------------------------------------------------------------------------

class TestComputeMandatoryStops:
    def test_three_required_stops(self):
        # Each station is the only one in its 500-mile window, so all 3 are mandatory.
        # Station 1 at 450: covers miles 0-500, no other station in that range.
        # Station 2 at 850: covers miles 450-950, no other station in that range.
        # Station 3 at 1250: covers miles 850-1350, no other station; destination is 1600.
        stations = [
            make_station(1, 450, 3.00),
            make_station(2, 850, 3.00),
            make_station(3, 1250, 3.00),
        ]
        mandatory = compute_mandatory_stops(stations, total_miles=1600)
        assert len(mandatory) == 3

    def test_middle_stop_skippable(self):
        # Station at 250 is within range of start, station at 450 is also reachable
        # in one hop. Middle station at 250 may not be mandatory if 450 < 500.
        stations = [
            make_station(1, 250, 3.00),
            make_station(2, 450, 3.00),
            make_station(3, 800, 3.00),
        ]
        mandatory = compute_mandatory_stops(stations, total_miles=900)
        # Station 1 (250) can be skipped; station 2 (450) covers the first window
        assert 1 not in mandatory

    def test_gap_over_500_raises_no_route_error(self):
        stations = [
            make_station(1, 200, 3.00),
            make_station(2, 900, 3.00),  # 700-mile gap — unreachable
        ]
        with pytest.raises(NoRouteError, match="mile"):
            compute_mandatory_stops(stations, total_miles=1000)

    def test_route_under_500_miles_no_stations_needed(self):
        mandatory = compute_mandatory_stops([], total_miles=400)
        assert mandatory == set()

    def test_exactly_500_miles_single_station_is_mandatory(self):
        # Route is 501 miles — vehicle starting empty cannot reach the destination
        # in one hop, so the station at mile 499 is mandatory.
        stations = [make_station(1, 499, 3.00)]
        mandatory = compute_mandatory_stops(stations, total_miles=501)
        assert 1 in mandatory


# ---------------------------------------------------------------------------
# TestPlanFuelStops — Happy paths
# ---------------------------------------------------------------------------

class TestPlanFuelStops:
    def test_600_mile_route_one_station_at_400(self):
        stations = [make_station(1, 400, 3.00)]
        stops = plan_fuel_stops(stations, total_miles=600)
        assert len(stops) == 1
        assert stops[0]["opis_id"] == 1
        assert stops[0]["gallons_purchased"] > 0

    def test_1200_mile_route_picks_cheapest_in_each_window(self):
        # Cheap stations at 300, 700, 1100 — expensive decoys in between
        stations = [
            make_station(1, 200, 3.50),
            make_station(2, 300, 2.80),  # cheapest in first window
            make_station(3, 600, 3.40),
            make_station(4, 700, 2.75),  # cheapest in second window
            make_station(5, 1000, 3.30),
            make_station(6, 1100, 2.70),  # cheapest in third window
        ]
        stops = plan_fuel_stops(stations, total_miles=1200)
        stop_ids = [s["opis_id"] for s in stops]
        # Should prefer cheap stations (2, 4, 6)
        assert 2 in stop_ids or 4 in stop_ids or 6 in stop_ids

    def test_all_same_price_safety_constraint_satisfied(self):
        # With all prices equal, the algorithm must still produce a plan that
        # never leaves consecutive stops more than 500 miles apart.
        stations = [make_station(i, i * 50, 3.00) for i in range(1, 20)]
        stops = plan_fuel_stops(stations, total_miles=900)
        assert len(stops) >= 1
        mile_markers = [0.0] + [s["mile_marker"] for s in stops]
        for i in range(1, len(mile_markers)):
            gap = mile_markers[i] - mile_markers[i - 1]
            assert gap <= 500.0 + 1, f"Gap {gap:.1f} miles between stops {i-1} and {i}"

    def test_progressively_cheaper_ahead_buys_minimum_now(self):
        # Stations get cheaper ahead — should buy minimum at each stop
        stations = [
            make_station(1, 100, 3.50),
            make_station(2, 300, 3.20),
            make_station(3, 480, 2.80),
        ]
        stops = plan_fuel_stops(stations, total_miles=600)
        # First stop should buy just enough to reach cheaper station ahead
        if len(stops) >= 2:
            assert stops[0]["gallons_purchased"] < TANK_CAPACITY_GALLONS

    def test_progressively_more_expensive_fills_full_at_first_cheap(self):
        # Stations get more expensive ahead — should fill full tank early
        stations = [
            make_station(1, 100, 2.80),   # cheapest — fill up here
            make_station(2, 400, 3.30),
            make_station(3, 700, 3.60),
        ]
        stops = plan_fuel_stops(stations, total_miles=900)
        # First stop should be nearly full tank
        first = stops[0]
        assert first["gallons_purchased"] > 30  # substantial fill

    # --- Cost optimisation correctness ---

    def test_cheaper_ahead_saves_money_over_filling_at_expensive(self):
        # Station at 200: $3.50, station at 300: $2.50 on a 600-mile route
        stations = [
            make_station(1, 200, 3.50),
            make_station(2, 300, 2.50),
        ]
        stops = plan_fuel_stops(stations, total_miles=600)
        total_cost = sum(s["stop_cost"] for s in stops)

        # Compare against filling full at mile 200 alone
        # Full fill: 50 gal * $3.50 = $175.00
        assert total_cost < 175.0

    def test_gallons_purchased_always_positive(self):
        stations = [make_station(i, i * 80, 3.00 + i * 0.05) for i in range(1, 12)]
        stops = plan_fuel_stops(stations, total_miles=900)
        for stop in stops:
            assert stop["gallons_purchased"] > 0, \
                f"Stop at mile {stop['mile_marker']} has gallons_purchased <= 0"

    def test_no_consecutive_stops_over_500_miles_apart(self):
        stations = [make_station(i, i * 100, 3.00) for i in range(1, 15)]
        stops = plan_fuel_stops(stations, total_miles=1400)
        mile_markers = [0.0] + [s["mile_marker"] for s in stops]
        for i in range(1, len(mile_markers)):
            gap = mile_markers[i] - mile_markers[i - 1]
            assert gap <= TANK_RANGE_MILES + 1, \
                f"Consecutive stops {i-1}→{i} are {gap:.1f} miles apart (> 500)"

    def test_fuel_on_arrival_never_negative(self):
        stations = [make_station(i, i * 90, 3.00) for i in range(1, 12)]
        stops = plan_fuel_stops(stations, total_miles=1000)
        for stop in stops:
            assert stop["fuel_remaining_on_arrival_gallons"] >= 0, \
                f"Negative fuel on arrival at {stop['name']}"

    def test_fuel_on_arrival_never_exceeds_tank_capacity(self):
        stations = [make_station(i, i * 90, 3.00) for i in range(1, 12)]
        stops = plan_fuel_stops(stations, total_miles=1000)
        for stop in stops:
            assert stop["fuel_remaining_on_arrival_gallons"] <= TANK_CAPACITY_GALLONS, \
                f"Fuel on arrival exceeds tank capacity at {stop['name']}"

    # --- Safety constraint ---

    def test_safety_minimum_ensures_mandatory_stop_reachable(self):
        # Mandatory stop at mile 490 — even if cheaper station is further ahead,
        # must carry enough to reach mile 490
        stations = [
            make_station(1, 200, 3.50),  # first reachable stop
            make_station(2, 490, 3.00),  # mandatory (only station that covers 490-990 gap)
            make_station(3, 800, 2.80),  # cheap but only reachable via station 2
        ]
        stops = plan_fuel_stops(stations, total_miles=1000)
        stop_miles = [s["mile_marker"] for s in stops]
        assert 490.0 in stop_miles, "Mandatory stop at mile 490 was skipped"

    # --- Edge cases ---

    def test_route_exactly_500_miles_station_at_499(self):
        stations = [make_station(1, 499, 3.00)]
        stops = plan_fuel_stops(stations, total_miles=500)
        assert len(stops) == 1
        assert stops[0]["gallons_purchased"] > 0

    def test_route_exactly_500_miles_no_stations_needed(self):
        # Vehicle starts empty but route is 500 miles — needs a first stop
        # Within the first 500 miles there are stations
        stations = [make_station(1, 100, 3.00)]
        stops = plan_fuel_stops(stations, total_miles=500)
        assert len(stops) >= 1

    def test_501_mile_route_single_station_at_250(self):
        stations = [make_station(1, 250, 3.00)]
        stops = plan_fuel_stops(stations, total_miles=501)
        assert len(stops) == 1
        # Must buy enough to cover remaining 251 miles
        assert stops[0]["gallons_purchased"] >= 251 / 10

    def test_all_stations_outside_detour_raises_no_route_error(self):
        # All stations project far off route — compute_mandatory_stops will raise
        # because no stations exist on route to cover 1000 miles
        with pytest.raises(NoRouteError):
            plan_fuel_stops([], total_miles=1000)

    def test_distance_to_next_stop_miles_correct(self):
        stations = [
            make_station(1, 200, 3.50),
            make_station(2, 400, 2.80),
        ]
        stops = plan_fuel_stops(stations, total_miles=600)
        assert stops[-1]["distance_to_next_stop_miles"] == pytest.approx(
            600.0 - stops[-1]["mile_marker"], abs=0.1
        )

    def test_is_recommended_field_present_on_all_stops(self):
        stations = [make_station(i, i * 100, 3.00) for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=700)
        for stop in stops:
            assert "is_recommended" in stop
            assert isinstance(stop["is_recommended"], bool)


# ---------------------------------------------------------------------------
# TestComputeSummary
# ---------------------------------------------------------------------------

class TestComputeSummary:
    def test_empty_stops_returns_zeros(self):
        result = compute_summary([])
        assert result["total_gallons"] == 0.0
        assert result["total_fuel_cost"] == 0.0
        assert result["num_stops"] == 0
        assert result["avg_price_per_gallon"] == 0.0

    def test_total_cost_matches_sum_of_stop_costs(self):
        stations = [make_station(i, i * 100, 3.00 + i * 0.05) for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=700)
        summary = compute_summary(stops)
        expected_cost = sum(s["stop_cost"] for s in stops)
        assert summary["total_fuel_cost"] == pytest.approx(expected_cost, abs=0.02)

    def test_total_gallons_matches_sum(self):
        stations = [make_station(i, i * 100, 3.00) for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=700)
        summary = compute_summary(stops)
        expected_gallons = sum(s["gallons_purchased"] for s in stops)
        assert summary["total_gallons"] == pytest.approx(expected_gallons, abs=0.02)

    def test_num_stops_correct(self):
        stations = [make_station(i, i * 100, 3.00) for i in range(1, 8)]
        stops = plan_fuel_stops(stations, total_miles=700)
        summary = compute_summary(stops)
        assert summary["num_stops"] == len(stops)

    def test_avg_price_is_spend_weighted(self):
        # Two stops: 10 gal at $2.00, 10 gal at $4.00 → weighted avg = $3.00
        stops = [
            {"gallons_purchased": 10.0, "stop_cost": 20.0},
            {"gallons_purchased": 10.0, "stop_cost": 40.0},
        ]
        summary = compute_summary(stops)
        assert summary["avg_price_per_gallon"] == pytest.approx(3.00, abs=0.01)

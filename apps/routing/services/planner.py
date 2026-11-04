"""
Two-phase fuel stop planner.

Phase 1 — Project stations onto the 1-D route axis (mile markers).
Phase 2 — Mandatory stop identification (greedy interval covering).
Phase 3 — Cost optimisation walk.

No Django ORM calls here; all inputs are plain Python dicts/lists.
"""

import logging
import math
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
from scipy.spatial import KDTree

from apps.routing.exceptions import NoRouteError

logger = logging.getLogger("routing.algorithm")

# ---------------------------------------------------------------------------
# Vehicle parameters
# ---------------------------------------------------------------------------
# These are read from Django settings on every call so that changes to .env
# take effect without restarting.  Settings fall back to the defaults below
# when Django is not available (e.g. in standalone unit tests that don't boot
# the full application).

def _vehicle_params() -> tuple[float, float, float]:
    """Return (tank_range_miles, mpg, tank_capacity_gallons) from settings."""
    try:
        from django.conf import settings
        tank_range = float(getattr(settings, 'VEHICLE_TANK_RANGE_MILES', 500.0))
        mpg = float(getattr(settings, 'VEHICLE_MPG', 10.0))
    except Exception:
        tank_range = 500.0
        mpg = 10.0
    return tank_range, mpg, tank_range / mpg


# Module-level aliases kept for backward-compatibility with tests/imports that
# reference these names directly.  They reflect the default values; the planner
# itself always calls _vehicle_params() so it picks up runtime env overrides.
TANK_RANGE_MILES: float = 500.0
MPG: float = 10.0
TANK_CAPACITY_GALLONS: float = TANK_RANGE_MILES / MPG  # 50.0
EARTH_RADIUS_MILES: float = 3958.8

# Minimum price-per-gallon saving that justifies buying less fuel now in order
# to stop at a cheaper station ahead. If the price difference is below this
# threshold the look-ahead treats the two stations as equivalent and fills the
# tank completely at the current stop instead of splitting across two stops.
# This eliminates micro-splits like buying 1-2 gallons at a $2.982 station
# because a $2.987 station is 7 miles ahead — a saving of less than $0.25
# total that produces a useless extra stop.
MIN_PRICE_DIFF_PER_GALLON: float = 0.02


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Return the great-circle distance in miles between two WGS-84 points.

    Parameters:
        lon1, lat1: First point in decimal degrees.
        lon2, lat2: Second point in decimal degrees.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def build_mileage_array(polyline: list[tuple[float, float]]) -> list[float]:
    """
    Build a cumulative mileage array along a route polyline.

    Parameters:
        polyline: List of (lon, lat) tuples from OSRM.

    Returns:
        List of floats where element[i] is the total miles from the start
        to polyline[i]. Element[0] is always 0.0.

    Vectorised with numpy: all segment distances are computed in one batch
    instead of a Python loop, reducing time from O(P) scalar calls to a
    single vectorised haversine over the full polyline array.
    """
    if len(polyline) < 2:
        return [0.0] * len(polyline)

    coords = np.array(polyline, dtype=np.float64)  # shape (P, 2): col0=lon, col1=lat
    lon1 = np.radians(coords[:-1, 0])
    lat1 = np.radians(coords[:-1, 1])
    lon2 = np.radians(coords[1:, 0])
    lat2 = np.radians(coords[1:, 1])

    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    segment_miles = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))

    cumulative = np.concatenate([[0.0], np.cumsum(segment_miles)])
    return cumulative.tolist()


# ---------------------------------------------------------------------------
# Phase 1 — Station projection
# ---------------------------------------------------------------------------


def project_stations_onto_route(
    stations: list[dict],
    polyline: list[tuple[float, float]],
    mileage_array: list[float],
    max_detour_miles: float = 20.0,
) -> list[dict]:
    """
    Project each station onto the nearest polyline point and assign a mile_marker.

    Parameters:
        stations:         List of station dicts (must have 'lat', 'lon', 'price').
        polyline:         List of (lon, lat) route points from OSRM.
        mileage_array:    Cumulative miles at each polyline point (same length).
        max_detour_miles: Stations farther than this from the route are dropped.

    Returns:
        Sorted list of station dicts (ascending mile_marker) with 'mile_marker'
        field added. When multiple stations share a mile_marker within 0.1 miles
        only the cheapest is kept.
    """
    projected: list[dict] = []

    if not stations or not polyline:
        return projected

    # Build a KDTree on polyline points (lat, lon) for O(S log P) nearest-point
    # lookup instead of the naive O(S × P) double loop. For a NY→LA route with
    # ~6000 polyline points and ~400 candidate stations, this reduces ~2.4M
    # haversine calls to ~400 tree queries — typically 50–100× faster.
    poly_arr = np.array([(lat, lon) for lon, lat in polyline], dtype=np.float64)
    poly_tree = KDTree(poly_arr)

    station_coords = np.array(
        [(s["lat"], s["lon"]) for s in stations], dtype=np.float64
    )

    # query returns Euclidean degree-distance and nearest polyline index per station
    distances_deg, nearest_indices = poly_tree.query(station_coords)

    # Convert degree distance to approximate miles (1° ≈ 69 miles at equator;
    # good enough for the detour filter — exact haversine only where needed).
    distances_miles = distances_deg * 69.0

    mileage_np = np.array(mileage_array, dtype=np.float64)

    for i, station in enumerate(stations):
        best_dist = float(distances_miles[i])
        best_idx = int(nearest_indices[i])

        if best_dist > max_detour_miles:
            logger.warning(
                "Station %s (%s, %s) is %.1f miles from route — outside corridor, skipping",
                station.get("name"), station.get("city"), station.get("state"), best_dist,
            )
            continue

        entry = dict(station)
        entry["mile_marker"] = float(mileage_np[best_idx])
        projected.append(entry)

    # Sort by mile_marker ascending
    projected.sort(key=lambda s: s["mile_marker"])

    # Deduplicate: within 0.1-mile windows keep cheapest
    if not projected:
        return projected

    deduped: list[dict] = []
    window_start = projected[0]["mile_marker"]
    window_best = projected[0]

    for s in projected[1:]:
        if s["mile_marker"] - window_start <= 0.1:
            if Decimal(str(s["price"])) < Decimal(str(window_best["price"])):
                window_best = s
        else:
            deduped.append(window_best)
            window_start = s["mile_marker"]
            window_best = s

    deduped.append(window_best)

    logger.info(
        "project_stations_onto_route: %d stations → %d projected (after dedup)",
        len(stations),
        len(deduped),
    )
    return deduped


# ---------------------------------------------------------------------------
# Phase 2 — Mandatory stop identification
# ---------------------------------------------------------------------------


def compute_mandatory_stops(
    stations_on_route: list[dict], total_miles: float
) -> set[int]:
    """
    Greedy interval covering. Returns the set of opis_ids that MUST be visited
    to ensure the vehicle never runs out of fuel.

    Raises:
        NoRouteError: If any gap > tank range has no reachable station.
    """
    tank_range, _mpg, _cap = _vehicle_params()

    position: float = 0.0
    mandatory: set[int] = set()

    while position + tank_range < total_miles:
        reachable = [
            s for s in stations_on_route
            if position < s["mile_marker"] <= position + tank_range
        ]
        if not reachable:
            gap_end = position + tank_range
            raise NoRouteError(
                f"No fuel station within vehicle range between mile "
                f"{position:.1f} and mile {gap_end:.1f}"
            )

        # Furthest reachable → minimum number of mandatory stops
        furthest = max(reachable, key=lambda s: s["mile_marker"])
        mandatory.add(furthest["opis_id"])
        position = furthest["mile_marker"]

    logger.info(
        "compute_mandatory_stops: %d mandatory stops identified for %.1f mile route",
        len(mandatory),
        total_miles,
    )
    return mandatory


# ---------------------------------------------------------------------------
# Phase 3 — Cost optimisation walk
# ---------------------------------------------------------------------------


def _to_decimal(value) -> Decimal:
    """Safely convert a numeric value to Decimal."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def plan_fuel_stops(
    stations_on_route: list[dict], total_miles: float
) -> list[dict]:
    """
    Walk the route greedily choosing the cheapest reachable station, with look-ahead
    optimisation: buy only enough fuel to reach a cheaper station ahead when one exists.

    Parameters:
        stations_on_route: Output of project_stations_onto_route (sorted by mile_marker).
        total_miles:        Total route distance in miles.

    Returns:
        Ordered list of stop dicts (see module docstring for field spec).

    Raises:
        NoRouteError: If the route cannot be completed.
    """
    tank_range, mpg, tank_capacity = _vehicle_params()

    mandatory_ids = compute_mandatory_stops(stations_on_route, total_miles)

    stops: list[dict] = []
    current_mile: float = 0.0
    # The vehicle is assumed to have just enough fuel to reach the first stop —
    # it arrives there with 0 gallons remaining.  We model this by starting with
    # current_fuel=0 and treating the first stop specially: instead of computing
    # fuel_on_arrival from travel distance, we set it to 0 directly.  This means
    # the algorithm never strands the vehicle before mile 0 while still reflecting
    # the realistic "depot start" scenario where the tank may be near empty.
    current_fuel: float = 0.0

    # Build a quick lookup: opis_id → station for mandatory stop mile_markers
    station_by_id: dict[int, dict] = {s["opis_id"]: s for s in stations_on_route}

    # Sorted mandatory checkpoints (used for safety minimum calculation)
    mandatory_checkpoints: list[float] = sorted(
        station_by_id[oid]["mile_marker"]
        for oid in mandatory_ids
        if oid in station_by_id
    )

    visited: set[int] = set()
    first_stop_done = False

    while True:
        if not first_stop_done:
            # First stop: pick the cheapest station within the first tank_range
            # miles.  The vehicle is assumed to have sufficient fuel to reach it
            # (arrives with 0 gallons — fuel_on_arrival is forced to 0 below).
            reachable = [
                s for s in stations_on_route
                if s["mile_marker"] <= tank_range
                and s["opis_id"] not in visited
            ]
        else:
            fuel_range = current_fuel * mpg
            if current_mile + fuel_range >= total_miles:
                break
            reachable = [
                s for s in stations_on_route
                if s["mile_marker"] > current_mile
                and s["mile_marker"] <= current_mile + fuel_range
                and s["opis_id"] not in visited
            ]

        if not reachable:
            if not first_stop_done:
                raise NoRouteError(
                    f"No fuel station within the first {tank_range:.0f} miles of the route"
                )
            raise NoRouteError(
                f"No fuel station reachable from mile {current_mile:.1f} "
                f"(fuel range: {current_fuel * mpg:.1f} miles)"
            )

        # Pick the cheapest reachable station
        cheapest = min(reachable, key=lambda s: _to_decimal(s["price"]))

        # For the first stop the vehicle is assumed to arrive on fumes (0 gallons).
        # For all subsequent stops, compute fuel remaining from actual travel.
        if not first_stop_done:
            fuel_on_arrival = 0.0
        else:
            fuel_burned = (cheapest["mile_marker"] - current_mile) / mpg
            fuel_on_arrival = max(0.0, current_fuel - fuel_burned)

        logger.debug(
            "Considering stop at %s mile=%.1f price=%s fuel_on_arrival=%.2f",
            cheapest.get("name"), cheapest["mile_marker"],
            cheapest["price"], fuel_on_arrival,
        )

        # --- Safety minimum: must be able to reach the next mandatory checkpoint ---
        next_mandatory_mile: float | None = None
        for m in mandatory_checkpoints:
            if m > cheapest["mile_marker"] + 0.01:
                next_mandatory_mile = m
                break

        if next_mandatory_mile is not None:
            safety_min = max(
                0.0,
                (next_mandatory_mile - cheapest["mile_marker"]) / mpg - fuel_on_arrival,
            )
        else:
            # No mandatory checkpoint ahead — must carry enough to reach the destination
            miles_to_end = total_miles - cheapest["mile_marker"]
            safety_min = max(0.0, miles_to_end / mpg - fuel_on_arrival)

        # --- Look-ahead: is there a meaningfully cheaper station on a full tank? ---
        # "Meaningfully cheaper" means the price difference exceeds
        # MIN_PRICE_DIFF_PER_GALLON. Differences below this threshold are
        # ignored so the algorithm fills up here rather than making a micro-stop
        # (e.g. buying 2 gallons now just to reach a station that is $0.005/gal
        # cheaper — a saving of $0.10 that produces an unnecessary extra stop).
        full_tank_range = tank_capacity * mpg
        current_price = _to_decimal(cheapest["price"])
        ahead_cheaper = [
            s for s in stations_on_route
            if s["mile_marker"] > cheapest["mile_marker"]
            and s["mile_marker"] <= cheapest["mile_marker"] + full_tank_range
            and current_price - _to_decimal(s["price"]) >= Decimal(str(MIN_PRICE_DIFF_PER_GALLON))
            and s["opis_id"] not in visited
        ]

        is_recommended: bool
        if ahead_cheaper:
            # Buy just enough to reach the nearest cheaper station
            nearest_cheaper = min(ahead_cheaper, key=lambda s: s["mile_marker"])
            desired = (
                (nearest_cheaper["mile_marker"] - cheapest["mile_marker"]) / mpg
                - fuel_on_arrival
            )
            is_recommended = True
            logger.debug(
                "Cheaper station ahead at mile=%.1f price=%s; buying minimum to reach it",
                nearest_cheaper["mile_marker"], nearest_cheaper["price"],
            )
        else:
            # No cheaper station ahead — fill the tank completely
            desired = tank_capacity - fuel_on_arrival
            # Recommended if it's cheapest in range; forced if it's the only option
            is_recommended = len(reachable) > 1

        gallons_to_buy = max(safety_min, desired, 0.0)
        gallons_to_buy = min(gallons_to_buy, tank_capacity - fuel_on_arrival)

        if gallons_to_buy < 0.01 and first_stop_done:
            # Enough fuel to skip this stop; advance without recording it
            logger.debug(
                "Skipping stop at %s (gallons_to_buy=%.4f < 0.01)",
                cheapest.get("name"), gallons_to_buy,
            )
            visited.add(cheapest["opis_id"])
            current_fuel = fuel_on_arrival
            current_mile = cheapest["mile_marker"]
            continue

        price_dec = _to_decimal(cheapest["price"])
        gallons_dec = Decimal(str(round(gallons_to_buy, 6)))
        stop_cost = (price_dec * gallons_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        stop: dict = {
            "opis_id": cheapest["opis_id"],
            "name": cheapest["name"],
            "address": cheapest["address"],
            "city": cheapest["city"],
            "state": cheapest["state"],
            "lat": cheapest["lat"],
            "lon": cheapest["lon"],
            "mile_marker": round(cheapest["mile_marker"], 2),
            "price_per_gallon": float(price_dec),
            "gallons_purchased": round(float(gallons_dec), 2),
            "stop_cost": float(stop_cost),
            "fuel_remaining_on_arrival_gallons": round(fuel_on_arrival, 2),
            "is_recommended": is_recommended,
            "distance_to_next_stop_miles": None,  # filled in post-loop
        }

        stops.append(stop)
        visited.add(cheapest["opis_id"])
        current_fuel = fuel_on_arrival + float(gallons_dec)
        current_mile = cheapest["mile_marker"]
        first_stop_done = True

        logger.debug(
            "Stop added: %s | bought=%.2f gal | cost=$%.2f | fuel_after=%.2f gal",
            cheapest.get("name"), float(gallons_dec), float(stop_cost), current_fuel,
        )

    # Fill in distance_to_next_stop_miles for each stop
    for i, stop in enumerate(stops):
        if i < len(stops) - 1:
            stop["distance_to_next_stop_miles"] = round(
                stops[i + 1]["mile_marker"] - stop["mile_marker"], 2
            )
        else:
            stop["distance_to_next_stop_miles"] = round(total_miles - stop["mile_marker"], 2)

    logger.info(
        "plan_fuel_stops: %d stops planned, total miles=%.1f",
        len(stops),
        total_miles,
    )
    return stops


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def compute_summary(stops: list[dict]) -> dict:
    """
    Aggregate stop list into a route summary.

    Returns:
        Dict with total_gallons, total_fuel_cost, num_stops, avg_price_per_gallon.
    """
    if not stops:
        return {
            "total_gallons": 0.0,
            "total_fuel_cost": 0.0,
            "num_stops": 0,
            "avg_price_per_gallon": 0.0,
        }

    total_gallons = Decimal("0")
    total_cost = Decimal("0")

    for s in stops:
        total_gallons += Decimal(str(s["gallons_purchased"]))
        total_cost += Decimal(str(s["stop_cost"]))

    avg_price = (total_cost / total_gallons).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    ) if total_gallons else Decimal("0")

    return {
        "total_gallons": float(total_gallons.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "total_fuel_cost": float(total_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "num_stops": len(stops),
        "avg_price_per_gallon": float(avg_price),
    }

"""
Route service — orchestrates the full fuel-stop planning pipeline.

Responsibilities:
    1. Geocode start and end location strings (parallel, non-blocking).
    2. Fetch the driving route from OSRM (single external call).
    3. Query the in-memory station index for stations near the route.
    4. Project stations onto the 1-D mile axis and run the cost planner.
    5. Assemble and return the response payload dict.

This layer has no knowledge of HTTP, caching, or Django views. It receives
plain strings and returns a plain dict — making it straightforward to test
without an HTTP client.
"""

import asyncio
import logging

from apps.routing.exceptions import GeocodingError, NoRouteError, RoutingServiceError
from apps.routing.services.geocoder import geocode_location
from apps.routing.services.osrm import fetch_route
from apps.routing.services.planner import (
    build_mileage_array,
    compute_summary,
    plan_fuel_stops,
    project_stations_onto_route,
)
from apps.routing.services.station_index import FuelStationIndex

logger = logging.getLogger("routing.service")

METERS_PER_MILE = 1609.344


async def plan_route(start_str: str, end_str: str) -> dict:
    """
    Execute the full pipeline for a single route request.

    Parameters:
        start_str: Raw start location string from the user (already validated).
        end_str:   Raw end location string from the user (already validated).

    Returns:
        Response payload dict with keys: fuel_stops, summary, route.

    Raises:
        GeocodingError:      If either location cannot be resolved.
        RoutingServiceError: If OSRM is unavailable or returns an error.
        NoRouteError:        If no valid fuel stop sequence can cover the route.
        ValueError:          If the computed route is too short to plan stops for.
    """
    # --- Step 1: Geocode start and end in parallel ---
    # geocode_location is synchronous (uses requests under the hood).
    # run_in_executor offloads each call to the default thread pool so they run
    # concurrently without blocking the async event loop.
    loop = asyncio.get_event_loop()
    start_coords, end_coords = await asyncio.gather(
        loop.run_in_executor(None, geocode_location, start_str),
        loop.run_in_executor(None, geocode_location, end_str),
    )

    # --- Step 2: Fetch driving route from OSRM (single external call) ---
    route_data = await fetch_route(start_coords, end_coords)

    total_miles = route_data["distance_meters"] / METERS_PER_MILE
    if total_miles < 10.0:
        raise ValueError("Route too short to plan fuel stops")

    # --- Step 3: Find stations near the route corridor (in-memory, ~3ms) ---
    polyline: list[list[float]] = route_data["geometry"]["coordinates"]
    polyline_tuples: list[tuple[float, float]] = [(p[0], p[1]) for p in polyline]

    nearby_stations = FuelStationIndex.query_near_route(polyline_tuples)

    # --- Step 4: Project onto mile axis and run cost planner (<10ms) ---
    mileage_array = build_mileage_array(polyline_tuples)
    stations_on_route = project_stations_onto_route(
        nearby_stations, polyline_tuples, mileage_array
    )

    stops = plan_fuel_stops(stations_on_route, total_miles)
    summary = compute_summary(stops)

    logger.info(
        "Plan computed: '%s' → '%s' | %d stops | $%.2f total",
        start_str, end_str, summary["num_stops"], summary["total_fuel_cost"],
    )

    # --- Step 5: Assemble response payload ---
    # fuel_stops is placed first so it appears before the large geometry array
    # in the JSON output, making the response easier to read in a terminal.
    return {
        "fuel_stops": stops,
        "summary": summary,
        "route": {
            "start": start_str,
            "end": end_str,
            "total_miles": round(total_miles, 2),
            "geometry": route_data["geometry"],
        },
    }

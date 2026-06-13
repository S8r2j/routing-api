"""
Async OSRM client. Makes exactly one HTTP call per route request.

The httpx.AsyncClient is created once at module level and reused across all
requests. This enables HTTP/1.1 keep-alive connection pooling to the OSRM
server — avoiding the TCP + TLS handshake overhead on every request, which
is measurable on cross-country routes where the OSRM response payload is large.
"""

import logging
import time

import httpx
from django.conf import settings

from apps.routing.exceptions import RoutingServiceError

logger = logging.getLogger('routing.osrm')

# Module-level client — shared across all requests within a Uvicorn worker.
# Limits kept low: we only ever make one concurrent OSRM call per request,
# so a pool of 10 connections is more than sufficient.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=settings.OSRM_TIMEOUT_SECONDS,
                write=5.0,
                pool=5.0,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            http2=False,
        )
    return _client


async def fetch_route(
    start: tuple[float, float],
    end: tuple[float, float],
) -> dict:
    """
    Fetch a driving route from the OSRM public demo server.

    Parameters:
        start: (lat, lon) of the origin.
        end:   (lat, lon) of the destination.

    Returns:
        Dict with keys:
            - geometry:         GeoJSON LineString (coordinates are [lon, lat])
            - distance_meters:  float, total route distance
            - duration_seconds: float, estimated travel time

    Raises:
        RoutingServiceError: On HTTP error, timeout, bad JSON, or OSRM error code.
    """
    start_lat, start_lon = start
    end_lat, end_lon = end

    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson&steps=false"
    )

    logger.debug("OSRM request: %s", url)
    t0 = time.monotonic()

    try:
        response = await _get_client().get(url)
    except httpx.TimeoutException:
        logger.error("OSRM request timed out: %s", url)
        raise RoutingServiceError("OSRM request timed out")
    except httpx.RequestError as exc:
        logger.error("OSRM network error: %s", exc)
        raise RoutingServiceError(f"OSRM network error: {exc}")

    elapsed = (time.monotonic() - t0) * 1000
    logger.info("OSRM responded in %.1f ms (status %d)", elapsed, response.status_code)

    if response.status_code != 200:
        logger.error("OSRM returned HTTP %d for %s", response.status_code, url)
        raise RoutingServiceError(f"OSRM returned {response.status_code}")

    try:
        body = response.json()
    except Exception as exc:
        logger.error("OSRM returned invalid JSON: %s", exc)
        raise RoutingServiceError("OSRM returned invalid JSON")

    if body.get('code') != 'Ok':
        msg = body.get('message', 'unknown error')
        logger.error("OSRM error code '%s': %s", body.get('code'), msg)
        raise RoutingServiceError(f"OSRM error: {msg}")

    route = body['routes'][0]
    return {
        'geometry': route['geometry'],
        'distance_meters': float(route['distance']),
        'duration_seconds': float(route['duration']),
    }

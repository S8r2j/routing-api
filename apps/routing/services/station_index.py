"""
In-memory KD-tree singleton for fast spatial lookup of fuel stations.
Built once at app startup; all request-time lookups are pure in-memory
O(log n) operations with no database queries on the hot path.

Database access is delegated entirely to db.get_all_stations().
"""

import logging

import numpy as np
from scipy.spatial import KDTree  # type: ignore

logger = logging.getLogger("routing.algorithm")


class FuelStationIndex:
    """
    Singleton wrapper around a scipy KDTree built from FuelStation lat/lon coords.

    Usage:
        FuelStationIndex.load()          # once, at startup
        FuelStationIndex.query_near_point(lat, lon)
        FuelStationIndex.query_near_route(polyline_points)
    """

    _tree: KDTree | None = None
    _stations: list[dict] = []
    _loaded: bool = False

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @classmethod
    def load(cls) -> None:
        """
        Fetch all stations from the DB via the db layer, build a KDTree on
        (lat, lon), and store the station list for later retrieval.

        Must be called before any query method. Calling it again reloads from DB.
        """
        from apps.routing.services.db import get_all_stations

        stations = get_all_stations()

        if not stations:
            logger.warning("FuelStationIndex.load(): no stations found in DB — index is empty")
            cls._stations = []
            cls._tree = None
            cls._loaded = True
            return

        coords = np.array([[s["lat"], s["lon"]] for s in stations], dtype=float)
        cls._tree = KDTree(coords)
        cls._stations = stations
        cls._loaded = True

        logger.info("FuelStationIndex loaded %d stations into KD-tree", len(stations))

    # -------------------------------------------------------------------
    # Guard
    # -------------------------------------------------------------------

    @classmethod
    def _ensure_loaded(cls) -> None:
        """Raise if the index has not been loaded yet."""
        if not cls._loaded:
            raise RuntimeError("FuelStationIndex not loaded. Run load() first.")

    # -------------------------------------------------------------------
    # Query helpers
    # -------------------------------------------------------------------

    @classmethod
    def query_near_point(
        cls, lat: float, lon: float, radius_deg: float = 0.5
    ) -> list[dict]:
        """
        Return all stations within `radius_deg` degrees of (lat, lon).

        Uses Euclidean distance in degree-space (fine for continental US at
        this scale — 0.5° ≈ 35 miles).

        Returns:
            List of station dicts (same structure as stored in _stations).
        """
        cls._ensure_loaded()

        if cls._tree is None:
            return []

        point = np.array([lat, lon], dtype=float)
        indices = cls._tree.query_ball_point(point, r=radius_deg)
        return [cls._stations[i] for i in indices]

    @classmethod
    def query_near_route(
        cls,
        polyline_points: list[tuple[float, float]],
        radius_deg: float = 0.35,
    ) -> list[dict]:
        """
        Find all stations within `radius_deg` degrees of any point on the route.

        Parameters:
            polyline_points: List of (lon, lat) tuples from OSRM.
            radius_deg: Search radius in degrees (0.35° ≈ 24 miles).

        Returns:
            Deduplicated list of station dicts. When the same opis_id appears
            multiple times (e.g. near several route points), only the entry
            with the lowest price is retained.
        """
        cls._ensure_loaded()

        if cls._tree is None or not polyline_points:
            return []

        # Convert (lon, lat) → (lat, lon) for tree lookup
        query_coords = np.array([[lat, lon] for lon, lat in polyline_points], dtype=float)

        # Batch query: for each route point get nearby station indices
        raw_indices: list[list[int]] = cls._tree.query_ball_point(
            query_coords, r=radius_deg
        )

        # Flatten and deduplicate — keep lowest price per opis_id
        best: dict[int, dict] = {}
        for index_list in raw_indices:
            for i in index_list:
                station = cls._stations[i]
                oid = station["opis_id"]
                if oid not in best or station["price"] < best[oid]["price"]:
                    best[oid] = station

        result = list(best.values())
        logger.debug(
            "query_near_route: %d route points → %d unique stations within %.2f°",
            len(polyline_points),
            len(result),
            radius_deg,
        )
        return result

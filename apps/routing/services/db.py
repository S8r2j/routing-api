"""
Database access layer for the routing app.

All ORM queries and writes are centralised here. No business logic lives in
this file — it translates between Django model instances and plain dicts that
the rest of the service layer can consume without knowing about the ORM.
"""

import logging

logger = logging.getLogger("routing.db")


def get_all_stations() -> list[dict]:
    """
    Return every FuelStation row as a plain dict.

    Called once at startup by FuelStationIndex.load() to populate the KD-tree.
    Returns an empty list if the table is empty.
    """
    from apps.routing.models import (
        FuelStation,  # local import — avoids circular deps at module load
    )

    stations = []
    for s in FuelStation.objects.all():
        stations.append(
            {
                "opis_id": s.opis_id,
                "name": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "lat": s.lat,
                "lon": s.lon,
                "price": s.price,   # kept as Decimal for downstream arithmetic
                "rack_id": s.rack_id,
            }
        )

    logger.debug("get_all_stations: returned %d rows", len(stations))
    return stations


def upsert_station(
    opis_id: int,
    name: str,
    address: str,
    city: str,
    state: str,
    lat: float,
    lon: float,
    price: float,
    rack_id: int | None,
) -> None:
    """
    Insert or update a FuelStation row keyed on opis_id.

    Called by the load_stations management command for each CSV row. Idempotent —
    re-running with the same opis_id updates the existing record in place.
    """
    from apps.routing.models import FuelStation

    FuelStation.objects.update_or_create(
        opis_id=opis_id,
        defaults=dict(
            name=name,
            address=address,
            city=city,
            state=state,
            lat=lat,
            lon=lon,
            price=price,
            rack_id=rack_id,
        ),
    )

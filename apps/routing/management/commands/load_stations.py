"""
Management command to geocode the fuel price CSV and populate the FuelStation table.
Run once: python manage.py load_stations
Idempotent — uses update_or_create keyed on opis_id.
"""

import csv
import logging
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.routing.services.db import upsert_station

logger = logging.getLogger("routing.algorithm")


def _geocode_pgeocode(city: str, state: str) -> tuple[float, float] | tuple[None, str]:
    """
    Try to resolve city+state via pgeocode using US postal codes.

    Returns (lat, lon) on success, or (None, reason_string) on failure.
    """
    try:
        import math

        import pgeocode  # type: ignore

        nomi = pgeocode.Nominatim("us")
        df = nomi._data  # underlying pandas DataFrame
        mask = (df["state_code"].str.upper() == state.upper()) & (
            df["place_name"].str.upper() == city.upper()
        )
        matches = df[mask]
        if matches.empty:
            # Check whether the state code itself exists in the dataset to
            # distinguish "unknown city in valid state" from "non-US state code".
            state_exists = (df["state_code"].str.upper() == state.upper()).any()
            if not state_exists:
                return None, f"non-US state code ({state})"
            return None, f"pgeocode returned no match for city '{city}' in state '{state}'"
        row = matches.iloc[0]
        lat, lon = float(row["latitude"]), float(row["longitude"])
        if math.isnan(lat) or math.isnan(lon):
            return None, f"pgeocode returned NaN coordinates for {city}, {state}"
        return lat, lon
    except Exception as exc:
        return None, f"pgeocode error: {exc}"


def _geocode_nominatim(
    city: str, state: str, delay: float = 1.0
) -> tuple[float, float] | tuple[None, str]:
    """
    Resolve city+state via geopy Nominatim with a polite delay.

    Returns (lat, lon) on success, or (None, reason_string) on failure.
    """
    try:
        from geopy.exc import GeocoderTimedOut  # type: ignore
        from geopy.geocoders import Nominatim  # type: ignore

        geolocator = Nominatim(user_agent="fuel_route_geocoder")
        query = f"{city}, {state}, USA"
        location = geolocator.geocode(query, country_codes="us", timeout=10)
        time.sleep(delay)
        if location:
            return float(location.latitude), float(location.longitude)
        return None, f"Nominatim returned no result for '{query}'"
    except GeocoderTimedOut:
        return None, f"Nominatim timed out for '{city}, {state}'"
    except Exception as exc:
        return None, f"Nominatim error for '{city}, {state}': {exc}"


def _geocode(city: str, state: str, nom_delay: float = 1.0) -> tuple[float, float] | tuple[None, str]:
    """
    Resolve a city+state to (lat, lon).

    Resolution order:
    1. pgeocode geonames data (offline, fast)
    2. geopy Nominatim (network, rate-limited fallback)

    Returns (lat, lon) on success, or (None, reason_string) on failure.
    """
    result = _geocode_pgeocode(city, state)
    if result[0] is not None:
        return result

    pgeocode_reason = result[1]
    nom_result = _geocode_nominatim(city, state, delay=nom_delay)
    if nom_result[0] is not None:
        return nom_result

    return None, f"{pgeocode_reason}; {nom_result[1]}"


class Command(BaseCommand):
    help = "Geocode fuel price CSV and populate the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=50,
            help="Number of rows between progress prints (default: 50)",
        )
        parser.add_argument(
            "--nominatim-delay",
            type=float,
            default=1.0,
            help="Seconds to wait between Nominatim calls to respect rate limit (default: 1.0)",
        )

    def handle(self, *args, **options):
        batch_size: int = options["batch"]
        nom_delay: float = options["nominatim_delay"]
        csv_path = Path(settings.BASE_DIR) / "data" / "fuel-prices-for-be-assessment.csv"

        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        self.stdout.write(f"Loading stations from {csv_path} ...")

        # Geocode cache so we don't hit Nominatim twice for the same city/state.
        geo_cache: dict[str, tuple[float, float] | None] = {}

        loaded = 0
        skipped = 0

        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        total = len(rows)
        self.stdout.write(f"Found {total} rows in CSV.")

        for idx, row in enumerate(rows, start=1):
            city = row.get("City", "").strip()
            state = row.get("State", "").strip()
            cache_key = f"{city.upper()}|{state.upper()}"

            if cache_key not in geo_cache:
                geo_cache[cache_key] = _geocode(city, state, nom_delay=nom_delay)

            cached_result = geo_cache[cache_key]

            if cached_result[0] is None:
                reason = cached_result[1]
                logger.warning(
                    "SKIP: %s, %s — reason: %s", city, state, reason
                )
                self.stderr.write(
                    self.style.WARNING(f"  SKIP: {city}, {state} — reason: {reason}")
                )
                skipped += 1
            else:
                lat, lon = cached_result
                opis_id_raw = row.get("OPIS Truckstop ID", "").strip()
                rack_id_raw = row.get("Rack ID", "").strip()

                try:
                    opis_id = int(float(opis_id_raw))
                except (ValueError, TypeError):
                    logger.warning(
                        "SKIP: %s, %s — reason: invalid OPIS Truckstop ID (%r)",
                        city, state, opis_id_raw,
                    )
                    skipped += 1
                    continue

                try:
                    price_str = row.get("Retail Price", "0").strip()
                    price = float(price_str) if price_str else 0.0
                except ValueError:
                    price = 0.0

                rack_id: int | None = None
                try:
                    rack_id = int(float(rack_id_raw)) if rack_id_raw else None
                except (ValueError, TypeError):
                    rack_id = None

                upsert_station(
                    opis_id=opis_id,
                    name=row.get("Truckstop Name", "").strip(),
                    address=row.get("Address", "").strip(),
                    city=city,
                    state=state,
                    lat=lat,
                    lon=lon,
                    price=price,
                    rack_id=rack_id,
                )
                loaded += 1

            if idx % batch_size == 0:
                self.stdout.write(f"  Processed {idx}/{total} rows (loaded={loaded}, skipped={skipped})")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Loaded {loaded} stations, skipped {skipped} rows."
            )
        )
        logger.info("load_stations complete: loaded=%d skipped=%d", loaded, skipped)

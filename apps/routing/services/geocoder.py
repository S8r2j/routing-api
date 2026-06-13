"""
Geocodes a free-text US location string to (lat, lon).
Results are LRU-cached in-process — unique locations are geocoded at most once
per server lifetime.

Resolution order:
1. pgeocode — offline geonames data, zero network I/O.
   Handles "City, ST", "City, State", and 5-digit ZIP inputs.
2. Nominatim — network fallback, only reached when pgeocode returns NaN.
"""

import math
import logging
import re
import unicodedata
from functools import lru_cache

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from apps.routing.exceptions import GeocodingError

logger = logging.getLogger('routing.geocoder')

# Continental USA bounding box
_LAT_MIN, _LAT_MAX = 24.396308, 49.384358
_LON_MIN, _LON_MAX = -125.0, -66.93457

_geolocator = Nominatim(user_agent="fuel_route_api_v1", timeout=5)

# US state name → two-letter abbreviation
_STATE_NAME_TO_ABBR: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

# 5-digit ZIP pattern
_ZIP_RE = re.compile(r'^\d{5}$')

# Unicode script blocks that indicate non-Latin input
_NON_LATIN_SCRIPTS = frozenset({
    'CJK', 'HIRAGANA', 'KATAKANA', 'HANGUL',
    'ARABIC', 'HEBREW', 'CYRILLIC', 'DEVANAGARI',
    'THAI', 'GEORGIAN', 'ARMENIAN', 'TIBETAN',
    'MYANMAR', 'ETHIOPIC', 'SINHALA', 'KHMER',
    'MONGOLIAN', 'BUGINESE', 'TAI',
})

# SQL / injection patterns — single quotes, semicolons, equals signs
_SQL_RE = re.compile(r"[';=]|--")


def _parse_location(location: str) -> tuple[str, str] | None:
    """
    Parse a free-text location string into (city, state_abbr) or (zip, '') .

    Handles:
      - "New York, NY"
      - "New York, New York"
      - "90210"           → ('90210', '')
      - "Los Angeles, CA 90001" → city extracted, ZIP ignored

    Returns None if the string cannot be parsed into a recognisable form.
    """
    text = location.strip()

    # Pure ZIP code
    if _ZIP_RE.match(text):
        return text, ''

    # Split on the last comma: everything before is city, everything after is state
    if ',' not in text:
        return None

    city_part, state_part = text.rsplit(',', 1)
    city = city_part.strip()
    state_raw = state_part.strip()

    # Strip an optional trailing ZIP from the state field, e.g. "CA 90001"
    state_raw = re.sub(r'\s+\d{5}(-\d{4})?$', '', state_raw).strip()

    # Normalise to two-letter abbreviation
    if len(state_raw) == 2:
        state_abbr = state_raw.upper()
    else:
        state_abbr = _STATE_NAME_TO_ABBR.get(state_raw.lower(), '').upper()

    if not state_abbr or not city:
        return None

    return city, state_abbr


def _pgeocode_lookup(location: str) -> tuple[float, float] | None:
    """
    Attempt to resolve the location string via pgeocode's offline geonames data.

    Returns (lat, lon) on success, None if the input cannot be parsed or
    pgeocode returns NaN / no match.
    """
    try:
        import pgeocode  # type: ignore

        parsed = _parse_location(location)
        if parsed is None:
            return None

        city_or_zip, state_abbr = parsed
        nomi = pgeocode.Nominatim('us')

        if _ZIP_RE.match(city_or_zip):
            # Direct postal-code lookup
            result = nomi.query_postal_code(city_or_zip)
            lat = float(result.get('latitude', float('nan')))
            lon = float(result.get('longitude', float('nan')))
        else:
            # City + state lookup via the bundled DataFrame
            df = nomi._data
            mask = (
                (df['state_code'].str.upper() == state_abbr) &
                (df['place_name'].str.upper() == city_or_zip.upper())
            )
            matches = df[mask]
            if matches.empty:
                return None
            row = matches.iloc[0]
            lat = float(row['latitude'])
            lon = float(row['longitude'])

        if math.isnan(lat) or math.isnan(lon):
            return None

        return lat, lon

    except Exception:
        return None


def _nominatim_geocode(location: str):
    """Call Nominatim, retrying once on timeout."""
    try:
        return _geolocator.geocode(location, country_codes='us')
    except GeocoderTimedOut:
        logger.warning("Nominatim timed out for '%s', retrying once", location)
        try:
            return _geolocator.geocode(location, country_codes='us')
        except GeocoderTimedOut:
            raise GeocodingError(f"Geocoding timed out for '{location}'")
    except GeocoderServiceError as exc:
        raise GeocodingError(f"Geocoding service error for '{location}': {exc}") from exc


def _validate_conus(lat: float, lon: float, location: str) -> None:
    """Raise GeocodingError if coordinates fall outside the CONUS bounding box."""
    if not (_LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX):
        logger.warning(
            "SKIP: %s — coordinates outside CONUS (lat=%.4f, lon=%.4f)",
            location, lat, lon,
        )
        raise GeocodingError("Location must be within the continental United States")


@lru_cache(maxsize=1000)
def geocode_location(location: str) -> tuple[float, float]:
    """
    Resolve a free-text US location string to (lat, lon).

    Tries pgeocode (offline) first; falls back to Nominatim only if pgeocode
    returns NaN or cannot parse the input. Results are cached for the lifetime
    of the process.

    Parameters:
        location: Human-readable location, e.g. "New York, NY" or "90210".

    Returns:
        (lat, lon) tuple in decimal degrees.

    Raises:
        GeocodingError: If the location cannot be resolved or is outside CONUS.
    """
    # Fix 3 — reject SQL / special-character injection patterns
    if _SQL_RE.search(location):
        raise GeocodingError("Invalid location input")

    # Fix 1 — reject non-Latin scripts before any network call
    for ch in location:
        script = unicodedata.name(ch, '').split()[0]
        if script in _NON_LATIN_SCRIPTS:
            raise GeocodingError(
                "Please provide the location in English "
                "(e.g., 'New York, NY' or 'Los Angeles, CA')"
            )

    # Tier 1 — pgeocode (offline, no network)
    coords = _pgeocode_lookup(location)
    if coords is not None:
        lat, lon = coords
        _validate_conus(lat, lon, location)
        logger.debug("Geocoded '%s' via pgeocode → (%.6f, %.6f)", location, lat, lon)
        return lat, lon

    # Tier 2 — Nominatim (network)
    logger.debug("pgeocode miss for '%s', falling back to Nominatim", location)
    result = _nominatim_geocode(location)

    if result is None:
        logger.warning("SKIP: %s — reason: Nominatim returned no result", location)
        raise GeocodingError(f"Could not resolve location: '{location}'")

    lat, lon = float(result.latitude), float(result.longitude)
    _validate_conus(lat, lon, location)
    logger.debug("Geocoded '%s' via Nominatim → (%.6f, %.6f)", location, lat, lon)
    return lat, lon

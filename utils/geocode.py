"""Geocoding utilities for candidate distance validation.

Extracts home address and desired work locations from StepStone profile text,
geocodes them using Nominatim, and calculates distances.

Nominatim requires max 1 request/second. Results are cached for the lifetime
of a scrape run; call clear_cache() between jobs.
"""
import logging
import re
import time

from geopy.geocoders import Nominatim
from geopy.distance import geodesic

logger = logging.getLogger(__name__)

# Module-level geocoder reused across calls
_geocoder = Nominatim(user_agent="stepstone-scraper-aramaz")

# In-memory cache: location string -> (lat, lon) or None
_geo_cache: dict[str, tuple[float, float] | None] = {}

# Rate limit tracking
_last_geocode_time: float = 0.0


def clear_cache() -> None:
    """Clear the geocoding cache between scrape jobs."""
    _geo_cache.clear()


def _rate_limited_geocode(query: str) -> tuple[float, float] | None:
    """Geocode a location string with Nominatim rate limiting (1 req/sec).

    Appends ', Deutschland' to disambiguate German cities (prevents 'Halle'
    matching Belgium, 'Frankfurt' matching Frankfurt an der Oder, etc.).
    Returns (lat, lon) on success, None on failure.
    """
    global _last_geocode_time

    cache_key = query.strip().lower()
    if cache_key in _geo_cache:
        return _geo_cache[cache_key]

    # Rate limit: Nominatim requires max 1 request per second
    now = time.time()
    elapsed = now - _last_geocode_time
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    try:
        search_query = f"{query}, Deutschland"
        location = _geocoder.geocode(search_query, timeout=10)
        _last_geocode_time = time.time()

        if location:
            result = (location.latitude, location.longitude)
            _geo_cache[cache_key] = result
            logger.info(f"Geocoded '{query}' -> ({result[0]:.4f}, {result[1]:.4f})")
            return result
        else:
            logger.warning(f"Geocoding failed for '{query}' - no results")
            _geo_cache[cache_key] = None
            return None
    except Exception as e:
        logger.warning(f"Geocoding error for '{query}': {e}")
        _geo_cache[cache_key] = None
        return None


def extract_wohnadresse(profile_text: str | None) -> str | None:
    """Extract the candidate's Wohnadresse (home address) from profile text.

    StepStone profile text contains lines like:
        'Wohnadresse\\t40880 Ratingen\\n\\nDeutschland'
        'Wohnadresse\\tDorsten\\n\\n'

    Returns the city/postal-code string (e.g. '40880 Ratingen'), or None.
    """
    if not profile_text:
        return None

    # Pattern 1: postal code (5 digits) + city name
    match = re.search(r"Wohnadresse\s+(\d{5}\s+[^\n]+)", profile_text)
    if match:
        return match.group(1).strip()

    # Pattern 2: city name only (no postal code)
    match = re.search(
        r"Wohnadresse\s+([A-Za-zäöüÄÖÜß]"
        r"[A-Za-zäöüÄÖÜß\s\-\.]+)",
        profile_text,
    )
    if match:
        return match.group(1).strip()

    return None


def extract_gewuenschte_arbeitsorte(profile_text: str | None) -> str | None:
    """Extract the candidate's desired work locations from profile text.

    StepStone profile text contains lines like:
        'Gewunschter Arbeitsort\\tRatingen Dusseldorf 40880 Ratingen'

    Returns the raw desired-locations string, or None.
    """
    if not profile_text:
        return None

    match = re.search(
        r"Gew[uü]nschte[r]?\s+Arbeitsort[e]?\s+([^\n]+)",
        profile_text,
    )
    if match:
        return match.group(1).strip()
    return None


def calculate_distance_km(
    candidate_location: str,
    job_location: str,
) -> float | None:
    """Calculate geodesic distance in km between two German city names.

    Returns the distance rounded to 1 decimal, or None if either location
    cannot be geocoded.
    """
    candidate_coords = _rate_limited_geocode(candidate_location)
    job_coords = _rate_limited_geocode(job_location)

    if candidate_coords is None or job_coords is None:
        return None

    try:
        distance = geodesic(candidate_coords, job_coords).kilometers
        return round(distance, 1)
    except Exception as e:
        logger.warning(f"Distance calculation error: {e}")
        return None


def check_desired_location_match(
    gewuenschte_arbeitsorte: str | None,
    job_location: str | None,
) -> bool:
    """Return True if the job city appears in the candidate's desired work locations.

    This is a positive relocation signal — the candidate explicitly wants to
    work in this city regardless of where they currently live.
    """
    if not gewuenschte_arbeitsorte or not job_location:
        return False

    desired_lower = gewuenschte_arbeitsorte.lower()
    # Strip parenthetical qualifiers: 'Halle (Saale)' -> 'halle'
    job_base = re.sub(r"\s*\([^)]*\)", "", job_location.lower()).strip()

    return job_base in desired_lower

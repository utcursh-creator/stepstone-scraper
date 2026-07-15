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


# German job ads name a district as "<Gemeinde> OT <Ortsteil>" ("Wölfersheim OT
# Wohnbach" = the Wohnbach district of Wölfersheim). No gazetteer holds that
# composite as a place name — neither Nominatim nor StepStone's own Ort
# lookup — so the whole string resolves to nothing.
#
# The \b is load-bearing, not decoration. Without it the literal "OT" matches
# mid-word in an all-caps location and eats the rest of the string:
# "ROT AM SEE" -> "R", "WÜSTENROT BADEN" -> "WÜSTENR". Rot am See and Rot an
# der Rot are real Baden-Württemberg municipalities and models/job.py accepts
# `location` as a bare str, so an all-caps Airtable row reaches this unaltered.
# Case-sensitivity alone does NOT make lowercase names like "Otterndorf" safe —
# only the word boundary does.
_ORTSTEIL_RE = re.compile(r"\s*[/,]?\s*\b(?:OT|Ortsteil|ortsteil)\s+\S.*$")


def strip_ortsteil(location: str) -> str:
    """Reduce '<Gemeinde> OT <Ortsteil>' to '<Gemeinde>'.

    Returns `location` unchanged when there is no Ortsteil suffix, so callers
    can apply this unconditionally — only strings that are already unresolvable
    change. Also returns the original if stripping would leave nothing (a bare
    'OT Wohnbach' with no municipality).

    Examples:
      "Wölfersheim OT Wohnbach"        → "Wölfersheim"
      "06242 Braunsbedra /OT Krumpa"   → "06242 Braunsbedra"
      "Wettin-Löbejün OT Dobis"        → "Wettin-Löbejün"
      "Neustadt (Ortsteil Mussbach)"   → "Neustadt"
      "Warendorf"                      → "Warendorf"   (unchanged)
      "ROT AM SEE"                     → "ROT AM SEE"  (unchanged)
    """
    if not location:
        return location
    # rstrip cleans up a separator the suffix left dangling, e.g. the opening
    # paren of "Neustadt (Ortsteil Mussbach)".
    stripped = _ORTSTEIL_RE.sub("", location).strip().rstrip(" (,/").strip()
    return stripped or location


def _geocode_query(query: str) -> tuple[float, float] | None:
    """One rate-limited Nominatim lookup. No caching — see _rate_limited_geocode.

    Appends ', Deutschland' to disambiguate German cities (prevents 'Halle'
    matching Belgium, 'Frankfurt' matching Frankfurt an der Oder, etc.).
    """
    global _last_geocode_time

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
            logger.info(f"Geocoded '{query}' -> ({result[0]:.4f}, {result[1]:.4f})")
            return result
        logger.warning(f"Geocoding failed for '{query}' - no results")
        return None
    except Exception as e:
        logger.warning(f"Geocoding error for '{query}': {e}")
        return None


def _rate_limited_geocode(query: str) -> tuple[float, float] | None:
    """Geocode a location string, falling back to its municipality on failure.

    Returns (lat, lon) on success, None if neither the full string nor its
    Ortsteil-stripped form resolves. Results (including None) are cached under
    the original query for the lifetime of the run.

    The fallback only fires after the full string has already failed, so a
    location that geocodes today keeps its exact coordinates.
    """
    cache_key = query.strip().lower()
    if cache_key in _geo_cache:
        return _geo_cache[cache_key]

    result = _geocode_query(query)

    if result is None:
        base = strip_ortsteil(query)
        if base != query:
            logger.info(
                f"Retrying geocode for {query!r} without its Ortsteil suffix -> {base!r}"
            )
            result = _geocode_query(base)

    _geo_cache[cache_key] = result
    return result


def geocode_location(location: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon), or None if it cannot be resolved.

    Public entry point for callers that need to know whether a location is
    resolvable at all — e.g. main.run_scrape validating the job's own town
    before spending any unlock credits.
    """
    return _rate_limited_geocode(location)


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


# Distance-decision reason codes returned by should_accept_far_candidate.
# These are stable strings used for logging + the candidate's rejection
# message in German. Keep them in sync if you add new codes.
DIST_TOO_FAR_NO_RELOCATION = "too_far_no_relocation"
DIST_TOO_FAR_FOR_RELOCATION = "too_far_for_relocation"
DIST_RELOCATION_ACCEPTED = "relocation_accepted"


def should_accept_far_candidate(
    distance_km: float,
    relocation_max_km: int,
    gewuenschte_arbeitsorte: str | None,
    job_location: str | None,
) -> tuple[bool, str]:
    """Decide whether a candidate whose Wohnort exceeds the job radius is still
    acceptable as a relocation candidate.

    Caller is responsible for the first-tier check (distance_km > max_distance_km);
    this function only handles the "what now?" decision for the far-Wohnort case.

    Logic (option B from the design discussion — Umair's choice):
      1. If `distance_km > relocation_max_km`: REJECT. The candidate is too far
         for any relocation signal to be plausible (Suraj-style 120 km Koch case).
      2. Otherwise, if the candidate's `gewünschte_arbeitsorte` mentions the
         job city: ACCEPT. They're a relocation candidate within feasible range.
      3. Otherwise: REJECT. Too far without any relocation signal.

    `relocation_max_km == 0` means "no softening at all" — every far-Wohnort
    candidate is rejected, even with a perfect relocation signal.

    Returns (accepted, reason_code) where reason_code is one of the
    DIST_* constants above.
    """
    if relocation_max_km <= 0 or distance_km > relocation_max_km:
        return False, DIST_TOO_FAR_FOR_RELOCATION
    if check_desired_location_match(gewuenschte_arbeitsorte, job_location):
        return True, DIST_RELOCATION_ACCEPTED
    return False, DIST_TOO_FAR_NO_RELOCATION

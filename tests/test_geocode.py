from unittest.mock import patch, MagicMock
import utils.geocode as geocode_mod
from utils.geocode import (
    extract_wohnadresse,
    extract_gewuenschte_arbeitsorte,
    calculate_distance_km,
    check_desired_location_match,
    should_accept_far_candidate,
    clear_cache,
    DIST_TOO_FAR_NO_RELOCATION,
    DIST_TOO_FAR_FOR_RELOCATION,
    DIST_RELOCATION_ACCEPTED,
)


# -- extract_wohnadresse --

def test_extract_wohnadresse_with_postal_code():
    text = "Wohnadresse\t40880 Ratingen\n\nDeutschland"
    assert extract_wohnadresse(text) == "40880 Ratingen"


def test_extract_wohnadresse_city_only():
    text = "Wohnadresse\tDorsten\n\n"
    assert extract_wohnadresse(text) == "Dorsten"


def test_extract_wohnadresse_missing():
    assert extract_wohnadresse("Some random profile text without address") is None


def test_extract_wohnadresse_empty():
    assert extract_wohnadresse("") is None
    assert extract_wohnadresse(None) is None


# -- extract_gewuenschte_arbeitsorte --

def test_extract_gewuenschte_arbeitsorte():
    text = "Gewunschter Arbeitsort\tHamburg 21035 Hamburg"
    result = extract_gewuenschte_arbeitsorte(text)
    assert result is not None
    assert "Hamburg" in result


def test_extract_gewuenschte_arbeitsorte_umlaut():
    text = "Gewunschter Arbeitsort\tKoln Dusseldorf"
    result = extract_gewuenschte_arbeitsorte(text)
    assert result is not None


def test_extract_gewuenschte_arbeitsorte_missing():
    assert extract_gewuenschte_arbeitsorte("No desired locations here") is None


# -- check_desired_location_match --

def test_desired_location_match_positive():
    assert check_desired_location_match("Hamburg 21035 Hamburg", "Hamburg") is True


def test_desired_location_match_negative():
    assert check_desired_location_match("Hamburg 21035 Hamburg", "Dortmund") is False


def test_desired_location_match_with_qualifier():
    # "Halle (Saale)" -> base "halle" should match "Halle Saale" in desired
    assert check_desired_location_match("Halle Saale", "Halle (Saale)") is True


def test_desired_location_match_none():
    assert check_desired_location_match(None, "Hamburg") is False
    assert check_desired_location_match("Hamburg", None) is False


# -- calculate_distance_km (mocked geocoding) --

def test_calculate_distance_km_success():
    clear_cache()
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        def mock_geocode(query, timeout=10):
            loc = MagicMock()
            if "Hamburg" in query:
                loc.latitude, loc.longitude = 53.5753, 10.0153
            else:
                loc.latitude, loc.longitude = 51.5136, 7.4653  # Dortmund
            return loc

        mock_gc.geocode.side_effect = mock_geocode
        dist = calculate_distance_km("Hamburg", "Dortmund")
    assert dist is not None
    assert 280 < dist < 295  # ~287 km geodesic Hamburg-Dortmund


def test_calculate_distance_km_geocode_failure_returns_none():
    clear_cache()
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.return_value = None
        dist = calculate_distance_km("Unknown City XYZ", "Dortmund")
    assert dist is None


def test_calculate_distance_km_uses_cache():
    clear_cache()
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        loc = MagicMock()
        loc.latitude, loc.longitude = 51.5136, 7.4653
        mock_gc.geocode.return_value = loc
        # First call populates cache
        calculate_distance_km("TestCity", "TestCity")
        call_count_after_first = mock_gc.geocode.call_count
        # Second call should use cache, no new geocode call
        calculate_distance_km("TestCity", "TestCity")
        assert mock_gc.geocode.call_count == call_count_after_first


# -- should_accept_far_candidate (option B: relocation cap) --
#
# The first-tier check (distance ≤ max) is the caller's responsibility; this
# helper only handles candidates already known to be beyond the strict radius.

def test_relocation_accepted_when_within_cap_and_desired_match():
    """Classic relocation case: 80 km Wohnort, wants to work in target city,
    well inside the 200 km feasibility cap → accepted."""
    accepted, reason = should_accept_far_candidate(
        distance_km=80.0,
        relocation_max_km=200,
        gewuenschte_arbeitsorte="Apfeltrang München",
        job_location="Apfeltrang",
    )
    assert accepted is True
    assert reason == DIST_RELOCATION_ACCEPTED


def test_relocation_rejected_when_beyond_cap_even_with_desired_match():
    """Regression test for Suraj Gajbhar — 120 km Wohnort with Apfeltrang in
    his desired locations USED to be accepted via the relocation softening.
    With a 100 km cap he must now be rejected; the desired-match no longer
    matters once we're beyond the feasibility distance."""
    accepted, reason = should_accept_far_candidate(
        distance_km=120.0,
        relocation_max_km=100,  # tight cap for this test
        gewuenschte_arbeitsorte="Apfeltrang München bundesweit",
        job_location="Apfeltrang",
    )
    assert accepted is False
    assert reason == DIST_TOO_FAR_FOR_RELOCATION


def test_rejected_within_cap_but_no_desired_match():
    """Wohnort beyond strict radius but inside the relocation cap, no
    Gewünschter-Arbeitsort match → no signal, reject."""
    accepted, reason = should_accept_far_candidate(
        distance_km=80.0,
        relocation_max_km=200,
        gewuenschte_arbeitsorte="Berlin Hamburg",  # no Apfeltrang
        job_location="Apfeltrang",
    )
    assert accepted is False
    assert reason == DIST_TOO_FAR_NO_RELOCATION


def test_relocation_cap_zero_disables_softening_entirely():
    """relocation_max_km == 0 → pure Wohnort-only mode: every far candidate
    is rejected, even with a matching gewünschte_arbeitsorte."""
    accepted, reason = should_accept_far_candidate(
        distance_km=30.0,
        relocation_max_km=0,
        gewuenschte_arbeitsorte="Apfeltrang",
        job_location="Apfeltrang",
    )
    assert accepted is False
    assert reason == DIST_TOO_FAR_FOR_RELOCATION


def test_relocation_at_exact_cap_is_accepted():
    """Boundary: distance == cap is within the feasibility window (≤)."""
    accepted, reason = should_accept_far_candidate(
        distance_km=200.0,
        relocation_max_km=200,
        gewuenschte_arbeitsorte="Apfeltrang",
        job_location="Apfeltrang",
    )
    assert accepted is True
    assert reason == DIST_RELOCATION_ACCEPTED


def test_relocation_one_km_beyond_cap_is_rejected():
    """Boundary: distance == cap + 1 is outside the feasibility window."""
    accepted, reason = should_accept_far_candidate(
        distance_km=201.0,
        relocation_max_km=200,
        gewuenschte_arbeitsorte="Apfeltrang",
        job_location="Apfeltrang",
    )
    assert accepted is False
    assert reason == DIST_TOO_FAR_FOR_RELOCATION


def test_relocation_with_no_gewuenschte_arbeitsorte():
    """No desired-location field at all → no signal even within cap → reject."""
    accepted, reason = should_accept_far_candidate(
        distance_km=50.0,
        relocation_max_km=200,
        gewuenschte_arbeitsorte=None,
        job_location="Apfeltrang",
    )
    assert accepted is False
    assert reason == DIST_TOO_FAR_NO_RELOCATION


# -- the geocoder not answering is not "no such place" (prod 2026-09-28) --

import pytest
from geopy.exc import GeocoderRateLimited, GeocoderTimedOut


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(geocode_mod, "RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(geocode_mod, "_blocked_until", 0.0)
    monkeypatch.setattr(geocode_mod, "_last_geocode_time", 0.0)
    monkeypatch.setattr(geocode_mod.time, "sleep", lambda s: None)
    geocode_mod._geo_cache.clear()
    yield
    geocode_mod._geo_cache.clear()


def _loc(lat, lon):
    loc = MagicMock()
    loc.latitude, loc.longitude = lat, lon
    return loc


def test_a_429_that_clears_on_retry_still_resolves(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = [GeocoderRateLimited("429"), _loc(48.3, 9.1)]
        assert geocode_mod.geocode_location("Burladingen") == (48.3, 9.1)


def test_a_geocoder_that_keeps_refusing_raises_and_is_not_cached(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = GeocoderRateLimited("429")
        with pytest.raises(geocode_mod.GeocoderUnavailable):
            geocode_mod.geocode_location("Burladingen")
        assert mock_gc.geocode.call_count == 3, "three attempts, then give up"
    assert "burladingen" not in geocode_mod._geo_cache, "an outage must not be cached as 'no such place'"


def test_after_a_429_the_next_job_does_not_ask_again(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = GeocoderRateLimited("429")
        with pytest.raises(geocode_mod.GeocoderUnavailable):
            geocode_mod.geocode_location("Burladingen")
        calls = mock_gc.geocode.call_count
        with pytest.raises(geocode_mod.GeocoderUnavailable):
            geocode_mod.geocode_location("Deggendorf")
        assert mock_gc.geocode.call_count == calls, "cooldown: no request while blocked"


def test_a_timeout_is_unavailable_not_unknown(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = GeocoderTimedOut("slow")
        with pytest.raises(geocode_mod.GeocoderUnavailable):
            calculate_distance_km("22589 Hamburg", "Hamburg")


def test_resolved_places_survive_clear_cache_and_unknown_ones_do_not(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = lambda q, timeout=10: _loc(53.55, 10.0) if "Hamburg" in q else None
        geocode_mod.geocode_location("Hamburg")
        geocode_mod.geocode_location("Nowhere XYZ")
        clear_cache()
        calls = mock_gc.geocode.call_count
        geocode_mod.geocode_location("Hamburg")
        assert mock_gc.geocode.call_count == calls, "a town that resolved is not asked again"
        geocode_mod.geocode_location("Nowhere XYZ")
        assert mock_gc.geocode.call_count > calls, "an unknown place is asked again next job"


def test_a_d_prefixed_postcode_is_looked_up_without_the_prefix(fast_retries):
    """Live 2026-09-28: 'D-82205 Gilching' found nothing, so a candidate from a
    real German town was rejected as not locatable."""
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.return_value = _loc(48.1, 11.3)
        assert geocode_mod.geocode_location("D-82205 Gilching") == (48.1, 11.3)
        assert mock_gc.geocode.call_args[0][0] == "82205 Gilching, Deutschland"


def test_a_d_prefixed_home_address_is_read_whole_after_an_unlock():
    assert extract_wohnadresse("Wohnadresse D-82205 Gilching\n") == "82205 Gilching"


def test_an_address_with_a_district_falls_back_to_its_postcode(fast_retries):
    """Live 2026-09-28: '65205 Wiesbaden Delkenheim' found nothing, and a real
    German candidate was rejected as not locatable."""
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = lambda q, timeout=10: _loc(50.05, 8.37) if q == "65205, Deutschland" else None
        assert geocode_mod.geocode_location("65205 Wiesbaden Delkenheim") == (50.05, 8.37)


def test_a_town_without_a_postcode_gets_no_postcode_fallback(fast_retries):
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.return_value = None
        assert geocode_mod.geocode_location("Nowhere XYZ") is None
        assert mock_gc.geocode.call_count == 1


def test_a_foreign_address_never_falls_back_to_a_german_postcode(fast_retries):
    """'75001 Paris, FR' must not be placed at a German 75001: that could put a
    candidate from abroad inside the radius and spend a credit on them."""
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.return_value = None
        assert geocode_mod.geocode_location("75001 Paris, FR") is None
        assert all("75001, Deutschland" != c.args[0] for c in mock_gc.geocode.call_args_list)

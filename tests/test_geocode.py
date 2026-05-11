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

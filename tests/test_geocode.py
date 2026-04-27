from unittest.mock import patch, MagicMock
import utils.geocode as geocode_mod
from utils.geocode import (
    extract_wohnadresse,
    extract_gewuenschte_arbeitsorte,
    calculate_distance_km,
    check_desired_location_match,
    clear_cache,
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

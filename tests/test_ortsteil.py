"""Tests for the German Ortsteil ("<Gemeinde> OT <Ortsteil>") location handling.

Prod incident 2026-07-15, offer 2468824 'Physiotherapeut in Wölfersheim OT
Wohnbach'. One unresolvable location string caused three separate failures:

  1. StepStone's Ort gazetteer had no match  → no Ort chip → the radius filter
     silently dropped → the search went nationwide and returned candidates
     104-378 km away (and one in Pécs, Hungary).
  2. Nominatim had no match either           → the JOB's coordinates were None
     → haversine(candidate, None) → distance None for EVERY candidate.
  3. The fail-closed distance gate read that None as "this candidate lives
     abroad" and filed five Germans (Mannheim, Leuna, Luckau, Bad Hindelang)
     under 'Ausland' — after paying one credit for each.

These tests pin all three fixes.
"""
import pytest
from unittest.mock import patch

import main as main_mod
import scraper.search as search_mod
import utils.geocode as geocode_mod
from models.job import JobInput
from utils.geocode import (
    check_desired_location_match,
    clear_cache,
    geocode_location,
    strip_ortsteil,
)


# ---------------------------------------------------------------- strip_ortsteil

@pytest.mark.parametrize("raw, expected", [
    # The real prod strings that broke offer 2468824.
    ("Wölfersheim OT Wohnbach", "Wölfersheim"),
    ("Braunsbedra /OT Krumpa", "Braunsbedra"),
    ("06242 Braunsbedra /OT Krumpa", "06242 Braunsbedra"),
    ("Wettin-Löbejün OT Dobis", "Wettin-Löbejün"),
    # Spelled-out variant.
    ("Wölfersheim Ortsteil Wohnbach", "Wölfersheim"),
    ("Wölfersheim, OT Wohnbach", "Wölfersheim"),
    # Parenthesised variant must not leave a dangling '('.
    ("Neustadt (Ortsteil Mussbach)", "Neustadt"),
])
def test_strip_ortsteil_reduces_to_municipality(raw, expected):
    assert strip_ortsteil(raw) == expected


@pytest.mark.parametrize("raw", [
    # Every location that works today must pass through untouched — this is
    # what makes strip_ortsteil safe to apply unconditionally.
    "Warendorf",
    "Hamburg",
    "Rüdesheim am Rhein",
    "Grünheide (Mark) Hangelsberg",
    "Bad Hindelang",
    "68161 Mannheim",
    "Wettin-Löbejün",
    # False-positive guards: 'OT' inside a word is not the Ortsteil abbreviation.
    "Otterndorf",
    "Sankt Ottilien",
    "Ottobrunn",
    # ALL-CAPS guard. Without a word boundary the literal 'OT' matches inside
    # these and eats the rest ('ROT AM SEE' -> 'R'), which would make search.py
    # type 'R' into StepStone and silently source from an arbitrary wrong town.
    # 'Rot am See' and 'Rot an der Rot' are real Baden-Württemberg
    # municipalities, and models/job.py takes `location` as a bare str with no
    # case normalisation, so an all-caps Airtable row reaches strip_ortsteil raw.
    "ROT AM SEE",
    "ROT AN DER ROT",
    "WÜSTENROT BADEN",
    "PIROT Serbien",
    # Nothing left to fall back to → keep the original rather than return "".
    "OT Wohnbach",
    "",
])
def test_strip_ortsteil_is_a_noop_for_normal_locations(raw):
    assert strip_ortsteil(raw) == raw


# ---------------------------------------------------------------- geocode fallback

def _mock_nominatim(known: dict[str, tuple[float, float]]):
    """Build a Nominatim stub that resolves only `known` queries.

    Keys are matched against the query WITHOUT the ', Deutschland' suffix that
    _geocode_query appends.
    """
    calls: list[str] = []

    def geocode(query, timeout=10):
        calls.append(query)
        bare = query.removesuffix(", Deutschland")
        if bare in known:
            lat, lon = known[bare]
            return type("Loc", (), {"latitude": lat, "longitude": lon})()
        return None

    return geocode, calls


def test_geocode_falls_back_to_municipality_when_full_string_fails():
    """The Wölfersheim case: the composite resolves to nothing, the Gemeinde does."""
    clear_cache()
    geocode, calls = _mock_nominatim({"Wölfersheim": (50.3947, 8.8156)})
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = geocode
        coords = geocode_location("Wölfersheim OT Wohnbach")

    assert coords == (50.3947, 8.8156)
    assert calls == [
        "Wölfersheim OT Wohnbach, Deutschland",  # full string first
        "Wölfersheim, Deutschland",              # then the fallback
    ], "must try the full string before falling back"


def test_geocode_does_not_fall_back_when_full_string_resolves():
    """Precision guard: a location that geocodes today keeps its exact coords."""
    clear_cache()
    geocode, calls = _mock_nominatim({
        "Wölfersheim OT Wohnbach": (50.4000, 8.8300),  # the district itself
        "Wölfersheim": (50.3947, 8.8156),              # the municipality
    })
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = geocode
        coords = geocode_location("Wölfersheim OT Wohnbach")

    assert coords == (50.4000, 8.8300), "must keep the district's own coords"
    assert calls == ["Wölfersheim OT Wohnbach, Deutschland"], "no needless fallback"


def test_geocode_returns_none_when_neither_form_resolves():
    clear_cache()
    geocode, calls = _mock_nominatim({})
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = geocode
        assert geocode_location("Nirgendwo OT Nichts") is None
    assert len(calls) == 2


def test_geocode_makes_no_fallback_call_for_a_plain_city():
    """A city with no Ortsteil suffix must cost exactly one Nominatim call."""
    clear_cache()
    geocode, calls = _mock_nominatim({})
    with patch.object(geocode_mod, "_geocoder") as mock_gc:
        mock_gc.geocode.side_effect = geocode
        assert geocode_location("Nirgendwo") is None
    assert calls == ["Nirgendwo, Deutschland"]


# ---------------------------------------------------------------- search criterion

@pytest.mark.asyncio
async def test_search_types_the_municipality_not_the_ortsteil(monkeypatch):
    """StepStone must be asked for 'Wölfersheim', so the Ort chip + radius apply.

    Typing the full 'Wölfersheim OT Wohnbach' is what dropped the radius filter
    and let the search go nationwide.
    """
    typed: list[str] = []

    async def fake_criterion(page, term):
        typed.append(term)

    async def fake_chip(page):
        return True

    monkeypatch.setattr(search_mod, "_add_criterion_via_autosuggest", fake_criterion)
    monkeypatch.setattr(search_mod, "_country_chip_present", fake_chip)
    monkeypatch.setattr(search_mod, "_set_page_size", lambda *a, **k: _async_none())
    monkeypatch.setattr(search_mod, "_kill_cookie_banner", lambda *a, **k: _async_none())
    monkeypatch.setattr(search_mod, "_scrape_cards_guarded", lambda *a, **k: _async_list())
    monkeypatch.setattr(search_mod, "human_delay", lambda *a, **k: _async_none())

    await search_mod._execute_search(
        _FakePage(), "Physiotherapeut (m/w/d)", "Wölfersheim OT Wohnbach",
        max_distance_km=25, keywords=None,
    )

    assert "Wölfersheim" in typed, f"expected the municipality to be typed, got {typed}"
    assert "Wölfersheim OT Wohnbach" not in typed, (
        "the unresolvable Ortsteil string must never be typed as the Ort criterion"
    )


async def _async_none():
    return None


async def _async_list():
    return []


class _FakeField:
    async def click(self, *a, **k):
        return None

    async def fill(self, *a, **k):
        return None

    async def type(self, *a, **k):
        return None

    async def press(self, *a, **k):
        return None


class _FakePage:
    async def goto(self, *a, **k):
        return None

    async def query_selector(self, *a, **k):
        return _FakeField()

    async def evaluate(self, *a, **k):
        return None


# ---------------------------------------------------------------- pre-flight abort

def _job(location: str) -> JobInput:
    return JobInput(
        offer_id="2468824",
        stage_id="13166770",
        job_title="Physiotherapeut (m/w/d)",
        location=location,
        max_distance_km=25,
    )


@pytest.mark.asyncio
async def test_run_scrape_aborts_before_the_browser_when_job_location_is_ungeocodable(
    monkeypatch,
):
    """THE credit guard.

    If the job's own town cannot be geocoded, every distance is None and every
    candidate is rejected — the job can only burn credits. It must abort before
    the browser is even launched, which is upstream of every unlock.
    """
    launched = False

    async def fake_browser(*a, **k):
        nonlocal launched
        launched = True
        raise AssertionError("browser launched despite an ungeocodable job location")

    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: None)
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)

    result = await main_mod.run_scrape(_job("Wölfersheim OT Wohnbach"))

    # `launched is False` is the load-bearing assertion — it is the one that
    # fails if the pre-flight is removed. The rest would still pass without the
    # fix, because run_scrape's `except Exception` catches the AssertionError
    # from fake_browser and sets partial=True itself. Keep them as a spec of
    # the abort's shape, not as coverage.
    assert launched is False, "must not spend a browser session, a proxy hit, or a credit"
    assert result.partial is True, "the run must be reported as incomplete"
    assert result.candidates == [], "nothing may be unlocked"
    assert result.candidates_unlocked == 0
    assert result.offer_id == "2468824", "the result must still identify the job"


@pytest.mark.asyncio
async def test_abort_reason_reaches_the_webhook_payload(monkeypatch):
    """The abort exists to make a human edit the Airtable row — so the reason
    must survive model_dump into the n8n payload. `partial` alone cannot carry
    it: it also means 'unlock cap reached' and 'the scrape crashed'."""
    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: None)

    result = await main_mod.run_scrape(_job("Nirgendwo OT Nichts"))

    assert result.error, "the abort must record a reason"
    assert "Nirgendwo OT Nichts" in result.error, "name the offending location"
    payload = result.model_dump()
    assert payload["error"] == result.error, "reason must serialize for n8n/Slack"
    assert payload["partial"] is True


def test_relocation_signal_needs_the_municipality_not_the_ortsteil():
    """Why main.run_scrape passes strip_ortsteil(job.location) to the
    relocation check.

    check_desired_location_match does a substring test of the job town against
    the candidate's Gewünschte Arbeitsorte. Candidates write "Wölfersheim";
    nobody writes "Wölfersheim OT Wohnbach". Matching on the raw string is
    always False, so every relocation candidate on an Ortsteil job would be
    rejected as too_far_no_relocation — a silent regression that only became
    reachable once these jobs started producing a real distance.
    """
    desired = "Wölfersheim Friedberg 61200 Reichelsheim"

    assert check_desired_location_match(desired, "Wölfersheim OT Wohnbach") is False, (
        "raw Ortsteil string cannot match — this is the trap"
    )
    assert check_desired_location_match(
        desired, strip_ortsteil("Wölfersheim OT Wohnbach")
    ) is True, "the municipality must match the candidate's stated wish"


@pytest.mark.asyncio
async def test_run_scrape_proceeds_when_job_location_geocodes(monkeypatch):
    """Control: a resolvable location must NOT be blocked by the pre-flight."""
    reached_browser = False

    async def fake_browser(*a, **k):
        nonlocal reached_browser
        reached_browser = True
        raise RuntimeError("stop here — we only care that the pre-flight let us through")

    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: (51.9532, 7.9912))
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)

    result = await main_mod.run_scrape(_job("Warendorf"))

    assert reached_browser is True, "a geocodable job must not be aborted"
    assert result.partial is True  # from the RuntimeError above, not the pre-flight

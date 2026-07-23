"""End-to-end guard for the eval-error handling in main.run_scrape.

Prod incident 2026-07-22: OpenRouter returned 402 Payment Required on every
call. evaluate_candidate returned EvalResult(match=False, ...) for each — which
was indistinguishable from a genuine "no match" verdict — so every candidate was
appended to result.candidates, sent to the webhook, and logged by n8n into the
Airtable dedup table. Those candidates are now skipped forever, never getting a
real evaluation once OpenRouter is funded.

The invariant these tests pin:
  * an eval that ERRORED must never enter result.candidates (so n8n can't log it),
  * it must be counted in candidates_eval_failed (so Slack can show it),
  * after EVAL_ERROR_ABORT_THRESHOLD consecutive errors the job aborts with a
    clear result.error, rather than walking the rest of the cards,
  * a genuine match=False verdict (error=False) is still emitted, unchanged.
"""
import pytest

import main as main_mod
from models.job import JobInput
from utils.openrouter import EvalResult


class _Card:
    """Minimal stand-in for scraper.search.SearchResult."""
    def __init__(self, profile_id):
        self.profile_id = profile_id
        self.preview_text = "Physiotherapeut mit 3 Jahren Berufserfahrung"
        self.cv_url = "https://example.test/cv"
        self.has_cv_attachment = True
        self.wohnort = "Berlin"
        self.gewuenschte_arbeitsorte = []


class _FakeBrowser:
    pass


def _job():
    return JobInput(
        offer_id="2468824", stage_id="13166770",
        job_title="Physiotherapeut (m/w/d)", location="Berlin",
        max_distance_km=25, max_candidates=50,
    )


def _wire_common(monkeypatch, cards):
    """Patch everything between run_scrape's entry and the eval loop so the test
    drives real cards through the real gate logic to the real eval-error branch.
    Only the eval itself differs between tests."""
    async def fake_browser(*a, **k):
        return _FakeBrowser(), object(), object()

    async def fake_auth(*a, **k):
        return None

    async def fake_close(*a, **k):
        return None

    async def fake_search(page, job_title, location, max_distance_km=25, keywords=None):
        return list(cards), 25

    async def fake_dup(*a, **k):
        return False  # nothing is a pre-unlock duplicate

    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: (52.52, 13.40))
    monkeypatch.setattr(main_mod, "select_account", lambda accounts, *a, **k: accounts[0])
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)
    monkeypatch.setattr(main_mod, "authenticate", fake_auth)
    monkeypatch.setattr(main_mod, "close_browser", fake_close)
    monkeypatch.setattr(main_mod, "search_candidates", fake_search)
    monkeypatch.setattr(main_mod, "check_duplicate", fake_dup)
    monkeypatch.setattr(main_mod, "calculate_distance_km", lambda w, j: 10.0)  # within radius → reaches eval


@pytest.mark.asyncio
async def test_errored_evals_are_not_emitted_and_abort_after_threshold(monkeypatch):
    """The 402 scenario: every eval errors. No candidate may enter
    result.candidates (so n8n can't burn them); all are counted; the job aborts
    after EVAL_ERROR_ABORT_THRESHOLD consecutive errors with a clear reason."""
    cards = [_Card(f"P{i}") for i in range(10)]
    _wire_common(monkeypatch, cards)

    calls = {"n": 0}

    async def fake_eval(**kwargs):
        calls["n"] += 1
        return EvalResult(error=True, reasoning="Error: OpenRouter returned 402")

    monkeypatch.setattr(main_mod, "evaluate_candidate", fake_eval)

    result = await main_mod.run_scrape(_job())

    # Nothing emitted — this is the whole point. n8n has nothing to log, so
    # nothing gets burned.
    assert result.candidates == [], "an errored eval must never emit a candidate"
    assert result.candidates_unlocked == 0
    # Every error is counted so Slack can show it…
    assert result.candidates_eval_failed == main_mod.EVAL_ERROR_ABORT_THRESHOLD
    # …and the job aborts at the threshold instead of walking all 10 cards.
    assert calls["n"] == main_mod.EVAL_ERROR_ABORT_THRESHOLD
    assert result.partial is True
    assert result.error, "an abort must record why"
    assert "OpenRouter" in result.error or "evaluation" in result.error.lower()


@pytest.mark.asyncio
async def test_a_single_error_skips_only_that_candidate(monkeypatch):
    """A lone transient error must skip just that candidate (not emitted, not
    counted against the cap) and let the run continue — not abort the job."""
    cards = [_Card("ERR"), _Card("GOOD")]
    _wire_common(monkeypatch, cards)

    async def fake_eval(**kwargs):
        if "Error" in kwargs["candidate_text"]:
            return EvalResult(error=True, reasoning="Error: evaluation timed out")
        # genuine non-match verdict (NOT an error) — must still be emitted
        return EvalResult(match=False, confidence=0.1, reasoning="Kein Treffer")

    # First card errors, second is a real verdict. Distinguish by text.
    cards[0].preview_text = "Error card"
    cards[1].preview_text = "Good card"
    monkeypatch.setattr(main_mod, "evaluate_candidate", fake_eval)

    result = await main_mod.run_scrape(_job())

    ids = [c.stepstone_profile_id for c in result.candidates]
    assert "ERR" not in ids, "the errored candidate must not be emitted"
    assert "GOOD" in ids, "a genuine no-match verdict must still be emitted (unchanged behavior)"
    assert result.candidates_eval_failed == 1
    assert result.partial is False, "one transient error must not abort the job"
    assert result.error == ""


@pytest.mark.asyncio
async def test_genuine_no_match_verdict_is_unchanged(monkeypatch):
    """Control: an error=False match=False verdict is emitted exactly as before,
    proving the fix only diverts *errors*, not verdicts."""
    cards = [_Card("A"), _Card("B")]
    _wire_common(monkeypatch, cards)

    async def fake_eval(**kwargs):
        return EvalResult(match=False, confidence=0.2, reasoning="Funktion passt nicht")

    monkeypatch.setattr(main_mod, "evaluate_candidate", fake_eval)

    result = await main_mod.run_scrape(_job())

    assert {c.stepstone_profile_id for c in result.candidates} == {"A", "B"}
    assert result.candidates_eval_failed == 0
    assert result.partial is False

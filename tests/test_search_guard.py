"""Tests for the search crash guard + autosuggest text-match preference.

Covers the pure/unit-testable pieces of scraper/search.py added after the
2026-07-03 prod incidents:
  - is_context_destroyed_error: narrow message-substring matcher for the
    patchright 'Execution context was destroyed' navigation error.
    picker used by the autosuggest click path (umlauts must pass through).
  - _scrape_cards_guarded: retry-once-then-empty behavior, verified
    deterministically by monkeypatching _scrape_cards + human_delay
    (no live browser needed — same pattern as test_search_fallback.py).
"""
import pytest

import scraper.search as search_mod
from scraper.search import is_context_destroyed_error


# ---------------------------------------------------------------------------
# is_context_destroyed_error
# ---------------------------------------------------------------------------

PROD_MESSAGE = (
    "Page.query_selector_all: Execution context was destroyed, "
    "most likely because of a navigation."
)


def test_context_destroyed_matches_prod_message_string():
    assert is_context_destroyed_error(PROD_MESSAGE) is True


def test_context_destroyed_matches_exception_instance():
    assert is_context_destroyed_error(Exception(PROD_MESSAGE)) is True


def test_context_destroyed_is_case_insensitive():
    assert is_context_destroyed_error("EXECUTION CONTEXT WAS DESTROYED") is True


def test_context_destroyed_rejects_other_errors():
    assert is_context_destroyed_error("Timeout 15000ms exceeded.") is False
    assert is_context_destroyed_error(RuntimeError("field not found")) is False
    assert is_context_destroyed_error("") is False


# ---------------------------------------------------------------------------
# _scrape_cards_guarded (retry once on context-destroyed, then empty list)
# ---------------------------------------------------------------------------

@pytest.fixture
def no_delay(monkeypatch):
    async def instant(*args, **kwargs):
        return None

    monkeypatch.setattr(search_mod, "human_delay", instant)


@pytest.mark.asyncio
async def test_guard_retries_once_and_succeeds(monkeypatch, no_delay):
    calls = []

    async def fake_scrape(page):
        calls.append(1)
        if len(calls) == 1:
            raise Exception(PROD_MESSAGE)
        return ["card1", "card2"]

    monkeypatch.setattr(search_mod, "_scrape_cards", fake_scrape)
    results = await search_mod._scrape_cards_guarded(None)
    assert results == ["card1", "card2"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_guard_returns_empty_when_retry_also_fails(monkeypatch, no_delay, caplog):
    calls = []

    async def fake_scrape(page):
        calls.append(1)
        raise Exception(PROD_MESSAGE)

    monkeypatch.setattr(search_mod, "_scrape_cards", fake_scrape)
    with caplog.at_level("WARNING", logger="scraper.search"):
        results = await search_mod._scrape_cards_guarded(None)
    assert results == []
    assert len(calls) == 2  # exactly ONE retry, no loop
    levels = [r.levelname for r in caplog.records]
    assert "WARNING" in levels and "ERROR" in levels


@pytest.mark.asyncio
async def test_guard_propagates_unrelated_errors(monkeypatch, no_delay):
    async def fake_scrape(page):
        raise RuntimeError("Timeout 15000ms exceeded.")

    monkeypatch.setattr(search_mod, "_scrape_cards", fake_scrape)
    with pytest.raises(RuntimeError, match="Timeout"):
        await search_mod._scrape_cards_guarded(None)


@pytest.mark.asyncio
async def test_guard_propagates_unrelated_error_on_retry(monkeypatch, no_delay):
    calls = []

    async def fake_scrape(page):
        calls.append(1)
        if len(calls) == 1:
            raise Exception(PROD_MESSAGE)
        raise RuntimeError("browser has been closed")

    monkeypatch.setattr(search_mod, "_scrape_cards", fake_scrape)
    with pytest.raises(RuntimeError, match="browser has been closed"):
        await search_mod._scrape_cards_guarded(None)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_guard_no_retry_on_clean_success(monkeypatch, no_delay):
    calls = []

    async def fake_scrape(page):
        calls.append(1)
        return []

    monkeypatch.setattr(search_mod, "_scrape_cards", fake_scrape)
    results = await search_mod._scrape_cards_guarded(None)
    assert results == []
    assert len(calls) == 1

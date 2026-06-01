"""Tests for the 0-results keyword fallback in search_candidates.

The fallback wraps _execute_search: if a keyworded search returns no cards,
retry once WITHOUT keywords. Verified deterministically by monkeypatching
_execute_search (no live browser needed).
"""
import pytest
import scraper.search as search_mod
from scraper.search import search_candidates


class _Card:
    pass


@pytest.mark.asyncio
async def test_fallback_retries_without_keywords_on_zero(monkeypatch):
    calls = []

    async def fake_execute(page, job_title, location, max_distance_km=25, keywords=None):
        calls.append(keywords)
        if keywords:           # first pass with keywords → 0 cards
            return [], 50
        return [_Card(), _Card()], 50  # retry without keywords → 2 cards

    monkeypatch.setattr(search_mod, "_execute_search", fake_execute)
    results, radius = await search_candidates(
        None, "Koch", "Berlin", max_distance_km=50, keywords=["Armatur"]
    )
    assert len(results) == 2
    assert radius == 50
    assert calls == [["Armatur"], None]  # tried WITH, then WITHOUT


@pytest.mark.asyncio
async def test_no_fallback_when_results_present(monkeypatch):
    calls = []

    async def fake_execute(page, job_title, location, max_distance_km=25, keywords=None):
        calls.append(keywords)
        return [_Card()], 50

    monkeypatch.setattr(search_mod, "_execute_search", fake_execute)
    results, _ = await search_candidates(None, "Koch", "Berlin", keywords=["Armatur"])
    assert len(results) == 1
    assert calls == [["Armatur"]]  # single call, no retry


@pytest.mark.asyncio
async def test_no_fallback_when_no_keywords(monkeypatch):
    calls = []

    async def fake_execute(page, job_title, location, max_distance_km=25, keywords=None):
        calls.append(keywords)
        return [], 50

    monkeypatch.setattr(search_mod, "_execute_search", fake_execute)
    results, _ = await search_candidates(None, "Koch", "Berlin", keywords=[])
    assert results == []
    assert calls == [[]]  # no retry: no keywords to drop

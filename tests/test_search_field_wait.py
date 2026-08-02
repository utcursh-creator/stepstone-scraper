"""Regression guards for the DirectSearch field-readiness crash.

Prod incident 2026-07-31, offer 2468458 ('Physiotherapeut (m/w/d)', Warendorf):
the whole scheduled batch aborted with

    RuntimeError: DirectSearch field #searchfield__textfield not found

5.43s after the search began. DirectSearch is an Angular SPA and the code
navigated with `wait_until="domcontentloaded"` — which fires when the HTML
shell is parsed, before Angular bootstraps — then gave the app a FIXED nap
(two human_delay calls, 3.0-5.5s total) and probed ONCE with query_selector,
which has no waiting semantics. That run had just completed a *fresh* login
(26.7s), so the app was bootstrapping cold on a brand-new proxy exit and lost
the race.

The invariants pinned here:
  * the field is AWAITED, never probed once — a regression to query_selector
    fails immediately because the fake page only yields it via wait_for_selector,
  * the wait is bounded and explicit (nothing above run_scrape bounds it),
  * only a patchright TimeoutError is swallowed — a dead page still propagates,
  * a genuine timeout names what the page ACTUALLY was, with PII redacted,
    because that string rides result.error into the operator's alert mail.
"""
import re

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper import search as search_mod
from scraper.search import (
    SEARCH_FIELD_SELECTOR,
    SEARCH_FIELD_SETTLE_TIMEOUT_MS,
    SEARCH_FIELD_TIMEOUT_MS,
    _redact,
    _safe_url,
    _wait_for_search_field,
)


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
    """A page whose search field is ONLY reachable by waiting for it.

    query_selector deliberately never returns the field: that is what makes a
    regression to the one-shot probe fail loudly instead of silently passing.
    """

    url = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"

    def __init__(self, *, field_appears=True, snapshot=None, raise_on_wait=None):
        self._field_appears = field_appears
        self._snapshot = snapshot or {}
        self._raise_on_wait = raise_on_wait
        self.waits = []

    async def goto(self, *a, **k):
        return None

    async def query_selector(self, selector, *a, **k):
        # The search field is NEVER available through the non-waiting probe.
        if SEARCH_FIELD_SELECTOR in selector:
            return None
        return None

    async def query_selector_all(self, *a, **k):
        return []

    async def wait_for_selector(self, selector, **kwargs):
        self.waits.append((selector, kwargs))
        if self._raise_on_wait is not None:
            raise self._raise_on_wait
        if not self._field_appears:
            raise PlaywrightTimeoutError("Timeout exceeded")
        return _FakeField()

    async def evaluate(self, *a, **k):
        return self._snapshot

    async def title(self):
        return "StepStone DirectSearch"

    async def add_style_tag(self, *a, **k):
        return None


# ------------------------------------------------------- the core invariant

@pytest.mark.asyncio
async def test_the_field_is_awaited_not_probed_once():
    """THE regression test. The fake page yields the field only via
    wait_for_selector, so reverting to query_selector fails here."""
    page = _FakePage()
    field = await _wait_for_search_field(page, SEARCH_FIELD_TIMEOUT_MS)

    assert field is not None, "the field must be reachable by waiting"
    selector, kwargs = page.waits[0]
    assert selector == SEARCH_FIELD_SELECTOR
    # 'visible', not 'attached': callers immediately click and type into it.
    assert kwargs.get("state") == "visible"
    # The budget must be explicit, never inherited from a global default.
    assert kwargs.get("timeout") is not None


@pytest.mark.asyncio
async def test_execute_search_no_longer_depends_on_the_one_shot_probe(monkeypatch):
    """End-to-end through the exact function that crashed in production."""
    page = _FakePage()

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)
    monkeypatch.setattr(search_mod, "_kill_cookie_banner", _no_delay)

    async def _fake_scrape_cards(*a, **k):
        return []

    async def _country_chip(*a, **k):
        return True

    monkeypatch.setattr(search_mod, "_scrape_cards_guarded", _fake_scrape_cards, raising=False)
    monkeypatch.setattr(search_mod, "_country_chip_present", _country_chip, raising=False)

    results, radius = await search_mod._execute_search(
        page, "Physiotherapeut (m/w/d)", "Warendorf", max_distance_km=25
    )

    assert results == []
    assert radius == 25


def test_timeouts_are_bounded_because_nothing_above_them_is():
    """run_scrape has no outer timeout and holds scrape_lock for the whole job,
    so every second spent waiting is a second n8n's next /scrape gets a 409."""
    assert SEARCH_FIELD_TIMEOUT_MS == 30_000
    assert SEARCH_FIELD_SETTLE_TIMEOUT_MS == 10_000
    # search_candidates may call _execute_search twice (0-results keyword fallback).
    assert 2 * SEARCH_FIELD_TIMEOUT_MS <= 60_000
    # The between-criteria budget is paid 2 + len(keywords) times per job, so it
    # must stay well under the cold-start budget.
    assert SEARCH_FIELD_SETTLE_TIMEOUT_MS < SEARCH_FIELD_TIMEOUT_MS


@pytest.mark.asyncio
async def test_only_a_timeout_is_swallowed_not_a_dead_page():
    """A destroyed execution context is a real failure, not 'field absent'."""
    page = _FakePage(raise_on_wait=RuntimeError("Execution context was destroyed"))

    with pytest.raises(RuntimeError, match="Execution context was destroyed"):
        await _wait_for_search_field(page, 1000)


# ------------------------------------------------------------- diagnostics

@pytest.mark.asyncio
async def test_error_names_the_page_instead_of_just_the_selector(monkeypatch):
    """The 2026-07-31 raise said only 'field not found', so a dead session, a
    slow bootstrap and a bot block were indistinguishable. This string reaches
    the operator's alert mail via result.error."""
    page = _FakePage(
        field_appears=False,
        snapshot={
            "ready_state": "complete",
            "angular_scopes": 0,
            "field_attached": False,
            "login_form": True,
            "consent_wall": False,
            "captcha": True,
            "result_cards": 0,
            "body": "Bitte melden Sie sich an",
        },
    )
    page.url = "https://www.stepstone.de/5/recruiterspace/login?token=SECRET123456&event=login"

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)
    monkeypatch.setattr(search_mod, "_kill_cookie_banner", _no_delay)

    with pytest.raises(RuntimeError) as exc:
        await search_mod._execute_search(page, "Physiotherapeut", "Warendorf")

    msg = str(exc.value)
    assert "SEARCH_FIELD_TIMEOUT" in msg
    assert "login_form=True" in msg
    assert "angular_scopes=0" in msg
    assert "captcha=True" in msg
    # The session token must never reach a log line or the webhook.
    assert "SECRET123456" not in msg
    assert "token=<redacted>" in msg
    # …but the diagnostic query key survives, so we know which view answered.
    assert "event=login" in msg


@pytest.mark.asyncio
async def test_diagnosis_survives_a_page_that_cannot_be_inspected(monkeypatch):
    """A diagnostic must never replace the error it describes."""

    class _HostilePage(_FakePage):
        async def title(self):
            raise RuntimeError("page closed")

        async def evaluate(self, *a, **k):
            raise RuntimeError("context destroyed")

    page = _HostilePage(field_appears=False)

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)
    monkeypatch.setattr(search_mod, "_kill_cookie_banner", _no_delay)

    with pytest.raises(RuntimeError, match="SEARCH_FIELD_TIMEOUT"):
        await search_mod._execute_search(page, "Physiotherapeut", "Warendorf")


@pytest.mark.parametrize(
    "raw,expected_absent",
    [
        ("Kontakt: max.mustermann@example.de", "max.mustermann@example.de"),
        ("Mobil 015123456789", "015123456789"),
        ("ProfileID 61140080", "61140080"),
        ("48231 Warendorf", "48231"),
    ],
)
def test_redact_masks_every_pii_shape_profile_py_extracts(raw, expected_absent):
    """This snippet travels to a public repo's logs and to the alert mail."""
    out = _redact(raw)
    assert expected_absent not in out


def test_redact_keeps_the_diagnostic_signal():
    """Short digit runs are the content this snippet exists to carry."""
    assert "403" in _redact("Access Denied 403")
    assert "Access Denied" in _redact("Access Denied 403")
    assert "25km" in _redact("Umkreis 25km")


def test_safe_url_never_raises_on_garbage():
    assert _safe_url("") == "://"
    assert isinstance(_safe_url("not a url at all"), str)
    # Whitelisted key survives; everything else is redacted.
    out = _safe_url("https://x.de/p?event=directsearchgen4&sid=abc123")
    assert "event=directsearchgen4" in out
    assert "abc123" not in out


# ------------------------------------------------- the other two call sites

@pytest.mark.asyncio
async def test_criterion_helper_waits_with_the_short_budget(monkeypatch):
    """A job pays this 2 + len(keywords) times — it must not be the 30s budget."""
    page = _FakePage()

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)

    await search_mod._add_criterion_via_autosuggest(page, "Physiotherapeut")

    _, kwargs = page.waits[0]
    assert kwargs["timeout"] == SEARCH_FIELD_SETTLE_TIMEOUT_MS


@pytest.mark.asyncio
async def test_criterion_helper_raises_a_diagnosed_error(monkeypatch):
    """Patching only _execute_search would leave this crash reachable seconds
    later with an indistinguishable message."""
    page = _FakePage(field_appears=False, snapshot={"angular_scopes": 3})

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)

    with pytest.raises(RuntimeError) as exc:
        await search_mod._add_criterion_via_autosuggest(page, "Physiotherapeut")

    msg = str(exc.value)
    assert "SEARCH_FIELD_LOST" in msg
    assert "Physiotherapeut" in msg, "the failing criterion must be named"


@pytest.mark.asyncio
async def test_lost_keyword_is_logged_not_silently_dropped(monkeypatch, caplog):
    """A silently widened search must never again be indistinguishable from a
    normal one — it costs more credits per useful candidate."""
    page = _FakePage(field_appears=False, snapshot={"angular_scopes": 3})

    async def _no_delay(*a, **k):
        return None

    monkeypatch.setattr(search_mod, "human_delay", _no_delay)

    with caplog.at_level("WARNING"):
        ok = await search_mod._add_keyword_criterion(page, "Archivierung")

    assert ok is False, "a lost keyword stays non-fatal"
    assert "Archivierung" in caplog.text
    assert "BROADER" in caplog.text

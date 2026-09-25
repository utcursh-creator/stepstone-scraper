"""Unlocking a Talent Finder candidate: the step that spends a StepStone credit.

Contract read from Stepstone Recruit's public JavaScript (2026-09-25): the
Freischalten button sends POST /talent/unlock {talentId, uniqueSearchCriteriaId}
with no confirmation dialog; the answer carries personalInfo and
alreadyUnlocked; no credits is a 403 with creditState.

What these tests pin, in order of how much money they protect:
  * credit_spent follows StepStone (alreadyUnlocked), never "did we get data"
  * an unclear outcome is settled by StepStone's unlock history, and an
    unverifiable one is counted as SPENT (a counter below real spend lets the
    daily cap authorise extra unlocks; the scraper/profile.py lesson)
  * no credits / no session STOPS the job instead of failing every candidate
  * a CV that cannot be fetched never loses the contact that was paid for
  * no contact data is written to the logs
"""
import base64

import pytest

import main as main_mod
from scraper import talent_unlock as tu
from scraper.talent_search import SearchResult
from scraper.talent_unlock import UnlockError, unlock_talent

TALENT = "712fdd83-7c9d-450e-a3f1-9bb942c373a6"
PDF = base64.b64encode(b"%PDF-1.7\n" + b"0" * 200).decode()
PERSON = {"firstName": "Maxima", "lastName": "Muster", "email": "maxima@example.test",
          "mobilePhoneNumber": "+49 151 0000000",
          "address": {"city": "Hamburg", "postalCode": "22589", "country": "DE"}}


def _candidate(has_cv=True, locked=True):
    return SearchResult(profile_id=TALENT, preview_text="Aktuelle Position: Physiotherapeutin",
                        wohnort="22589 Hamburg", has_cv_attachment=has_cv, is_locked=locked,
                        display_name="Maxima M.")


class FakePage:
    """Answers the page-world requests like StepStone. `unlock` is a list of
    answers consumed in order; `balances` likewise for GET /credits."""

    def __init__(self, unlock, balances=(147, 146), history=None, cv=None):
        self.unlock = list(unlock)
        self.balances = list(balances)
        self.history = history
        self.cv = {"status": 200, "base64": PDF, "size": 209} if cv is None else cv
        self.calls = []

    async def evaluate(self, expression, arg=None, *, isolated_context=True):
        self.calls.append({"isolated": isolated_context, **arg})
        url, method = arg["url"], arg.get("method", "GET")
        if url == tu.CREDITS_PATH:
            b = self.balances.pop(0) if self.balances else None
            return {"status": 200, "body": {"remainingCredits": b}} if b is not None else {"status": 500}
        if url == tu.UNLOCK_PATH and method == "POST":
            return self.unlock.pop(0)
        if "/unlocks/latest" in url:
            return self.history if self.history is not None else {"status": 500}
        if "/cv/view" in url:
            return self.cv
        raise AssertionError(f"unexpected request {method} {url}")

    def unlock_calls(self):
        return [c for c in self.calls if c["url"] == tu.UNLOCK_PATH]


def _ok(already=False, person=PERSON):
    return {"status": 200, "body": {"personalInfo": person, "alreadyUnlocked": already}}


def _history(minutes_ago=1, current_user=True):
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"status": 200, "body": {"talentId": TALENT, "actions": [
        {"actionType": "UNLOCKED", "actionDate": when, "isCurrentUser": current_user}]}}


async def test_an_unlock_sends_what_the_button_sends_and_returns_the_contact():
    page = FakePage(unlock=[_ok()])

    result, spent = await unlock_talent(page, _candidate(), "Account 1", criteria_id=68799)

    assert spent is True and result.unlock_reason == "success" and result.unlocked
    call = page.unlock_calls()[0]
    assert call["isolated"] is False and call["method"] == "POST"
    assert call["body"] == {"talentId": TALENT, "uniqueSearchCriteriaId": 68799}
    assert (result.name, result.email, result.phone) == ("Maxima Muster", "maxima@example.test", "+49 151 0000000")
    assert result.profile_text.startswith("Wohnadresse 22589 Hamburg\n"), \
        "the post-unlock distance gate reads the home address from this line"
    assert result.cv_base64 == PDF and result.cv_filename == "Lebenslauf.pdf"


async def test_an_already_unlocked_candidate_is_free_and_says_so():
    page = FakePage(unlock=[_ok(already=True)], balances=(147, 147))
    result, spent = await unlock_talent(page, _candidate(locked=False), "Account 1", 68799)
    assert spent is False and result.unlock_reason == "already_unlocked"


async def test_no_credits_stops_the_job_and_costs_nothing():
    page = FakePage(unlock=[{"status": 403, "body": {"creditState": "NO_CREDITS"}}])
    with pytest.raises(UnlockError) as err:
        await unlock_talent(page, _candidate(), "Account 1", 68799)
    assert err.value.code == "UNLOCK_NO_CREDITS" and "NO_CREDITS" in str(err.value)


async def test_a_zero_balance_stops_before_any_unlock_is_sent():
    page = FakePage(unlock=[_ok()], balances=(0,))
    with pytest.raises(UnlockError) as err:
        await unlock_talent(page, _candidate(), "Account 1", 68799)
    assert err.value.code == "UNLOCK_NO_CREDITS"
    assert page.unlock_calls() == [], "not even one request on an empty balance"


async def test_a_zero_balance_still_reads_an_already_unlocked_candidate():
    page = FakePage(unlock=[_ok(already=True)], balances=(0, 0))
    result, spent = await unlock_talent(page, _candidate(locked=False), "Account 1", 68799)
    assert result is not None and spent is False


async def test_a_lost_session_stops_the_job():
    page = FakePage(unlock=[{"status": 401, "body": None}])
    with pytest.raises(UnlockError) as err:
        await unlock_talent(page, _candidate(), "Account 1", 68799)
    assert err.value.code == "UNLOCK_SESSION_LOST"


async def test_an_unclear_unlock_that_stepstone_did_charge_is_read_back_for_free():
    """A 5xx/timeout AFTER the charge: the history proves it, and a second
    request returns the contact as alreadyUnlocked. The credit is still ours."""
    page = FakePage(unlock=[{"status": 504, "body": None}, _ok(already=True)], history=_history())
    result, spent = await unlock_talent(page, _candidate(), "Account 1", 68799)
    assert spent is True and result.unlock_reason == "success"
    assert result.email == "maxima@example.test"
    assert len(page.unlock_calls()) == 2


async def test_an_unclear_unlock_that_stepstone_did_not_charge_costs_nothing():
    page = FakePage(unlock=[{"status": 504, "body": None}], history=_history(minutes_ago=120))
    assert await unlock_talent(page, _candidate(), "Account 1", 68799) == (None, False)
    assert len(page.unlock_calls()) == 1, "never retried into a second charge"


async def test_an_unverifiable_unlock_is_counted_as_spent():
    page = FakePage(unlock=[{"status": 0, "error": "AbortError"}], history=None)
    assert await unlock_talent(page, _candidate(), "Account 1", 68799) == (None, True)


async def test_a_cv_that_cannot_be_fetched_never_loses_the_paid_contact():
    for cv in ({"status": 500}, {"status": 0, "error": "AbortError"},
               {"status": 200, "base64": base64.b64encode(b"<html>" + b" " * 200).decode()}):
        page = FakePage(unlock=[_ok()], cv=cv)
        result, spent = await unlock_talent(page, _candidate(), "Account 1", 68799)
        assert spent is True and result.email and result.cv_base64 is None


async def test_no_cv_request_for_a_candidate_without_a_cv():
    page = FakePage(unlock=[_ok()])
    await unlock_talent(page, _candidate(has_cv=False), "Account 1", 68799)
    assert not [c for c in page.calls if "/cv/view" in c["url"]]


async def test_a_balance_that_disagrees_is_logged_but_the_response_is_trusted(caplog):
    page = FakePage(unlock=[_ok()], balances=(147, 147))
    _, spent = await unlock_talent(page, _candidate(), "Account 1", 68799)
    assert spent is True and "Credit cross-check" in caplog.text


async def test_contact_details_never_reach_the_logs(caplog):
    caplog.set_level("INFO")
    await unlock_talent(FakePage(unlock=[_ok()]), _candidate(), "Account 1", 68799)
    for secret in ("maxima@example.test", "0000000", "Muster"):
        assert secret not in caplog.text


# ------------------------------------------------------------- main.py

def _wire_unlock(monkeypatch, tmp_path, unlock_behaviour):
    from tests.test_eval_error import _Card, _wire_common
    from utils.openrouter import EvalResult

    cards = [_Card("A"), _Card("B")]
    _wire_common(monkeypatch, cards)
    monkeypatch.setattr(main_mod, "UNLOCK_COUNTER_PATH", str(tmp_path / "unlocks.json"))

    async def match(**kwargs):
        return EvalResult(match=True, confidence=0.9, reasoning="passt")
    monkeypatch.setattr(main_mod, "evaluate_candidate", match)

    async def not_in_recruitee(**kwargs):
        return False, None, []
    monkeypatch.setattr(main_mod, "check_candidate_exists_in_recruitee", not_in_recruitee)

    async def no_push(**kwargs):
        return None
    monkeypatch.setattr(main_mod, "_push_to_recruitee", no_push)

    calls = []

    async def fake_unlock(page, candidate, account_label, criteria_id):
        calls.append(candidate.profile_id)
        outcome = unlock_behaviour(candidate.profile_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(main_mod, "unlock_talent", fake_unlock)
    return calls


def _unlocked(pid, spent):
    from models.candidate import CandidateResult
    return CandidateResult(name="X Y", stepstone_profile_id=pid, email="x@example.test", unlocked=True,
                           unlock_reason="success" if spent else "already_unlocked", account_used="Account 1",
                           cv_base64=PDF, cv_filename="Lebenslauf.pdf"), spent


async def test_credit_spent_reaches_the_webhook_and_a_free_unlock_is_not_counted(monkeypatch, tmp_path):
    _wire_unlock(monkeypatch, tmp_path, lambda pid: _unlocked(pid, spent=(pid == "A")))

    result = await main_mod.run_scrape(__import__("tests.test_eval_error", fromlist=["_job"])._job())

    by_id = {c.stepstone_profile_id: c for c in result.candidates}
    assert by_id["A"].credit_spent is True and by_id["B"].credit_spent is False
    assert result.credits_spent == 1, "n8n logs the Credit Ledger from this, never from `unlocked`"
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert main_mod.unlock_budget.unlocks_today(str(tmp_path / "unlocks.json"), today) == 1, \
        "the daily cap counts the paid unlock only"


async def test_running_out_of_credits_stops_the_job_with_its_code(monkeypatch, tmp_path):
    calls = _wire_unlock(monkeypatch, tmp_path,
                         lambda pid: UnlockError("creditState=NO_CREDITS", code="UNLOCK_NO_CREDITS"))

    result = await main_mod.run_scrape(__import__("tests.test_eval_error", fromlist=["_job"])._job())

    assert calls == ["A"], "no further unlock is attempted after the first refusal"
    assert result.partial is True and result.error.startswith("UNLOCK_NO_CREDITS")

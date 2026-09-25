"""Talent Finder search (scraper/talent_search.py).

The contract was read from the live app (structure-only probe, 2026-09-25) and
from its public JavaScript. These tests pin the parts that decide what the
scraper asks StepStone for and what it does with the answer:
  * the query: job title without gender markers, job keywords as MUSTs,
    StepStone's boolean syntax kept intact
  * the radius: StepStone only offers 40/60/80/100/150 km; the smallest that
    COVERS the job is sent, the exact limit stays local
  * the request runs in the PAGE world (the isolated world was refused live)
  * a genuine zero is an outcome; a failed search is a SearchError with a code
    (2026-08-10: six failed searches looked exactly like six empty ones)
  * pages load lazily, so a job that fills its cap early requests nothing more
  * what reaches the LLM carries no name, email or phone
"""
import json

import pytest

from scraper import talent_search as ts
from scraper.talent_search import SearchError, search_talents


def _record(i=0, **overrides):
    """Shaped like a live /search content[] record (field names from the probe)."""
    rec = {
        "id": f"00000000-0000-4000-8000-{i:012d}",
        "score": 0.87,
        "currentJobTitle": "Physiotherapeutin",
        "lastActivity": "2026-09-24T10:00:00Z",
        "cvUpdated": "2026-06-01T00:00:00Z",
        "hasCv": True,
        "isLocked": True,
        "jobPreferences": {"desiredSalary": None, "desiredJobLocations": ["Hamburg", {"name": "Lübeck"}],
                           "desiredJobTitles": ["Physiotherapeut/in"], "desiredWorkTypes": ["FULLTIME"],
                           "desiredContractTypes": [], "remoteWorkTypes": []},
        "languages": [{"languageId": "deutsch", "level": "B1"}, {"languageId": "englisch", "level": "C1"}],
        "personalInfo": {"firstName": "Maxima", "lastName": "M.", "email": None, "mobilePhoneNumber": None,
                         "address": {"city": "Hamburg", "country": "DE", "postalCode": "22589"}},
        "workExperiences": [{"id": None, "companyName": "Reha Beispielstadt", "jobTitle": "Physiotherapeutin",
                             "startDate": "2025-01", "endDate": None, "isCurrentJob": True,
                             "jobDescription": "Manuelle Therapie", "tasks": []}],
        "matchingExperienceCount": 2,
        "relevantMonthsCount": 53,
        "jobSearchUrgency": "OPEN_TO_OFFERS",
        "hasDrivingLicense": True,
        "drivingLicenses": ["B"],
        "skills": ["Manuelle Therapie", "Lymphdrainage"],
        "educations": [{"id": None, "courseTitle": "Physiotherapie", "institution": "Hochschule Beispiel",
                        "completionYear": 2019, "qualification": "Bachelor"}],
    }
    rec.update(overrides)
    return rec


def _page(records, total, number=0, size=20):
    return {"content": records, "totalElements": total, "totalPages": -(-total // size) if total else 0,
            "number": number, "size": size, "uniqueSearchCriteriaId": 68799}


class FakePage:
    """Answers the page-world fetch() the way StepStone did, and records calls."""

    def __init__(self, pages=None, suggestions=None, url="https://recruit.stepstone.com/talent-sourcing"):
        self.url = url
        self.pages = pages or {}           # page_number -> response dict, or list of them (consumed in order)
        self.suggestions = ["Hamburg"] if suggestions is None else suggestions
        self.calls = []
        self.gotos = []

    async def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    async def evaluate(self, expression, arg=None, *, isolated_context=True):
        self.calls.append({"isolated": isolated_context, **arg})
        url = arg["url"]
        if url.startswith(ts.LOCATION_SUGGEST_PATH):
            if isinstance(self.suggestions, dict):
                return self.suggestions
            return {"status": 200, "body": self.suggestions}
        n = int(url.split("pageNumber=")[1].split("&")[0])
        answer = self.pages.get(n, {"status": 200, "body": _page([], 0, n)})
        if isinstance(answer, list):
            answer = answer.pop(0)
        return answer

    def search_calls(self):
        return [c for c in self.calls if c["url"].startswith(ts.SEARCH_PATH)]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(*a, **k):
        return None
    monkeypatch.setattr(ts, "human_delay", instant)


def _ok(body):
    return {"status": 200, "body": body}


# ------------------------------------------------------------------ query

def test_the_job_title_loses_its_gender_marker_and_keywords_become_musts():
    assert ts.build_keyword("Physiotherapeut (m/w/d)") == "Physiotherapeut"
    assert ts.build_keyword("SAP Consultant Archivierung (m/w/d)", ["Archivierung"]) == \
        "(SAP Consultant Archivierung) AND Archivierung"
    assert ts.build_keyword("Pflegefachkraft Wundversorgung (m/w/d) Homecare", ["Wunden"]) == \
        "(Pflegefachkraft Wundversorgung Homecare) AND Wunden"


def test_boolean_words_and_syntax_characters_cannot_change_the_query():
    """'and' in a title must not become StepStone's AND; quotes and brackets are syntax."""
    assert ts.build_keyword("Sales and Marketing Manager (w/m/d)", ["Key Account", "SAP"]) == \
        '"Sales and Marketing Manager" AND "Key Account" AND SAP'
    assert ts.build_keyword('Koch "Chef" (m/w/d)', ["(Kälte)"]) == "(Koch Chef) AND Kälte"


@pytest.mark.parametrize("wanted, sent", [(0, 0), (10, 40), (25, 40), (40, 40), (50, 60),
                                          (75, 80), (100, 100), (120, 150), (500, 150)])
def test_the_smallest_radius_that_covers_the_job_is_sent(wanted, sent):
    assert ts.backend_radius_km(wanted) == sent


# ---------------------------------------------------------------- mapping

def test_a_record_becomes_what_the_pre_unlock_gates_read():
    r = ts.to_search_result(_record())
    assert r.profile_id == "00000000-0000-4000-8000-000000000000"
    assert r.wohnort == "22589 Hamburg", "postcode + city: a far better distance gate than a city name"
    assert r.has_cv_attachment is True and r.is_locked is True
    assert r.gewuenschte_arbeitsorte == ["Hamburg", "Lübeck"]
    assert r.languages == [("deutsch", "B1"), ("englisch", "C1")]
    assert r.relevant_months == 53 and r.score == 0.87
    assert r.profile_url.endswith("/talent-sourcing/results/00000000-0000-4000-8000-000000000000")


def test_what_the_llm_reads_has_the_facts_and_no_personal_identifiers():
    rec = _record(personalInfo={"firstName": "Maxima", "lastName": "M.", "email": "maxima@example.test",
                                "mobilePhoneNumber": "+49 151 0000000",
                                "address": {"city": "Hamburg", "country": "DE", "postalCode": "22589"}})
    text = ts.to_search_result(rec).preview_text
    for fact in ("Physiotherapeutin bei Reha Beispielstadt (2025-01 – heute)", "Bachelor: Physiotherapie",
                 "Sprachen: deutsch (B1), englisch (C1)", "Wohnort: 22589 Hamburg",
                 "Lebenslauf vorhanden: ja", "Relevante Berufserfahrung laut StepStone: 53 Monate"):
        assert fact in text
    for identifier in ("Maxima", "maxima@example.test", "0000000"):
        assert identifier not in text


def test_a_record_without_an_id_is_dropped_and_unknown_lock_state_counts_as_locked():
    assert ts.to_search_result(_record(id=None)) is None
    assert ts.to_search_result(_record(isLocked=None)).is_locked is True
    assert ts.to_search_result(_record(isLocked=False)).is_locked is False


def test_a_foreign_address_keeps_its_country_for_the_distance_gate():
    rec = _record(personalInfo={"address": {"city": "Wien", "country": "AT", "postalCode": "1010"}})
    assert ts.to_search_result(rec).wohnort == "1010 Wien, AT"


# -------------------------------------------------------------- transport

async def test_the_search_runs_in_the_page_world_with_the_radius_that_covers_the_job():
    page = FakePage(pages={0: _ok(_page([_record(0), _record(1)], 2))})

    outcome = await search_talents(page, "Physiotherapeut (m/w/d)", "Hamburg", max_distance_km=25)

    assert outcome.total == 2 and [r.profile_id[-1] for r in outcome.first_page] == ["0", "1"]
    call = page.search_calls()[0]
    assert call["isolated"] is False, "the isolated world was refused on the live site"
    assert call["method"] == "POST" and "size=20&pageNumber=0&sortOrder=RELEVANCE" in call["url"]
    assert call["body"] == {"keyword": "Physiotherapeut", "rawQuery": "Physiotherapeut",
                            "locations": [{"locationName": "Hamburg", "radius": 40}]}


async def test_optimiert_sends_no_radius_at_all():
    page = FakePage(pages={0: _ok(_page([_record()], 1))})
    await search_talents(page, "Physiotherapeut", "Hamburg", max_distance_km=0)
    assert page.search_calls()[0]["body"]["locations"] == [{"locationName": "Hamburg"}]


async def test_a_genuine_zero_is_an_outcome_not_an_error():
    page = FakePage(pages={0: _ok(_page([], 0))})
    outcome = await search_talents(page, "Physiotherapeut", "Hamburg")
    assert outcome.total == 0 and outcome.first_page == []
    assert [r async for r in outcome.iterate()] == []


async def test_zero_with_keywords_retries_once_without_them():
    page = FakePage(pages={0: [_ok(_page([], 0)), _ok(_page([_record()], 1))]})
    outcome = await search_talents(page, "Mechatroniker (m/w/d)", "Hamburg", keywords=["Kälte"])
    keywords_sent = [c["body"]["keyword"] for c in page.search_calls()]
    assert keywords_sent == ["Mechatroniker AND Kälte", "Mechatroniker"]
    assert outcome.keyword_fallback is True and outcome.total == 1


async def test_later_pages_load_only_when_the_job_still_needs_candidates():
    page = FakePage(pages={0: _ok(_page([_record(i) for i in range(20)], 45)),
                           1: _ok(_page([_record(20 + i) for i in range(20)], 45, 1)),
                           2: _ok(_page([_record(40 + i) for i in range(5)], 45, 2))})
    outcome = await search_talents(page, "Physiotherapeut", "Hamburg")
    assert len(page.search_calls()) == 1, "only the first page up front"

    taken = []
    async for r in outcome.iterate():
        taken.append(r)
        if len(taken) == 21:   # the job's cap is reached one into page 2
            break
    assert len(page.search_calls()) == 2, "page 3 must never be requested"

    everything = [r async for r in (await search_talents(FakePage(pages=page.pages), "Physiotherapeut", "Hamburg")).iterate()]
    assert len(everything) == 45


async def test_no_more_than_max_pages_are_ever_requested():
    pages = {n: _ok(_page([_record(n * 20 + i) for i in range(20)], 367, n)) for n in range(19)}
    page = FakePage(pages=pages)
    outcome = await search_talents(page, "Physiotherapeut", "Hamburg", max_pages=3)
    assert len([r async for r in outcome.iterate()]) == 60
    assert len(page.search_calls()) == 3


@pytest.mark.parametrize("status, code", [(401, "SEARCH_SESSION_LOST"), (403, "SEARCH_SESSION_LOST"),
                                          (429, "SEARCH_RATE_LIMITED"), (400, "SEARCH_REJECTED")])
async def test_a_refused_search_is_an_error_with_a_code(status, code):
    page = FakePage(pages={0: {"status": status, "body": {"message": "nope"}}})
    with pytest.raises(SearchError) as err:
        await search_talents(page, "Physiotherapeut", "Hamburg")
    assert err.value.code == code and str(err.value).startswith(code)


async def test_a_server_error_is_retried_once_then_reported():
    page = FakePage(pages={0: [{"status": 503, "body": None}, _ok(_page([_record()], 1))]})
    assert (await search_talents(page, "Physiotherapeut", "Hamburg")).total == 1

    page = FakePage(pages={0: [{"status": 503, "body": None}, {"status": 0, "error": "net::ERR_FAILED"}]})
    with pytest.raises(SearchError) as err:
        await search_talents(page, "Physiotherapeut", "Hamburg")
    assert err.value.code == "SEARCH_UNAVAILABLE"
    assert len(page.search_calls()) == 2, "exactly one retry"


async def test_results_announced_but_not_delivered_are_not_a_zero():
    page = FakePage(pages={0: _ok(_page([], 12))})
    with pytest.raises(SearchError) as err:
        await search_talents(page, "Physiotherapeut", "Hamburg")
    assert err.value.code == "SEARCH_BAD_RESPONSE"


async def test_a_location_stepstone_does_not_know_fails_loudly_instead_of_searching_zero():
    page = FakePage(suggestions=[])
    with pytest.raises(SearchError) as err:
        await search_talents(page, "Physiotherapeut", "Wölfersheim OT Wohnbach")
    assert err.value.code == "SEARCH_LOCATION_UNKNOWN"
    assert not page.search_calls(), "no search may run on an unknown location"


async def test_the_location_uses_stepstones_own_spelling():
    page = FakePage(suggestions=[{"name": "Hamburg-Altona"}, {"name": "Hamburg"}],
                    pages={0: _ok(_page([_record()], 1))})
    await search_talents(page, "Physiotherapeut", "hamburg")
    assert page.search_calls()[0]["body"]["locations"][0]["locationName"] == "Hamburg"


async def test_if_suggestions_are_unavailable_the_location_is_searched_as_typed():
    page = FakePage(suggestions={"status": 503, "body": None}, pages={0: _ok(_page([_record()], 1))})
    await search_talents(page, "Physiotherapeut", "Hamburg")
    assert page.search_calls()[0]["body"]["locations"][0]["locationName"] == "Hamburg"


async def test_it_opens_talent_finder_first_when_the_page_is_elsewhere():
    page = FakePage(url="https://recruit.stepstone.com/", pages={0: _ok(_page([_record()], 1))})
    await search_talents(page, "Physiotherapeut", "Hamburg")
    assert page.gotos == ["https://recruit.stepstone.com/talent-sourcing"]


# ------------------------------------------------------------- main.py

async def test_a_failed_search_reaches_n8n_as_an_error_not_as_an_empty_run(monkeypatch, caplog):
    """2026-08-10: six failed searches reached the client's Slack as six green
    zeros. A SearchError must set result.error (with its code) and stop the job."""
    import main as main_mod
    from tests.test_eval_error import _job, _wire_common

    _wire_common(monkeypatch, [])

    async def failing(*a, **k):
        raise SearchError("StepStone answered 401", code="SEARCH_SESSION_LOST")
    monkeypatch.setattr(main_mod, "search_talents", failing)

    result = await main_mod.run_scrape(_job())

    assert result.partial is True
    assert result.error.startswith("SEARCH_SESSION_LOST")
    assert result.candidates == []
    assert "Scrape error" not in caplog.text, \
        "the job must stop cleanly at the failed search, not crash further down"


async def test_a_genuine_empty_search_is_a_clean_run(monkeypatch):
    import main as main_mod
    from tests.test_eval_error import _job, _wire_common
    _wire_common(monkeypatch, [])   # the shared fake search returns total 0

    result = await main_mod.run_scrape(_job())

    assert result.partial is False and not result.error

"""The Talent Finder search in a REAL headless Chromium, against the fake
StepStone of tests/test_auth_recruit.py. The unit tests use a fake page; this
proves what they cannot: patchright's page-world evaluate really sends a JSON
POST with the session cookie, and the answer comes back parsed."""
from scraper import talent_search as ts
from scraper.auth import authenticate
from scraper.talent_search import search_talents
from tests.test_auth_recruit import EMAIL, PASSWORD, site  # noqa: F401  (fixture)


async def test_a_real_browser_search_after_login_pages_through_the_results(site, monkeypatch):  # noqa: F811
    fake, context, page, _ = site

    async def instant(*a, **k):
        return None
    monkeypatch.setattr(ts, "human_delay", instant)

    await authenticate(context, page, EMAIL, PASSWORD)
    outcome = await search_talents(page, "Physiotherapeut (m/w/d)", "Hamburg", max_distance_km=25)
    results = [r async for r in outcome.iterate()]

    assert outcome.total == 27 and len(results) == 27
    assert results[0].wohnort == "22589 Hamburg" and results[0].has_cv_attachment
    assert [r["page_number"] for r in fake.search_requests] == [0, 1]
    first = fake.search_requests[0]
    assert first["method"] == "POST" and first["cookie_ok"], "the session cookie must ride along"
    assert first["json"] == {"keyword": "Physiotherapeut", "rawQuery": "Physiotherapeut",
                             "locations": [{"locationName": "Hamburg", "radius": 40}]}

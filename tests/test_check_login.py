"""check_login.py drives the real authenticate() and must report the outcome in
one line without ever printing the password. Exercised against the same fake
StepStone as tests/test_auth_recruit.py, in a real headless Chromium."""
import json

import pytest

import check_login
from tests.test_auth_recruit import EMAIL, PASSWORD, site  # noqa: F401  (fixture)


async def test_a_successful_check_reports_the_balance(site):  # noqa: F811
    fake, context, page, _ = site
    ok, line = await check_login.run_check(context, page, EMAIL, PASSWORD)
    assert ok is True
    assert line.startswith(f"LOGGED IN as {EMAIL}: 147 credits left until")
    assert PASSWORD not in line


async def test_a_failed_check_names_the_code_and_never_the_password(site):  # noqa: F811
    fake, context, page, _ = site
    fake.scenario = "captcha"
    ok, line = await check_login.run_check(context, page, EMAIL, PASSWORD)
    assert ok is False
    assert line.startswith("FAILED [LOGIN_CAPTCHA]")
    assert PASSWORD not in line


async def test_missing_credentials_exit_2_without_opening_a_browser(monkeypatch, capsys):
    monkeypatch.setattr(check_login, "load_dotenv", lambda: None)
    monkeypatch.delenv("STEPSTONE_EMAIL_2", raising=False)
    monkeypatch.delenv("STEPSTONE_PASS_2", raising=False)
    assert await check_login.main(["--account", "2"]) == 2
    assert "STEPSTONE_EMAIL_2" in capsys.readouterr().err


def test_variables_are_found_in_either_case_like_the_scraper_reads_them(monkeypatch):
    """Railway stores several of this service's variables in lowercase. The
    scraper (pydantic-settings) reads them case-insensitively; so must the check."""
    monkeypatch.delenv("STEPSTONE_EMAIL_1", raising=False)
    monkeypatch.setenv("stepstone_email_1", "lower@example.test")
    monkeypatch.setenv("PROXY_HOST", "proxy.test")
    assert check_login._env("STEPSTONE_EMAIL_1") == "lower@example.test"
    assert check_login._env("proxy_host") == "proxy.test"
    assert check_login._env("NOT_SET_ANYWHERE", "fallback") == "fallback"


async def test_inspection_reports_the_apps_headers_our_requests_and_the_form(site):  # noqa: F811
    """Names only, never values; both of our request routes are tried; the
    search form is read from the Talent Finder page."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"
    ok, _ = await check_login.run_check(context, page, EMAIL, PASSWORD)
    assert ok

    report = await check_login.inspect_after_login(page)

    app = [c for c in report["app_api_calls"] if c["path"].endswith("/credits")]
    assert app and "x-app-auth" in app[0]["header_names"] and app[0]["via"] == "xhr"
    assert report["our_balance_request_from_isolated_world"] == "401"
    assert report["our_balance_request_from_page_world"] == "401"
    assert any(n.startswith("tf_session") for n in report["app_host_cookie_names"])
    assert "yes" not in str(report["app_api_calls"]), "header VALUES must never be reported"
    placeholders = [i["placeholder"] for i in report["search_form"]["inputs"]]
    assert "Ort oder Postleitzahl eingeben" in placeholders
    assert "Umkreis: 25 km" in report["search_form_with_erweitert_open"]["distance_texts"]


async def test_a_third_fresh_login_within_an_hour_is_refused(monkeypatch, tmp_path, capsys):
    """Three fresh logins in ~25 min tripped StepStone's bot check (2026-09-25).
    The limit is enforced before any browser opens."""
    monkeypatch.setattr(check_login, "ATTEMPT_LOG", str(tmp_path / "attempts.json"))
    monkeypatch.setattr(check_login, "load_dotenv", lambda: None)
    monkeypatch.setenv("STEPSTONE_EMAIL_1", "a@example.test")
    monkeypatch.setenv("STEPSTONE_PASS_1", "x")
    now = check_login.time.time()
    (tmp_path / "attempts.json").write_text(json.dumps([now - 1500, now - 600]))

    assert await check_login.main(["--no-proxy"]) == 2
    assert "Refusing: 2 fresh logins already in the last hour" in capsys.readouterr().err


def test_attempts_older_than_an_hour_do_not_count(monkeypatch, tmp_path):
    monkeypatch.setattr(check_login, "ATTEMPT_LOG", str(tmp_path / "attempts.json"))
    now = check_login.time.time()
    (tmp_path / "attempts.json").write_text(json.dumps([now - 7200, now - 4000, now - 60]))
    assert check_login._recent_attempts(now) == [now - 60]


async def test_the_live_search_summary_holds_counts_and_never_candidate_data(site, monkeypatch):  # noqa: F811
    from scraper import talent_search
    from scraper.talent_search import search_talents

    async def instant(*a, **k):
        return None
    monkeypatch.setattr(talent_search, "human_delay", instant)
    fake, context, page, _ = site
    ok, _ = await check_login.run_check(context, page, EMAIL, PASSWORD)
    assert ok

    summary = check_login.search_summary(
        await search_talents(page, "Physiotherapeut (m/w/d)", "Hamburg", max_pages=1))

    assert summary["stepstone_total"] == 27 and summary["first_page_results"] == 20
    assert summary["with_postcode"] == 20 and summary["radius_sent_km"] == 40
    assert [r["page_number"] for r in fake.search_requests] == [0], "first page only"
    text = json.dumps(summary)
    assert "Test" not in text and "00000000-" not in text and "22589" not in text, "counts only"

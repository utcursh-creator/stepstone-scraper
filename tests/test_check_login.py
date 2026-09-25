"""check_login.py drives the real authenticate() and must report the outcome in
one line without ever printing the password. Exercised against the same fake
StepStone as tests/test_auth_recruit.py, in a real headless Chromium."""
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

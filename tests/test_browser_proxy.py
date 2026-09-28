from datetime import date

from scraper.browser import proxy_password


def test_every_job_of_the_day_uses_the_same_ip_for_an_account():
    a = proxy_password("pw", "DE", "Recruiter@Example.test", date(2026, 9, 30))
    b = proxy_password("pw", "DE", "recruiter@example.test", date(2026, 9, 30))
    assert a == b and a.startswith("pw_country-de_session-") and a.endswith("_lifetime-24h")


def test_accounts_and_days_get_their_own_ip():
    d = date(2026, 9, 30)
    assert proxy_password("pw", "DE", "a@example.test", d) != proxy_password("pw", "DE", "b@example.test", d)
    assert proxy_password("pw", "DE", "a@example.test", d) != proxy_password("pw", "DE", "a@example.test", date(2026, 10, 1))


def test_without_an_account_a_one_off_session_is_used():
    assert proxy_password("pw", "DE", None).endswith("_lifetime-10m")

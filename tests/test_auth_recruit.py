"""Login to Stepstone Recruit, exercised in a REAL headless Chromium.

StepStone retired DirectSearch in September 2026 (see scraper/auth.py). These
tests serve a fake StepStone from inside the browser (context.route), built
from the live login page's structure read on 2026-09-25:

  recruit.stepstone.com         Tealium cookie banner (#ccmgt_...), then a JS
                                redirect to the login host
  login.recruit.stepstone.com   one form: input[name=email],
                                input[name=password], submit, and an empty,
                                hidden div.captcha-container
  recruit.stepstone.com/callback  sets the session, back to the app
  .../api/v1/credits            200 {remainingCredits} only with a session

So real selectors, real typing, real clicks and real redirects run. No network,
no real account. The lessons of the old login stay pinned here:
  * login is PROVEN by the server (credits 200), never by "the form is gone"
    (2026-07-31: a blank page has no form either)
  * cookies are cleared before a fresh login, so one account's session never
    rides into another's (2026-08-03)
"""
import json

import pytest
from patchright.async_api import async_playwright

import main as main_mod
from models.job import JobInput
from scraper import auth as auth_mod
from scraper.auth import AuthenticationError, authenticate

EMAIL = "recruiter@example.test"
PASSWORD = "pw-Secret-123"
OTHER_EMAIL = "second@example.test"
OTHER_PASSWORD = "pw-Other-456"

CREDITS = {"remainingCredits": 147, "state": "ACTIVE", "untilDate": "2026-09-27T00:00:00Z",
           "contractRemainingCredits": None, "unlimited": False, "contractUnlimited": False}

APP_HTML = """<!doctype html><html><head><title>Stepstone Recruit</title></head><body>
<div id="cmMainContainer" style="display:none">
  <section id="explicit"><button id="ccmgt_explicit_preferences">Einstellungen oder ablehnen</button>
    <button id="ccmgt_explicit_accept">Alles akzeptieren</button></section>
  <section id="prefs" style="display:none"><button id="ccmgt_preferences_reject">Speichern und Beenden</button>
    <button id="ccmgt_preferences_accept">Alles akzeptieren</button></section>
</div>
<div id="app"></div>
<script>
const val = (n) => (document.cookie.split('; ').find((c) => c.startsWith(n + '=')) || '').split('=')[1];
function boot() {
  if (location.pathname === '/callback') {
    document.cookie = 'tf_session=' + new URLSearchParams(location.search).get('code') + '; path=/';
    location.replace('/'); return;
  }
  if (val('tf_session') && val('tf_session') !== 'expired') {
    document.getElementById('app').innerHTML = '<nav><a href="/talent-sourcing">Talent Finder</a></nav>';
    // Like the real app: the dashboard never loads the balance; Talent Finder
    // does, with a header the app adds itself (a bare fetch does not carry it).
    if (location.pathname.startsWith('/talent-sourcing')) {
      document.getElementById('app').innerHTML += '<div data-testid="search-box">'
        + '<input type="text" placeholder="Jobtitel, Stichwort oder boolesche Operatoren ein">'
        + '<input type="text" placeholder="Ort oder Postleitzahl eingeben">'
        + '<button aria-label="Suchen">S</button><button id="adv">Erweitert</button>'
        + '<div id="advpanel" style="display:none"><label>Umkreis: 25 km</label></div></div>';
      document.getElementById('adv').onclick = () => { document.getElementById('advpanel').style.display = 'block'; };
      const xhr = new XMLHttpRequest();  // the real app loads it with XHR, not fetch
      xhr.open('GET', '/recruiter/talent-sourcing/api/v1/credits');
      xhr.setRequestHeader('x-app-auth', 'yes');
      xhr.send();
    }
    return;
  }
  setTimeout(() => { location.href = '__LOGIN_TARGET__'; }, __REDIRECT_DELAY_MS__);
}
function consent(level) {
  document.cookie = 'consent_level=' + level + '; path=/';
  document.getElementById('cmMainContainer').style.display = 'none';
  boot();
}
document.getElementById('ccmgt_explicit_preferences').onclick = () => {
  document.getElementById('explicit').style.display = 'none';
  document.getElementById('prefs').style.display = 'block';
};
document.getElementById('ccmgt_preferences_reject').onclick = () => consent('essential');
document.getElementById('ccmgt_explicit_accept').onclick = () => consent('all');
document.getElementById('ccmgt_preferences_accept').onclick = () => consent('all');
if (val('consent_level')) boot(); else document.getElementById('cmMainContainer').style.display = 'block';
</script></body></html>"""

LOGIN_HTML = """<!doctype html><html><head><title>Stepstone Recruit</title></head><body>
<form>
  <div><input type="text" name="email" aria-label="E-Mail-Adresse"></div>
  <div><input type="password" name="password" aria-label="Passwort">
       <button type="button" aria-label="Passwort anzeigen">o</button></div>
  <div class="captcha-container" style="display: none;"></div>
  <div id="err"></div>
  <button type="submit">Jetzt einloggen</button>
</form>
<script>
const SCENARIO = '__SCENARIO__';
const ACCOUNTS = __ACCOUNTS__;
document.querySelector('form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const email = document.querySelector('[name=email]').value;
  const password = document.querySelector('[name=password]').value;
  // Counted in the DOM: patchright's page.evaluate runs in an isolated world
  // and cannot see page globals, so a window variable would read as 'never'.
  document.body.dataset.submitted = String(Number(document.body.dataset.submitted || 0) + 1);
  setTimeout(() => {
    if (SCENARIO === 'captcha') {
      const c = document.querySelector('.captcha-container');
      c.style.display = 'block';
      c.innerHTML = '<iframe src="https://captcha.provider.test/challenge" style="width:300px;height:80px"></iframe>';
      return;
    }
    if (SCENARIO === 'code_error') {  // the live bot-check failure, 2026-09-25
      const d = document.getElementById('err');
      d.setAttribute('role', 'alert');
      d.textContent = 'Der angegebene Code ist falsch. Bitte versuchen Sie es erneut.';
      return;
    }
    if (SCENARIO === 'reject' || ACCOUNTS[email] !== password) {
      const d = document.getElementById('err');
      d.setAttribute('role', 'alert');
      d.textContent = 'Falsche E-Mail-Adresse oder falsches Passwort.';
      return;
    }
    location.href = 'https://recruit.stepstone.com/callback?code=session-for-' + encodeURIComponent(email);
  }, 300);
});
</script></body></html>"""

BLOCK_HTML = """<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>
You don't have permission to access "http://recruit.stepstone.com/" on this server.<p>
Reference #18.2f3c1702.1790336147.9a1b2c</p></body></html>"""


class FakeStepstone:
    """Serves the fake StepStone and records what the browser did."""

    def __init__(self):
        self.scenario = "ok"            # ok | reject | captcha | blocked
        self.login_target = "https://login.recruit.stepstone.com/login?state=abc&client=test"
        self.accounts = {EMAIL: PASSWORD, OTHER_EMAIL: OTHER_PASSWORD}
        self.hits = []                  # (host, path)
        self.credits_cookies = []       # cookie header seen by the credits API
        self.credits_mode = "cookie"    # cookie | app_only (the live site, 2026-09-25)
        self.credits_calls = []         # (by_app, status)
        self.redirect_delay_ms = 300    # unauthenticated app -> login page

    async def handle(self, route):
        req = route.request
        url = req.url
        host = url.split("/")[2]
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        self.hits.append((host, path.split("?")[0]))
        if host == "recruit.stepstone.com":
            if self.scenario == "blocked":
                return await route.fulfill(status=403, content_type="text/html", body=BLOCK_HTML)
            if path.startswith("/recruiter/talent-sourcing/api/v1/credits"):
                headers = await req.all_headers()
                cookie = headers.get("cookie", "")
                by_app = headers.get("x-app-auth") == "yes"
                self.credits_cookies.append(cookie)
                ok = "tf_session=session-for-" in cookie and (by_app or self.credits_mode == "cookie")
                self.credits_calls.append((by_app, 200 if ok else 401))
                if ok:
                    return await route.fulfill(status=200, content_type="application/json", body=json.dumps(CREDITS))
                return await route.fulfill(status=401, content_type="application/json", body="{}")
            return await route.fulfill(status=200, content_type="text/html",
                                       body=APP_HTML.replace("__LOGIN_TARGET__", self.login_target)
                                       .replace("__REDIRECT_DELAY_MS__", str(self.redirect_delay_ms)))
        if host in ("login.recruit.stepstone.com", "login.stepstone-security.test"):
            body = (LOGIN_HTML.replace("__SCENARIO__", self.scenario)
                    .replace("__ACCOUNTS__", json.dumps(self.accounts)))
            return await route.fulfill(status=200, content_type="text/html", body=body)
        return await route.fulfill(status=404, body="")

    def login_page_hits(self):
        return [h for h in self.hits if h[0] == "login.recruit.stepstone.com"]


@pytest.fixture
async def site(monkeypatch, tmp_path):
    """A real headless Chromium wired to the fake StepStone. Driver stopped on
    teardown: a leaked Playwright driver is its own production incident."""
    monkeypatch.setattr(auth_mod, "LANDING_TIMEOUT_S", 8)
    monkeypatch.setattr(auth_mod, "SESSION_REUSE_TIMEOUT_S", 8)
    monkeypatch.setattr(auth_mod, "OUTCOME_TIMEOUT_S", 8)
    monkeypatch.setattr(auth_mod, "POLL_INTERVAL_S", 0.2)
    monkeypatch.setattr(auth_mod, "TALENT_FINDER_NUDGE_S", 0.6)
    monkeypatch.setattr(auth_mod, "_session_path", lambda email: str(tmp_path / f"{email}.json"))

    async def quick(*a, **k):
        return None
    monkeypatch.setattr(auth_mod, "human_delay", quick)

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as e:  # pragma: no cover - environment without Chromium
        await pw.stop()
        pytest.skip(f"Chromium not available for patchright: {e}")
    context = await browser.new_context(locale="de-DE")
    fake = FakeStepstone()
    await context.route("**/*", fake.handle)
    page = await context.new_page()
    try:
        yield fake, context, page, tmp_path
    finally:
        await browser.close()
        await pw.stop()


async def _submits(page):
    return int(await page.evaluate("document.body.dataset.submitted || '0'"))


def _cookie(name, value, host="recruit.stepstone.com"):
    return {"name": name, "value": value, "domain": host, "path": "/"}


async def test_fresh_login_declines_cookies_types_credentials_and_proves_itself(site, caplog):
    fake, context, page, _ = site
    caplog.set_level("INFO")

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147, "authenticate() returns StepStone's own balance"
    assert fake.login_page_hits(), "a fresh login must go through the login form"
    # The credits API only answers 200 with the session the callback set, so a
    # returned balance proves the server accepted this exact login.
    assert any("tf_session=session-for-" in c for c in fake.credits_cookies)
    consent = {c["name"]: c["value"] for c in await context.cookies("https://recruit.stepstone.com")}
    assert consent.get("consent_level") == "essential", "non-essential cookies must be declined, never accepted"
    assert PASSWORD not in caplog.text, "the password must never reach the logs"


async def test_login_is_proven_by_the_apps_own_balance_call_when_ours_is_refused(site):
    """The live site, 2026-09-25: logged in fine, but a fetch() we made ourselves
    never got the balance, and the dashboard does not load it. The proof must
    come from the app's OWN call on Talent Finder, observed on the network."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert ("recruit.stepstone.com", "/talent-sourcing") in fake.hits, "Talent Finder must be opened"
    assert (True, 200) in fake.credits_calls, "the balance must come from the app's own call"
    assert (False, 401) in fake.credits_calls, "our own fetch was refused, as on the live site"


async def test_a_talent_finder_visit_before_login_does_not_use_up_the_one_after(site):
    """The second live check, 2026-09-25: the redirect to the login page was
    slow, so Talent Finder was opened BEFORE logging in. After the login the
    app landed on the dashboard, which never loads the balance: without a fresh
    Talent Finder visit after the login, a good login times out."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"
    fake.redirect_delay_ms = 1500   # slower than the 0.6s nudge -> a visit before login

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    tf_visits = [h for h in fake.hits if h == ("recruit.stepstone.com", "/talent-sourcing")]
    assert len(tf_visits) >= 2, "one Talent Finder visit before the login and a fresh one after it"


async def test_a_failed_proof_says_what_both_balance_routes_saw(site, monkeypatch):
    """A proof that never arrives must not fail vaguely again: the error names
    what the app's call and our fetch returned."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"
    monkeypatch.setattr(auth_mod, "TALENT_FINDER_URL", "https://recruit.stepstone.com/nowhere")
    monkeypatch.setattr(auth_mod, "_open_talent_finder",
                        lambda page: _async_value("disabled in this test"))

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_OUTCOME_TIMEOUT"
    assert "our fetch=401" in str(err.value) and "app's own call=not seen" in str(err.value)


async def _async_value(value):
    return value


async def test_a_saved_session_that_still_works_is_reused_without_the_form(site):
    fake, context, page, tmp = site
    (tmp / f"{EMAIL}.json").write_text(json.dumps([
        _cookie("tf_session", "session-for-recruiter"), _cookie("consent_level", "essential")]))

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert not fake.login_page_hits(), "a working session must not log in again"


async def test_a_dead_saved_session_falls_back_to_a_fresh_login(site):
    fake, context, page, tmp = site
    (tmp / f"{EMAIL}.json").write_text(json.dumps([
        _cookie("tf_session", "expired"), _cookie("consent_level", "essential")]))

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert fake.login_page_hits(), "a dead session must trigger a fresh login"
    saved = json.loads((tmp / f"{EMAIL}.json").read_text())
    assert any(c["name"] == "tf_session" and c["value"].startswith("session-for-") for c in saved), \
        "the NEW session must be saved for the next job"


async def test_a_second_account_never_rides_on_the_first_accounts_session(site):
    """2026-08-03: account 2 was tried on the same context with account 1's
    cookies still in it. A fresh login now starts from an empty cookie jar."""
    fake, context, page, _ = site
    await authenticate(context, page, EMAIL, PASSWORD)
    first_login_hits = len(fake.login_page_hits())

    await authenticate(context, page, OTHER_EMAIL, OTHER_PASSWORD)

    assert len(fake.login_page_hits()) > first_login_hits, \
        "account 2 must type its own credentials, not inherit account 1's session"
    session = {c["name"]: c["value"] for c in await context.cookies("https://recruit.stepstone.com")}
    assert session["tf_session"] == "session-for-second@example.test"


async def test_wrong_credentials_fail_with_the_pages_own_message(site):
    fake, context, page, _ = site
    fake.scenario = "reject"

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_REJECTED"
    assert "Falsche E-Mail-Adresse oder falsches Passwort" in str(err.value)
    assert PASSWORD not in str(err.value)


async def test_a_captcha_fails_loudly_and_is_never_worked_around(site):
    fake, context, page, _ = site
    fake.scenario = "captcha"

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_CAPTCHA"
    assert "captcha.provider.test" in str(err.value), "the provider is named so it can be handled later"
    assert await _submits(page) == 1, "exactly one submit: no retry loop into a CAPTCHA"


async def test_a_code_error_without_a_code_field_is_a_captcha_not_a_wrong_password(site):
    """Live, 2026-09-25: 'Der angegebene Code ist falsch' on a form with no code
    field. Filed as LOGIN_REJECTED it would trigger a second-account login."""
    fake, context, page, _ = site
    fake.scenario = "code_error"

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_CAPTCHA"
    assert "Der angegebene Code ist falsch" in str(err.value)
    assert await _submits(page) == 1, "exactly one submit"


async def test_an_edge_block_page_is_reported_as_blocked(site):
    fake, context, page, _ = site
    fake.scenario = "blocked"

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_BLOCKED"
    assert "Reference" in str(err.value)


async def test_credentials_are_never_typed_on_a_host_that_is_not_stepstones_login(site):
    """Same form, different host (a redirect we did not expect). The password
    must not be typed there, whatever the page looks like."""
    fake, context, page, _ = site
    fake.login_target = "https://login.stepstone-security.test/login?state=abc"

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_PAGE_TIMEOUT"
    assert page.url.startswith("https://login.stepstone-security.test/")
    assert await page.evaluate("document.querySelector('[name=password]').value") == ""
    assert await _submits(page) == 0, "nothing may be submitted on a foreign host"


# ---------------------------------------------------------------- main.py

def _job():
    return JobInput(offer_id="2468824", stage_id="13166770", job_title="Physiotherapeut (m/w/d)",
                    location="Berlin", max_distance_km=25, max_candidates=50)


def _wire_main(monkeypatch, auth_behaviour):
    """run_scrape with two accounts and a scripted authenticate()."""
    calls = []

    async def fake_browser(*a, **k):
        return object(), object(), object()

    async def fake_close(*a, **k):
        return None

    async def fake_auth(context, page, email, password, solver=None):
        calls.append(email)
        outcome = auth_behaviour[email]
        if isinstance(outcome, AuthenticationError):
            raise outcome
        return dict(CREDITS)

    async def fake_search(*a, **k):
        return [], 25

    accounts = [{"email": EMAIL, "password": PASSWORD}, {"email": OTHER_EMAIL, "password": OTHER_PASSWORD}]
    monkeypatch.setattr(type(main_mod.settings), "get_accounts", lambda self: accounts)
    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: (52.52, 13.40))
    monkeypatch.setattr(main_mod, "select_account", lambda accs, *a, **k: accs[0])
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)
    monkeypatch.setattr(main_mod, "close_browser", fake_close)
    monkeypatch.setattr(main_mod, "authenticate", fake_auth)
    monkeypatch.setattr(main_mod, "search_candidates", fake_search)
    return calls


async def test_after_a_captcha_the_second_account_is_not_tried(monkeypatch):
    calls = _wire_main(monkeypatch, {
        EMAIL: AuthenticationError("captcha shown", code="LOGIN_CAPTCHA"),
        OTHER_EMAIL: None,
    })

    result = await main_mod.run_scrape(_job())

    assert calls == [EMAIL], "a CAPTCHA is about the browser/IP; a second login from it risks a lock"
    assert "All accounts failed to authenticate" in result.error, "n8n routes AUTH_FAILED on this text"
    assert "LOGIN_CAPTCHA" in result.error and "NOT tried" in result.error


async def test_a_rejected_password_falls_back_to_the_second_account(monkeypatch):
    calls = _wire_main(monkeypatch, {
        EMAIL: AuthenticationError("wrong password", code="LOGIN_REJECTED"),
        OTHER_EMAIL: None,
    })

    result = await main_mod.run_scrape(_job())

    assert calls == [EMAIL, OTHER_EMAIL]
    assert result.account_used == "Account 2"
    assert "authenticate" not in (result.error or "")


async def test_when_every_account_fails_each_reason_is_reported(monkeypatch):
    calls = _wire_main(monkeypatch, {
        EMAIL: AuthenticationError("wrong password", code="LOGIN_REJECTED"),
        OTHER_EMAIL: AuthenticationError("no result after submit", code="LOGIN_OUTCOME_TIMEOUT"),
    })

    result = await main_mod.run_scrape(_job())

    assert calls == [EMAIL, OTHER_EMAIL]
    assert "All accounts failed to authenticate" in result.error
    assert "LOGIN_REJECTED" in result.error and "LOGIN_OUTCOME_TIMEOUT" in result.error

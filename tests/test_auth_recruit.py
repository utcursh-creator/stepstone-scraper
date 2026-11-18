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
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest
from patchright.async_api import async_playwright

import main as main_mod
from models.job import JobInput
from scraper import auth as auth_mod
from scraper.auth import AuthenticationError, authenticate
from tests.fakes import fake_search_outcome

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
// Like the live app (auth0-spa-js with refresh tokens, seen 2026-09-25): it
// keeps its own login in localStorage and renews it at the login host's
// /oauth/token, without the login page and without the login session cookie.
const KEEPS_LOGIN = __KEEPS_LOGIN__;
const STALL_ON_REFUSED = __STALL_ON_REFUSED__;  // a refused token leaves the app stuck (unknown live)
const STORE_KEY = '@@auth0spajs@@::test-client::https://recruit.test::openid profile email offline_access';
const keep = (rt, scope) => localStorage.setItem(STORE_KEY, JSON.stringify({ body: { refresh_token: rt, scope }, expiresAt: 0 }));
function boot() {
  if (location.pathname === '/callback') {
    const code = new URLSearchParams(location.search).get('code');
    document.cookie = 'tf_session=' + code + '; path=/';
    if (KEEPS_LOGIN) keep('rt-' + code, 'openid profile email offline_access');
    location.replace('/'); return;
  }
  const loggedIn = val('tf_session') && val('tf_session') !== 'expired';
  if (!loggedIn && KEEPS_LOGIN && localStorage.getItem(STORE_KEY)) {
    const rt = JSON.parse(localStorage.getItem(STORE_KEY)).body.refresh_token;
    fetch('https://login.recruit.stepstone.com/oauth/token', { method: 'POST',
      body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: rt }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((t) => { keep(t.refresh_token, t.scope); document.cookie = 'tf_session=' + t.session + '; path=/'; boot(); })
      .catch(() => { if (STALL_ON_REFUSED) return; localStorage.removeItem(STORE_KEY); boot(); });
    return;
  }
  if (loggedIn) {
    // Like the real app: its own fetch carries its auth header. Only code in
    // the page's MAIN world goes through this wrapper.
    const origFetch = window.fetch;
    window.fetch = (u, init = {}) => origFetch(u, { ...init, headers: { ...(init.headers || {}), 'x-app-auth': 'yes' } });
    document.getElementById('app').innerHTML = '<nav><a href="/talent-sourcing">Talent Finder</a></nav>';
    if (__TF_BROKEN__ && location.pathname.startsWith('/talent-sourcing')) {
      document.getElementById('app').innerHTML += '<p>Oh nein, wir können diese Seite gerade nicht laden.</p>';
      return;
    }
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
// A slow proxy: the form arrives well after the page itself (live, 2026-09-25).
const form = document.querySelector('form');
form.style.display = 'none';
if (__RENDER_DELAY_MS__ >= 0) setTimeout(() => { form.style.display = ''; }, __RENDER_DELAY_MS__);
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
        self.credits_mode = "cookie"    # cookie | app_only (the live site) | refused (never answers)
        self.credits_calls = []         # (by_app, status)
        self.redirect_delay_ms = 300    # unauthenticated app -> login page
        self.login_render_delay_ms = 0  # login page loaded -> its form shows
        self.login_blank_loads = 0      # this many login page loads never show the form
        self.tf_broken = False          # Talent Finder shows its error page, never loads the balance
        self.search_requests = []
        self.search_total = 27
        self.remaining = CREDITS["remainingCredits"]
        self.unlock_requests = []
        self.app_keeps_login = False    # the app keeps a renewable login in localStorage (the live app)
        self.stall_on_refused_token = False
        self.used_refresh_tokens = set()
        self.token_refreshes = []       # account renewed through /oauth/token, in order

    async def handle(self, route):
        req = route.request
        url = req.url
        host = url.split("/")[2]
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        self.hits.append((host, path.split("?")[0]))
        if host == "recruit.stepstone.com":
            if self.scenario == "blocked":
                return await route.fulfill(status=403, content_type="text/html", body=BLOCK_HTML)
            if path.startswith("/recruiter/talent-sourcing/api/v1/autosuggestion/locations"):
                return await route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps([{"name": "Hamburg"}]))
            if path.startswith("/recruiter/talent-sourcing/api/v1/search"):
                headers = await req.all_headers()
                session_ok = "tf_session=session-for-" in headers.get("cookie", "")
                self.search_requests.append({"method": req.method, "json": req.post_data_json,
                                             "cookie_ok": session_ok,
                                             "page_number": int(path.split("pageNumber=")[1].split("&")[0])})
                if not session_ok:
                    return await route.fulfill(status=401, content_type="application/json", body="{}")
                n = self.search_requests[-1]["page_number"]
                size, total = 20, self.search_total
                content = [{"id": f"00000000-0000-4000-8000-{n * size + i:012d}", "hasCv": True, "isLocked": True,
                            "currentJobTitle": "Physiotherapeutin",
                            "personalInfo": {"firstName": "Test", "lastName": "T.",
                                             "address": {"city": "Hamburg", "postalCode": "22589", "country": "DE"}}}
                           for i in range(max(0, min(size, total - n * size)))]
                return await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                    {"content": content, "totalElements": total, "totalPages": -(-total // size),
                     "number": n, "size": size, "uniqueSearchCriteriaId": 68799}))
            if path.startswith("/recruiter/talent-sourcing/api/v1/talent/unlock"):
                headers = await req.all_headers()
                if "tf_session=session-for-" not in headers.get("cookie", ""):
                    return await route.fulfill(status=401, content_type="application/json", body="{}")
                self.unlock_requests.append(req.post_data_json)
                self.remaining -= 1
                return await route.fulfill(status=200, content_type="application/json", body=json.dumps(
                    {"alreadyUnlocked": False, "personalInfo": {
                        "firstName": "Test", "lastName": "Person", "email": "t.person@example.test",
                        "mobilePhoneNumber": "+49 170 0000000",
                        "address": {"postalCode": "22589", "city": "Hamburg", "country": "DE"}}}))
            if "/cv/view" in path:
                return await route.fulfill(status=200, content_type="application/pdf",
                                           body=b"%PDF-1.4\n" + b"0" * 4096 + b"\n%%EOF")
            if path.startswith("/recruiter/talent-sourcing/api/v1/credits"):
                headers = await req.all_headers()
                cookie = headers.get("cookie", "")
                by_app = headers.get("x-app-auth") == "yes"
                self.credits_cookies.append(cookie)
                ok = ("tf_session=session-for-" in cookie and self.credits_mode != "refused"
                      and (by_app or self.credits_mode == "cookie"))
                self.credits_calls.append((by_app, 200 if ok else 401))
                if ok:
                    return await route.fulfill(status=200, content_type="application/json",
                                               body=json.dumps({**CREDITS, "remainingCredits": self.remaining}))
                return await route.fulfill(status=401, content_type="application/json", body="{}")
            return await route.fulfill(status=200, content_type="text/html",
                                       body=APP_HTML.replace("__LOGIN_TARGET__", self.login_target)
                                       .replace("__TF_BROKEN__", "true" if self.tf_broken else "false")
                                       .replace("__KEEPS_LOGIN__", "true" if self.app_keeps_login else "false")
                                       .replace("__STALL_ON_REFUSED__", "true" if self.stall_on_refused_token else "false")
                                       .replace("__REDIRECT_DELAY_MS__", str(self.redirect_delay_ms)))
        if host == "login.recruit.stepstone.com" and path.startswith("/oauth/token"):
            # Rotating refresh tokens, like Auth0: each one works once.
            rt = (parse_qs(req.post_data or "").get("refresh_token") or [""])[0]
            cors = {"Access-Control-Allow-Origin": "https://recruit.stepstone.com"}
            if "session-for-" not in rt or rt in self.used_refresh_tokens:
                return await route.fulfill(status=403, headers=cors, content_type="application/json",
                                           body='{"error": "invalid_grant"}')
            self.used_refresh_tokens.add(rt)
            session = "session-for-" + rt.split("session-for-", 1)[1]
            self.token_refreshes.append(session.split("session-for-", 1)[1])
            return await route.fulfill(status=200, headers=cors, content_type="application/json", body=json.dumps({
                "refresh_token": f"rt{len(self.token_refreshes)}-{session}", "session": session,
                "scope": "openid profile email offline_access", "expires_in": 300}))
        if host in ("login.recruit.stepstone.com", "login.stepstone-security.test"):
            blank = self.login_blank_loads > 0
            self.login_blank_loads -= 1
            body = (LOGIN_HTML.replace("__SCENARIO__", self.scenario)
                    .replace("__ACCOUNTS__", json.dumps(self.accounts))
                    .replace("__RENDER_DELAY_MS__", "-1" if blank else str(self.login_render_delay_ms)))
            return await route.fulfill(status=200, content_type="text/html", body=body)
        return await route.fulfill(status=404, body="")

    def login_page_hits(self):
        # The app renewing its own login at /oauth/token is not the login page.
        return [h for h in self.hits if h[0] == "login.recruit.stepstone.com" and h[1] != "/oauth/token"]


@pytest.fixture
async def site(monkeypatch, tmp_path):
    """A real headless Chromium wired to the fake StepStone. Driver stopped on
    teardown: a leaked Playwright driver is its own production incident."""
    monkeypatch.setattr(auth_mod, "LANDING_TIMEOUT_S", 8)
    monkeypatch.setattr(auth_mod, "LOGIN_FORM_TIMEOUT_S", 8)
    monkeypatch.setattr(auth_mod, "LOGIN_BLANK_RELOAD_S", 2)
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


async def test_login_is_proven_only_by_a_request_carrying_the_apps_session(site):
    """The live site, 2026-09-25: a bare fetch() (patchright's isolated world)
    never got the balance. Only requests that go through the app's own fetch,
    which adds its session, are answered: the app's call on Talent Finder, or
    ours made from the page's main world."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert (True, 200) in fake.credits_calls, "the balance came with the app's own session header"
    assert (False, 200) not in fake.credits_calls, "nothing proves a login without the app's session"


async def test_a_slow_redirect_before_login_does_not_detour_through_talent_finder(site):
    """Live, 2026-09-25: the redirect to the login page was slow, so Talent
    Finder was opened BEFORE logging in. With the cookie jar just cleared that
    visit can only bounce back to the login page, and on the third live check
    the detour cost the login page its time budget."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"
    fake.redirect_delay_ms = 1500   # slower than the 0.6s nudge

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    tf_visits = [i for i, h in enumerate(fake.hits) if h == ("recruit.stepstone.com", "/talent-sourcing")]
    first_login = fake.hits.index(fake.login_page_hits()[0])
    assert all(i > first_login for i in tf_visits), "no Talent Finder visit before the login page"


async def test_the_login_page_gets_its_own_time_after_a_slow_app(site, monkeypatch):
    """Live, 2026-09-25: the app and its redirect took most of the landing
    budget, and the login form rendered just after it ran out. Each leg of the
    way now has its own budget (8s each in these tests; ~11s in total here)."""
    fake, context, page, _ = site
    monkeypatch.setattr(auth_mod, "LOGIN_BLANK_RELOAD_S", 30)  # slow, not blank: no reload
    fake.redirect_delay_ms = 5000
    fake.login_render_delay_ms = 6000

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147


async def test_a_login_page_that_stays_blank_is_reloaded_once(site):
    fake, context, page, _ = site
    fake.login_blank_loads = 1

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert len(fake.login_page_hits()) == 2, "one blank load, one reload"


async def test_a_login_page_that_never_renders_times_out_after_one_reload(site):
    fake, context, page, _ = site
    fake.login_blank_loads = 99

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_PAGE_TIMEOUT"
    assert len(fake.login_page_hits()) == 2, "exactly one reload, never a reload loop"
    assert "failed requests:" in str(err.value)
    assert await _submits(page) == 0


async def test_a_failed_proof_says_what_both_balance_routes_saw(site, monkeypatch):
    """A proof that never arrives must not fail vaguely again: the error names
    what the app's call and our fetch returned."""
    fake, context, page, _ = site
    fake.credits_mode = "refused"
    monkeypatch.setattr(auth_mod, "TALENT_FINDER_URL", "https://recruit.stepstone.com/nowhere")
    monkeypatch.setattr(auth_mod, "_open_talent_finder",
                        lambda page: _async_value("disabled in this test"))

    with pytest.raises(AuthenticationError) as err:
        await authenticate(context, page, EMAIL, PASSWORD)

    assert err.value.code == "LOGIN_OUTCOME_TIMEOUT"
    assert "our fetch=401" in str(err.value) and "app's own call=not seen" in str(err.value)


async def test_login_is_proven_when_talent_finder_fails_to_load(site):
    """Live, 2026-09-28 through the proxy: logged in (the header showed the
    account holder), but Talent Finder showed "Oh nein, wir können..." and
    never loaded the balance. Our own credits request from the page's main
    world carries the app's session and proves the login anyway."""
    fake, context, page, _ = site
    fake.credits_mode = "app_only"
    fake.tf_broken = True

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147


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
    assert any(c["name"] == "tf_session" and c["value"].startswith("session-for-") for c in saved["cookies"]), \
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


async def test_a_fresh_login_keeps_the_device_id_but_no_old_session(site):
    """The login host's "did" names the browser, not an account or a session.
    Dropping it made every fresh login look like a brand-new device."""
    fake, context, page, _ = site
    await context.add_cookies([
        _cookie("did", "device-1", "login.recruit.stepstone.com"),
        _cookie("did_compat", "device-1", "login.recruit.stepstone.com"),
        _cookie("auth0", "old-login-session", "login.recruit.stepstone.com"),
        _cookie("did", "not-the-login-host"),
        _cookie("tf_session", "session-for-someone-else"),
    ])

    await authenticate(context, page, EMAIL, PASSWORD)

    login = {c["name"]: c["value"] for c in await context.cookies("https://login.recruit.stepstone.com")}
    assert login.get("did") == "device-1" and login.get("did_compat") == "device-1"
    assert "auth0" not in login, "the old login session must not survive a fresh login"
    app = {c["name"]: c["value"] for c in await context.cookies("https://recruit.stepstone.com")}
    assert app["tf_session"] == f"session-for-{EMAIL}"
    assert app.get("did") is None, "only the login host's device id is kept"


async def _next_job(context, fake):
    """A new browser for the next job, built from the saved login the way
    create_browser builds it."""
    nxt = await context.browser.new_context(locale="de-DE", storage_state=auth_mod.load_login(EMAIL))
    await nxt.route("**/*", fake.handle)
    return nxt, await nxt.new_page()


def _end_stepstones_login_session(path):
    """What a week does (live, Nov 13 and Nov 16 2026): StepStone's login
    session is over, so the saved cookies alone no longer get in."""
    saved = json.loads(path.read_text())
    for c in saved["cookies"]:
        if c["name"] == "tf_session":
            c["value"] = "expired"
    path.write_text(json.dumps(saved))


async def test_the_apps_stored_login_outlives_stepstones_login_session(site):
    """The weekly password login (and its CAPTCHA) came from saving only the
    cookies: the app's own renewable login lives in localStorage and was
    thrown away after every job. Saved with the cookies, it gets the next job
    in after StepStone's login session has ended, without the password."""
    fake, context, page, tmp = site
    fake.app_keeps_login = True
    await authenticate(context, page, EMAIL, PASSWORD)
    _end_stepstones_login_session(tmp / f"{EMAIL}.json")
    logins = len(fake.login_page_hits())

    nxt, page2 = await _next_job(context, fake)
    credits = await authenticate(nxt, page2, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    assert len(fake.login_page_hits()) == logins, "no login page and no password: the app renewed its own login"
    assert fake.token_refreshes == [EMAIL]
    saved = json.loads((tmp / f"{EMAIL}.json").read_text())
    stored = {i["name"]: i["value"] for o in saved["origins"] if o["origin"] == "https://recruit.stepstone.com"
              for i in o["localStorage"]}
    assert any(json.loads(v)["body"]["refresh_token"].startswith("rt1-") for v in stored.values()), \
        "the renewed token is saved: the one just used works only once"


async def test_a_second_account_never_rides_on_the_first_accounts_stored_login(site):
    """A fresh login forgets the app's stored login as well as the cookies:
    otherwise the app would renew account 1's login while account 2 logs in,
    and the job would spend account 1's credits."""
    fake, context, page, _ = site
    fake.app_keeps_login = True
    await authenticate(context, page, EMAIL, PASSWORD)

    await authenticate(context, page, OTHER_EMAIL, OTHER_PASSWORD)

    assert fake.token_refreshes == [], "account 1's stored login was never used again"
    session = {c["name"]: c["value"] for c in await context.cookies("https://recruit.stepstone.com")}
    assert session["tf_session"] == f"session-for-{OTHER_EMAIL}"


async def test_the_saved_login_knows_when_the_password_was_typed(site, caplog):
    """How long a login lasts is the question every expiry raises; the saved
    login answers it, and each reuse says which route got it in."""
    fake, context, page, tmp = site
    fake.app_keeps_login = True
    caplog.set_level("INFO")
    await authenticate(context, page, EMAIL, PASSWORD)
    typed_at = json.loads((tmp / f"{EMAIL}.json").read_text())["password_login_at"]
    assert abs((datetime.now(timezone.utc) - datetime.fromisoformat(typed_at)).total_seconds()) < 60
    assert "renewable: yes" in caplog.text and "offline_access: yes" in caplog.text, \
        "the log says whether the app's login can outlive StepStone's login session"

    _end_stepstones_login_session(tmp / f"{EMAIL}.json")
    nxt, page2 = await _next_job(context, fake)
    await authenticate(nxt, page2, EMAIL, PASSWORD)

    assert fake.token_refreshes == [EMAIL], "the route is named from what was seen: a renewal happened"
    assert json.loads((tmp / f"{EMAIL}.json").read_text())["password_login_at"] == typed_at, \
        "a reuse keeps the time of the last password login"
    assert "through the app's stored login" in caplog.text
    assert "password last typed 0.0 days ago" in caplog.text


async def test_a_renewed_token_is_saved_even_when_the_reuse_does_not_finish(site):
    """The app renews its token as soon as it loads. If the reuse then times
    out (a slow proxy), the browser holds the new token and the file the used
    one: the next job would send the used token, and StepStone cancels the
    whole chain when a used token comes back."""
    fake, context, page, tmp = site
    fake.app_keeps_login = True
    await authenticate(context, page, EMAIL, PASSWORD)
    _end_stepstones_login_session(tmp / f"{EMAIL}.json")
    fake.credits_mode = "refused"   # in the app, but the balance never comes

    nxt, page2 = await _next_job(context, fake)
    with pytest.raises(AuthenticationError) as err:
        await authenticate(nxt, page2, EMAIL, PASSWORD)

    assert err.value.code == "SESSION_REUSE_TIMEOUT"
    assert fake.token_refreshes == [EMAIL]
    saved = json.loads((tmp / f"{EMAIL}.json").read_text())
    tokens = [json.loads(i["value"])["body"]["refresh_token"] for o in saved["origins"] for i in o["localStorage"]
              if i["name"].startswith("@@auth0spajs@@")]
    assert tokens and tokens[0].startswith("rt1-"), "the renewed token, not the used one"


async def test_a_stored_login_stepstone_refused_is_dropped(site, caplog):
    """A refused token loaded into every later job could leave the app stuck
    each time (neither the app nor the login form): no job would ever run.
    Dropped, the next job falls back to the cookies alone, as before."""
    fake, context, page, tmp = site
    fake.app_keeps_login = True
    fake.stall_on_refused_token = True
    await authenticate(context, page, EMAIL, PASSWORD)
    _end_stepstones_login_session(tmp / f"{EMAIL}.json")
    fake.used_refresh_tokens.add(f"rt-session-for-{EMAIL}")   # StepStone no longer accepts it

    nxt, page2 = await _next_job(context, fake)
    with pytest.raises(AuthenticationError) as err:
        await authenticate(nxt, page2, EMAIL, PASSWORD)

    assert err.value.code == "SESSION_REUSE_TIMEOUT"
    assert "app login renewals: 403" in str(err.value)
    assert json.loads((tmp / f"{EMAIL}.json").read_text())["origins"] == []
    assert await _submits(page2) == 0, "no password was typed"


async def test_a_login_that_worked_is_not_failed_by_its_save(site, monkeypatch):
    """Saving the whole login opens a helper page of its own. If that fails,
    the login still counts, and its cookies are kept so the next job does not
    type the password again."""
    fake, context, page, tmp = site

    async def broken():
        raise RuntimeError("Target page, context or browser has been closed")
    monkeypatch.setattr(context, "storage_state", broken)

    credits = await authenticate(context, page, EMAIL, PASSWORD)

    assert credits["remainingCredits"] == 147
    saved = json.loads((tmp / f"{EMAIL}.json").read_text())
    assert any(c["name"] == "tf_session" for c in saved["cookies"]) and saved["password_login_at"]


def test_a_login_saved_before_this_change_still_loads(tmp_path, monkeypatch):
    """Until Nov 2026 the file was a plain list of cookies."""
    monkeypatch.setattr(auth_mod, "_session_path", lambda email: str(tmp_path / f"{email}.json"))
    cookies = [_cookie("tf_session", "session-for-recruiter")]
    (tmp_path / f"{EMAIL}.json").write_text(json.dumps(cookies))

    assert auth_mod.load_login(EMAIL) == {"cookies": cookies, "origins": []}
    (tmp_path / f"{EMAIL}.json").write_text("{not json")
    assert auth_mod.load_login(EMAIL) is None
    assert auth_mod.load_login(OTHER_EMAIL) is None


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
        return fake_search_outcome([])

    accounts = [{"email": EMAIL, "password": PASSWORD}, {"email": OTHER_EMAIL, "password": OTHER_PASSWORD}]
    monkeypatch.setattr(type(main_mod.settings), "get_accounts", lambda self: accounts)
    monkeypatch.setattr(main_mod, "geocode_location", lambda loc: (52.52, 13.40))
    monkeypatch.setattr(main_mod, "select_account", lambda accs, *a, **k: accs[0])
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)
    monkeypatch.setattr(main_mod, "close_browser", fake_close)
    monkeypatch.setattr(main_mod, "authenticate", fake_auth)
    monkeypatch.setattr(main_mod, "search_talents", fake_search)
    monkeypatch.setattr(main_mod, "_auth_blocked_until", 0.0)
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


async def test_after_every_account_failed_the_next_jobs_do_not_log_in_again(monkeypatch):
    """n8n sends the next queued job right after a failed one. Without a
    cooldown, one failed login becomes one login per queued job from rotating
    IPs, which is how the account gets locked."""
    calls = _wire_main(monkeypatch, {
        EMAIL: AuthenticationError("code error", code="LOGIN_CAPTCHA"),
        OTHER_EMAIL: None,
    })

    first = await main_mod.run_scrape(_job())
    second = await main_mod.run_scrape(_job())
    third = await main_mod.run_scrape(_job())

    assert "All accounts failed to authenticate" in first.error
    assert calls == [EMAIL], "no login at all for the jobs that follow"
    for r in (second, third):
        assert r.error.startswith("AUTH_COOLDOWN:") and r.partial is True
        assert "All accounts failed to authenticate" not in r.error


async def test_a_successful_login_sets_no_cooldown(monkeypatch):
    calls = _wire_main(monkeypatch, {EMAIL: None, OTHER_EMAIL: None})

    await main_mod.run_scrape(_job())
    await main_mod.run_scrape(_job())

    assert calls == [EMAIL, EMAIL]


async def test_a_job_starts_from_the_saved_login_and_saves_it_again(monkeypatch, tmp_path):
    """The browser is built from the whole saved login (cookies and the app's
    localStorage), and the job ends by saving the whole login again: the app
    renews its token during the job, and the old one works only once."""
    monkeypatch.setattr(auth_mod, "_session_path", lambda email: str(tmp_path / f"{email}.json"))
    saved = {"cookies": [_cookie("tf_session", "session-for-recruiter")],
             "origins": [{"origin": "https://recruit.stepstone.com",
                          "localStorage": [{"name": "k", "value": "token-1"}]}],
             "password_login_at": "2026-11-18T05:12:46+00:00"}
    (tmp_path / f"{EMAIL}.json").write_text(json.dumps(saved))
    built = {}

    class Ctx:
        async def storage_state(self):
            return {"cookies": saved["cookies"],
                    "origins": [{"origin": "https://recruit.stepstone.com",
                                 "localStorage": [{"name": "k", "value": "token-2"}]}]}

    async def fake_browser(*a, **k):
        built.update(k)
        return object(), Ctx(), object()

    _wire_main(monkeypatch, {EMAIL: None, OTHER_EMAIL: None})
    monkeypatch.setattr(main_mod, "create_browser", fake_browser)

    await main_mod.run_scrape(_job())

    assert built["storage_state"] == {"cookies": saved["cookies"], "origins": saved["origins"]}
    after = json.loads((tmp_path / f"{EMAIL}.json").read_text())
    assert after["origins"][0]["localStorage"][0]["value"] == "token-2", "the renewed token is kept"
    assert after["password_login_at"] == saved["password_login_at"]

"""Guards for session restore + fresh-login fallback in scraper.auth.

Two prod incidents shaped this:

2026-07-31 — the saved-session check asserted the ABSENCE of a login form after
a 1-2s nap. On an Angular SPA that passes simply because nothing has rendered,
and a bot-challenge page has no login form either, so authenticate() returned,
main.py logged "Authentication successful", and the job then crashed in the
search phase on a page that was never the app. The check now demands a POSITIVE
marker (#searchfield__textfield).

2026-08-03 — "AuthenticationError: All accounts failed to authenticate". Falling
through to a fresh login while the restored session's cookies were still in the
context: StepStone redirects an authenticated browser away from the login page,
so no username field renders and that reads as a login failure. main.py then
retried the other account on the SAME context, replaying account 1's cookies,
and failed identically. Cookies are now cleared before every fresh login.
"""
import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper import auth as auth_mod
from scraper.auth import AUTHENTICATED_MARKER, AuthenticationError, authenticate


class _FakeElement:
    """`on_click` lets the submit button flip the page to its logged-in state,
    so the post-submit verification (form must be gone) can pass."""

    def __init__(self, visible=True, on_click=None):
        self._visible = visible
        self._on_click = on_click

    async def is_visible(self):
        return self._visible

    async def fill(self, *a, **k):
        return None

    async def click(self, *a, **k):
        if self._on_click:
            self._on_click()

    async def press(self, *a, **k):
        if self._on_click:
            self._on_click()

    async def get_attribute(self, *a, **k):
        return None


class _FakeContext:
    def __init__(self):
        self.added_cookies = []
        self.cleared = 0

    async def add_cookies(self, cookies):
        self.added_cookies.append(cookies)

    async def clear_cookies(self):
        self.cleared += 1

    async def cookies(self):
        return [{"name": "PHRECRUITERAUTHCOOKIE", "value": "x"}]


class _FakePage:
    """Records navigations; `marker_visible` drives the session check."""

    url = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"

    def __init__(self, *, marker_visible: bool, has_login_form: bool = True):
        self._marker_visible = marker_visible
        self._has_login_form = has_login_form
        self._submitted = False
        self.gotos = []

    def _submit(self):
        # A successful login makes the form go away — that is what auth.py's
        # post-submit verification checks for.
        self._submitted = True

    async def goto(self, url, **k):
        self.gotos.append(url)
        return None

    async def wait_for_selector(self, selector, **k):
        if selector == AUTHENTICATED_MARKER and self._marker_visible:
            return _FakeElement()
        raise PlaywrightTimeoutError("Timeout exceeded")

    async def query_selector(self, selector, *a, **k):
        form_present = self._has_login_form and not self._submitted
        if "username" in selector or "login" in selector or "email" in selector:
            return _FakeElement(on_click=self._submit) if form_present else None
        if "password" in selector:
            return _FakeElement(on_click=self._submit) if form_present else None
        if "submit" in selector or "Anmelden" in selector or "Einloggen" in selector:
            return _FakeElement(on_click=self._submit)
        return None

    async def evaluate(self, *a, **k):
        return None

    async def wait_for_load_state(self, *a, **k):
        return None

    async def screenshot(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _no_delays_no_disk(monkeypatch, tmp_path):
    async def _no_delay(*a, **k):
        return None

    async def _no_banner(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "human_delay", _no_delay)
    monkeypatch.setattr(auth_mod, "_dismiss_cookie_banner", _no_banner)
    monkeypatch.setattr(auth_mod, "_save_session", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_a_live_session_is_reused_without_logging_in_again(monkeypatch):
    """The positive marker is present -> no navigation to the login page."""
    monkeypatch.setattr(auth_mod, "_load_session", lambda p: [{"name": "c", "value": "v"}])
    ctx, page = _FakeContext(), _FakePage(marker_visible=True)

    await authenticate(ctx, page, "ba@example.de", "pw", None)

    assert auth_mod.LOGIN_URL not in page.gotos, "a valid session must not re-login"
    assert ctx.cleared == 0, "a reused session's cookies must be left alone"


@pytest.mark.asyncio
async def test_a_dead_session_falls_through_to_a_fresh_login(monkeypatch):
    """The 2026-07-31 fail-open: absence of a login form is NOT proof of a
    session. A marker timeout must reach the real login flow."""
    monkeypatch.setattr(auth_mod, "_load_session", lambda p: [{"name": "c", "value": "v"}])
    ctx, page = _FakeContext(), _FakePage(marker_visible=False)

    await authenticate(ctx, page, "ba@example.de", "pw", None)

    assert auth_mod.LOGIN_URL in page.gotos, "a dead session must trigger a fresh login"


@pytest.mark.asyncio
async def test_the_stale_jar_is_discarded_before_the_fresh_login(monkeypatch):
    """THE 2026-08-03 regression guard. Keeping the restored cookies makes
    StepStone redirect an authenticated browser away from the login page, so no
    username field renders and that misreads as a login failure."""
    monkeypatch.setattr(auth_mod, "_load_session", lambda p: [{"name": "c", "value": "v"}])
    ctx, page = _FakeContext(), _FakePage(marker_visible=False)

    await authenticate(ctx, page, "ba@example.de", "pw", None)

    assert ctx.cleared == 1, "cookies must be cleared before the fresh login"
    # Order matters: clearing after navigating would not help.
    assert page.gotos[-1] == auth_mod.LOGIN_URL


@pytest.mark.asyncio
async def test_cookies_are_cleared_even_with_no_saved_session(monkeypatch):
    """main.py's fallback loop retries the OTHER account on the SAME context.
    Without this, account 1's cookies are replayed while logging in as
    account 2 and the retry fails for account 1's reasons."""
    monkeypatch.setattr(auth_mod, "_load_session", lambda p: None)
    ctx, page = _FakeContext(), _FakePage(marker_visible=False)

    await authenticate(ctx, page, "jn@example.de", "pw", None)

    assert ctx.cleared == 1, "a fresh login must always start from a clean jar"


@pytest.mark.asyncio
async def test_a_missing_username_field_still_raises(monkeypatch):
    """The guard must not swallow a genuinely broken login page."""
    monkeypatch.setattr(auth_mod, "_load_session", lambda p: None)
    ctx, page = _FakeContext(), _FakePage(marker_visible=False, has_login_form=False)

    with pytest.raises(AuthenticationError, match="username input field"):
        await authenticate(ctx, page, "ba@example.de", "pw", None)

"""Log in to Stepstone Recruit (recruit.stepstone.com).

In September 2026 StepStone retired DirectSearch (www.stepstone.de/5/...) and
moved recruiters to Stepstone Recruit / Talent Finder. Every URL and selector
the old login used is gone.

The new flow, mapped from the live login page on 2026-09-25 (structure only,
nothing typed into it):

  recruit.stepstone.com
      a Tealium cookie banner, then a JS redirect to
  login.recruit.stepstone.com/login?state=...
      ONE form: input[name=email], input[name=password], a submit button, and
      an EMPTY, hidden div.captcha-container that only fills when the login
      looks risky. It did not show for a person in a normal browser; a
      headless browser behind a proxy is exactly what makes it appear.
  --submit-->  recruit.stepstone.com/callback?code=...  -->  the app

Proof of login is server-side, never "the form went away". The app's own
GET /recruiter/talent-sourcing/api/v1/credits must answer 200 with a numeric
remainingCredits. A blank page, a bot-challenge page and a half-loaded SPA all
lack a login form too, and the old absence test was fooled by exactly that
(2026-07-31). The same endpoint is the authoritative credit balance, so
authenticate() returns it.
"""
import asyncio
import json
import logging
import os
import random
import re
from urllib.parse import urlparse

from patchright.async_api import BrowserContext, Page

from utils.delays import human_delay

logger = logging.getLogger(__name__)

APP_HOST = "recruit.stepstone.com"
LOGIN_HOST = "login.recruit.stepstone.com"
APP_URL = f"https://{APP_HOST}/"
CREDITS_PATH = "/recruiter/talent-sourcing/api/v1/credits"

EMAIL_INPUT = "input[name='email']"
PASSWORD_INPUT = "input[name='password']"
SUBMIT_BUTTON = "form button[type='submit']"
CAPTCHA_CONTAINER = ".captcha-container"

# Tealium consent banner on the app host. We DECLINE non-essential cookies:
# "Einstellungen oder ablehnen" opens the preferences, "Speichern und Beenden"
# saves them with every optional category off. Declining also means fewer
# third-party trackers competing with the app over the residential proxy.
CONSENT_PREFERENCES = "#ccmgt_explicit_preferences"
CONSENT_SAVE_DECLINED = "#ccmgt_preferences_reject"

# Seconds. Module constants so tests can shrink them.
LANDING_TIMEOUT_S = 45      # app URL -> login form, or -> the app itself
SESSION_REUSE_TIMEOUT_S = 30
OUTCOME_TIMEOUT_S = 60      # after submit -> the app, an error, or a CAPTCHA
POLL_INTERVAL_S = 1.0

# Akamai / edge block pages. Matched against visible page text only.
BLOCK_PAGE_RE = re.compile(
    r"access denied|zugriff verweigert|you don't have permission to access|"
    r"reference\s*#\s*\d+\.|request blocked|too many requests|zu viele anfragen",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# Codes that are about this browser/IP, not the account. Trying the second
# account from the same browser straight after one of these is one more
# suspicious login, and repeated suspicious logins are how accounts get locked.
NO_FALLBACK_CODES = frozenset({"LOGIN_CAPTCHA", "LOGIN_BLOCKED"})

_CREDITS_JS = """
async (path) => {
  try {
    const r = await fetch(path, { credentials: 'include', headers: { accept: 'application/json' } });
    let body = null;
    try { body = await r.json(); } catch (e) {}
    return { status: r.status, body };
  } catch (e) {
    return { status: 0, error: String(e) };
  }
}
"""

# Visible error text on the login page after a submit. Deliberately narrow:
# a polite aria-live "loading" message must not read as a rejection.
_LOGIN_ERROR_JS = """
() => {
  const vis = (e) => !!e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length)
    && getComputedStyle(e).visibility !== 'hidden';
  const out = new Set();
  const add = (e) => { if (vis(e)) { const t = (e.innerText || '').trim(); if (t) out.add(t); } };
  document.querySelectorAll('[role="alert"], [aria-live="assertive"], '
    + '[data-genesis-element*="NOTIFICATION"], [data-genesis-element*="ALERT"], '
    + '[data-genesis-element*="ERROR"]').forEach(add);
  document.querySelectorAll('[aria-invalid="true"]').forEach((input) => {
    (input.getAttribute('aria-describedby') || '').split(/\\s+/).forEach((id) => {
      if (id) add(document.getElementById(id));
    });
  });
  return [...out].join(' | ').slice(0, 300);
}
"""

_CAPTCHA_PROVIDERS_JS = """
(sel) => [...document.querySelectorAll(sel + ' iframe, ' + sel + ' script')]
  .map((e) => { try { return new URL(e.src).hostname; } catch (err) { return ''; } })
  .filter(Boolean)
"""


class AuthenticationError(Exception):
    """`code` is a stable token for routing (n8n, the operator email).
    str() carries the code and the human-readable detail."""

    def __init__(self, message: str, code: str = "LOGIN_FAILED"):
        super().__init__(f"{code}: {message}")
        self.code = code


# ----------------------------------------------------------------- sessions

def _session_path(email: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "_", email)
    return os.path.join("sessions", f"{safe}.json")


def _load_session(path: str) -> list[dict] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _save_session(path: str, cookies: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cookies, f)


# ------------------------------------------------------------------ helpers

def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _safe_url(url: str) -> str:
    """Scheme, host and path only: the login URL carries state/nonce/PKCE values."""
    p = urlparse(url or "")
    return f"{p.scheme}://{p.hostname}{p.path}" if p.hostname else (url or "")[:60]


def _balance(credits: dict) -> str:
    if credits.get("unlimited"):
        return "unlimited credits"
    until = credits.get("untilDate") or "unknown date"
    return f"{credits.get('remainingCredits')} credits left until {until}"


async def _visible(page: Page, selector: str) -> bool:
    try:
        el = await page.query_selector(selector)
        return bool(el) and await el.is_visible()
    except Exception:
        return False  # mid-navigation: the next poll decides


async def _page_text(page: Page, limit: int = 3000) -> str:
    try:
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        return (text or "")[:limit]
    except Exception:
        return ""


async def _fetch_credits(page: Page) -> dict | None:
    """The app's own credit balance, or None unless it PROVES an authenticated
    recruiter: 200 with a numeric remainingCredits (or unlimited=true)."""
    if _host(page.url) != APP_HOST:
        return None
    try:
        res = await page.evaluate(_CREDITS_JS, CREDITS_PATH)
    except Exception:
        return None  # e.g. the execution context was destroyed by a redirect
    if not isinstance(res, dict) or res.get("status") != 200:
        return None
    body = res.get("body")
    if not isinstance(body, dict):
        return None
    remaining = body.get("remainingCredits")
    if (isinstance(remaining, int) and not isinstance(remaining, bool)) or body.get("unlimited") is True:
        return body
    return None


async def _decline_consent(page: Page) -> None:
    if not await _visible(page, CONSENT_PREFERENCES):
        return
    try:
        await page.click(CONSENT_PREFERENCES, timeout=5000)
        await page.wait_for_selector(CONSENT_SAVE_DECLINED, state="visible", timeout=10000)
        await human_delay(300, 700)
        await page.click(CONSENT_SAVE_DECLINED, timeout=5000)
        logger.info("Cookie banner: declined non-essential cookies")
    except Exception as e:
        # Left in place, the banner times the landing out, and that error
        # reports it. Nothing is gained by raising here.
        logger.warning(f"Cookie banner present but could not be declined: {type(e).__name__}: {e}")


async def _login_form_visible(page: Page) -> bool:
    return (
        _host(page.url) == LOGIN_HOST
        and await _visible(page, EMAIL_INPUT)
        and await _visible(page, PASSWORD_INPUT)
    )


async def _await_landing(page: Page, timeout_s: float) -> tuple[str, dict | None]:
    """After opening the app URL, wait until we are either IN the app (proven by
    the credits call) or ON the login form. Returns ("app", credits),
    ("login_form", None), ("blocked", None) or ("timeout", None)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        await _decline_consent(page)
        credits = await _fetch_credits(page)
        if credits is not None:
            return "app", credits
        if await _login_form_visible(page):
            return "login_form", None
        if BLOCK_PAGE_RE.search(await _page_text(page)):
            return "blocked", None
        await asyncio.sleep(POLL_INTERVAL_S)
    return "timeout", None


async def _describe_page(page: Page) -> str:
    text = EMAIL_RE.sub("<email>", " ".join((await _page_text(page, 400)).split()))
    return (
        f"url={_safe_url(page.url)}; login form visible={await _login_form_visible(page)}; "
        f"cookie banner visible={await _visible(page, CONSENT_PREFERENCES)}; text={text[:160]!r}"
    )


async def _type_credentials(page: Page, email: str, password: str) -> None:
    # Credentials go to StepStone's login host and nowhere else. The landing
    # loop only reports a login form there, so this is the second lock on the
    # same door: a redirect to any other host must never receive a password.
    if _host(page.url) != LOGIN_HOST:
        raise AuthenticationError(
            f"refusing to type credentials on {_safe_url(page.url)}; only "
            f"{LOGIN_HOST} receives them",
            code="LOGIN_UNEXPECTED_HOST",
        )
    for selector, value in ((EMAIL_INPUT, email), (PASSWORD_INPUT, password)):
        field = page.locator(selector).first
        await field.click()
        await field.fill("")
        await field.press_sequentially(value, delay=random.randint(35, 90))
        await human_delay(400, 900)


async def _await_outcome(page: Page, email: str) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OUTCOME_TIMEOUT_S
    while loop.time() < deadline:
        await _decline_consent(page)
        credits = await _fetch_credits(page)
        if credits is not None:
            return credits
        if _host(page.url) == LOGIN_HOST:
            if await _visible(page, CAPTCHA_CONTAINER):
                try:
                    providers = await page.evaluate(_CAPTCHA_PROVIDERS_JS, CAPTCHA_CONTAINER)
                except Exception:
                    providers = []
                raise AuthenticationError(
                    f"StepStone showed a CAPTCHA when logging in as {email} "
                    f"(provider: {', '.join(sorted(set(providers))) or 'unknown'}). This is "
                    f"about the browser and IP, not the password. Do not retry in a loop: "
                    f"every further attempt raises the risk of an account lock.",
                    code="LOGIN_CAPTCHA",
                )
            try:
                error_text = await page.evaluate(_LOGIN_ERROR_JS)
            except Exception:
                error_text = ""
            if error_text:
                raise AuthenticationError(
                    f"StepStone rejected the login for {email}: "
                    f"{EMAIL_RE.sub('<email>', error_text)!r}",
                    code="LOGIN_REJECTED",
                )
        if BLOCK_PAGE_RE.search(await _page_text(page)):
            raise AuthenticationError(
                f"StepStone's edge blocked the login for {email}. {await _describe_page(page)}",
                code="LOGIN_BLOCKED",
            )
        await asyncio.sleep(POLL_INTERVAL_S)
    raise AuthenticationError(
        f"no result {OUTCOME_TIMEOUT_S}s after submitting the login for {email}. "
        f"{await _describe_page(page)}",
        code="LOGIN_OUTCOME_TIMEOUT",
    )


async def _fresh_login(context: BrowserContext, page: Page, email: str, password: str) -> dict:
    # Discard every cookie FIRST (lesson of 2026-08-03). A session we just
    # judged unusable may still be half-valid, and main.py retries the OTHER
    # account on this same context: without a clean jar, account 1's cookies
    # would ride along into account 2's login.
    await context.clear_cookies()
    await page.goto(APP_URL, wait_until="domcontentloaded")
    state, credits = await _await_landing(page, LANDING_TIMEOUT_S)
    if state == "app":
        return credits
    if state == "blocked":
        raise AuthenticationError(
            f"StepStone's edge blocked the app before login for {email}. {await _describe_page(page)}",
            code="LOGIN_BLOCKED",
        )
    if state != "login_form":
        raise AuthenticationError(
            f"never reached the login form or the app within {LANDING_TIMEOUT_S}s. "
            f"{await _describe_page(page)}",
            code="LOGIN_PAGE_TIMEOUT",
        )
    await _type_credentials(page, email, password)
    await page.click(SUBMIT_BUTTON, timeout=10000)
    return await _await_outcome(page, email)


async def authenticate(
    context: BrowserContext,
    page: Page,
    email: str,
    password: str,
    captcha_solver=None,
) -> dict:
    """Log in to Stepstone Recruit as `email`, reusing a saved session when it
    still works. Returns the account's credit balance as reported by StepStone
    ({"remainingCredits": int, "untilDate": ..., "unlimited": bool, ...}).
    Raises AuthenticationError with a `code` on any failure.

    `captcha_solver` is accepted for call-site compatibility only. The CAPTCHA
    this page may show is not a reCAPTCHA iframe the old solver understood, so
    a CAPTCHA fails the login loudly (LOGIN_CAPTCHA) instead of being guessed at.
    """
    if captcha_solver is not None:
        logger.info("CAPTCHA auto-solve is not wired for Stepstone Recruit; a CAPTCHA fails the login")

    session_file = _session_path(email)
    saved_cookies = _load_session(session_file)
    if saved_cookies:
        try:
            await context.add_cookies(saved_cookies)
            await page.goto(APP_URL, wait_until="domcontentloaded")
            state, credits = await _await_landing(page, SESSION_REUSE_TIMEOUT_S)
        except Exception as e:
            state, credits = "error", None
            logger.warning(f"Restoring the saved session for {email} failed: {type(e).__name__}: {e}")
        if state == "app":
            _save_session(session_file, await context.cookies())  # keep rotated cookies
            logger.info(f"Reused saved Stepstone Recruit session for {email} ({_balance(credits)})")
            return credits
        if state == "blocked":
            raise AuthenticationError(
                f"StepStone's edge blocked the app for {email} while restoring the session. "
                f"{await _describe_page(page)}",
                code="LOGIN_BLOCKED",
            )
        logger.warning(
            f"Saved session for {email} did not prove itself (landing: {state}). "
            f"Falling through to a fresh login, which costs no credits."
        )

    credits = await _fresh_login(context, page, email, password)
    _save_session(session_file, await context.cookies())
    logger.info(f"Logged in to Stepstone Recruit as {email} ({_balance(credits)})")
    return credits

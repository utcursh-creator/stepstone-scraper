import json
import os
import re
from patchright.async_api import BrowserContext, Page
from utils.delays import human_delay

LOGIN_URL = "https://www.stepstone.de/5/index.cfm?event=login"
DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"


class AuthenticationError(Exception):
    pass


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


def _is_login_page(url: str) -> bool:
    lower = url.lower()
    return any(k in lower for k in ["login", "anmelden", "signin"])


async def _dismiss_cookie_banner(page: Page) -> None:
    for selector in [
        "button[data-testid='cookie-accept']",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Accept')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await human_delay(500, 1000)
                return
        except Exception:
            continue


async def authenticate(
    context: BrowserContext,
    page: Page,
    email: str,
    password: str,
    captcha_solver=None,
) -> None:
    """Authenticate to StepStone DirectSearch.

    Tries saved session first, falls back to fresh login.
    Raises AuthenticationError if login fails.
    """
    session_file = _session_path(email)

    # 1. Try saved session
    saved_cookies = _load_session(session_file)
    if saved_cookies:
        await context.add_cookies(saved_cookies)
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
        await human_delay(1000, 2000)
        if not _is_login_page(page.url):
            return  # Session still valid

    # 2. Fresh login
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await human_delay(2000, 4000)
    await _dismiss_cookie_banner(page)

    # Find and fill email field
    email_field = None
    for selector in [
        "input[name='login']",
        "input[name='email']",
        "input[name='username']",
        "input[type='email']",
        "input[id='login']",
    ]:
        email_field = await page.query_selector(selector)
        if email_field:
            break

    if not email_field:
        await page.screenshot(path="screenshots/login_no_email_field.png")
        raise AuthenticationError("Could not find email input field on login page")

    await email_field.fill(email)
    await human_delay(500, 1500)

    # Find and fill password field
    password_field = await page.query_selector("input[type='password']")
    if not password_field:
        await page.screenshot(path="screenshots/login_no_password_field.png")
        raise AuthenticationError("Could not find password field on login page")

    await password_field.fill(password)
    await human_delay(500, 1500)

    # Submit
    submit_btn = None
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Anmelden')",
        "button:has-text('Login')",
        "button:has-text('Einloggen')",
    ]:
        submit_btn = await page.query_selector(selector)
        if submit_btn:
            break

    if submit_btn:
        await submit_btn.click()
    else:
        await password_field.press("Enter")

    # Wait for navigation
    await human_delay(3000, 5000)

    # 3. CAPTCHA handling (if solver provided)
    if captcha_solver:
        captcha_frame = await page.query_selector("iframe[src*='recaptcha']")
        if captcha_frame:
            sitekey = await captcha_frame.get_attribute("data-sitekey")
            if sitekey:
                try:
                    result = captcha_solver.recaptcha(sitekey=sitekey, url=page.url)
                    await page.evaluate(
                        f"document.getElementById('g-recaptcha-response').innerHTML = '{result['code']}'"
                    )
                    await human_delay(1000, 2000)
                except Exception:
                    pass  # CAPTCHA solving failed, continue anyway

    # 4. Verify login
    if _is_login_page(page.url):
        await page.screenshot(path="screenshots/login_failed.png")
        raise AuthenticationError(f"Login failed for {email} — still on login page")

    # 5. Save session
    cookies = await context.cookies()
    _save_session(session_file, cookies)

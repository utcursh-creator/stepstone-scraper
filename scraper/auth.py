import json
import os
import re
from patchright.async_api import BrowserContext, Page
from utils.delays import human_delay

LOGIN_URL = "https://www.stepstone.de/5/recruiterspace/login"
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
    """Accept cookie banner + inject CSS to nuke any late-loading variants."""
    for selector in [
        "#ccmgt_explicit_accept",
        "button[data-testid='cookie-accept']",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Alles akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Accept')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                try:
                    await btn.click(force=True, timeout=5000)
                    await human_delay(500, 1000)
                except Exception:
                    pass
        except Exception:
            continue
    # CSS nuke for any banners that load late (GDPRConsentManagerContainer etc)
    try:
        await page.add_style_tag(content="""
            #GDPRConsentManagerContainer,
            #GDPRConsentManagerContainer *,
            .cc-accordion,
            [class*='consent-manager'],
            [id*='consent-overlay'] {
                display: none !important;
                visibility: hidden !important;
                pointer-events: none !important;
                opacity: 0 !important;
            }
        """)
    except Exception:
        pass


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
        # Session valid if no login form is present (URL may contain 'login' as substring)
        has_login_form = await page.query_selector("input[name='username'], input[name='password']") is not None
        if not has_login_form:
            return  # Session still valid

    # 2. Fresh login
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await human_delay(2000, 4000)
    await _dismiss_cookie_banner(page)

    # Find and fill email/username field
    # Recruiter Space uses input[name='username'] (confirmed 2026-04-17)
    email_field = None
    for selector in [
        "input[name='username']",
        "input[name='login']",
        "input[name='email']",
        "input[type='email']",
        "input[id='login']",
    ]:
        email_field = await page.query_selector(selector)
        if email_field and await email_field.is_visible():
            break
        email_field = None

    if not email_field:
        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/login_no_email_field.png")
        raise AuthenticationError("Could not find username input field on login page")

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

    # Hide any late-loading cookie/GDPR banner that may intercept the click
    await page.evaluate("""
        (() => {
            const sels = ['#GDPRConsentManagerContainer', '.cc-accordion', '[id*="consent"]', '[class*="consent-manager"]'];
            for (const s of sels) {
                document.querySelectorAll(s).forEach(el => { el.style.display = 'none'; });
            }
        })()
    """)
    await human_delay(200, 500)

    if submit_btn:
        try:
            await submit_btn.click(timeout=10000)
        except Exception:
            # Fallback: force click ignoring overlays
            await submit_btn.click(force=True, timeout=10000)
    else:
        await password_field.press("Enter")

    # Wait for navigation to complete
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
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
                    pass

    # 4. Verify login - check by absence of login form, not URL substring
    # (post-login URLs can still contain 'login' as query params or path fragments)
    still_has_login_form = await page.query_selector("input[name='username']") is not None
    still_has_login_form = still_has_login_form and await page.query_selector("input[name='password']") is not None
    if still_has_login_form:
        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/login_failed.png")
        raise AuthenticationError(f"Login failed for {email} - form still visible post-submit")

    # 5. Save session
    cookies = await context.cookies()
    _save_session(session_file, cookies)

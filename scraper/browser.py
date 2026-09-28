import hashlib
import uuid
from datetime import date

from patchright.async_api import async_playwright, Browser, BrowserContext, Page


def proxy_password(proxy_pass: str, proxy_country: str, sticky_key: str | None,
                   day: date | None = None) -> str:
    """IPRoyal modifiers go on the PASSWORD: pass_country-de_session-XXX_lifetime-24h.

    With a sticky_key (the account's email) the session id is fixed for that
    account for the day, so every job of the day leaves from the SAME
    residential IP. A random 10-minute session per job meant a new IP for every
    login, and fresh logins from ever-changing IPs are what StepStone's bot
    check reacts to (CAPTCHA, 2026-09-28). Without a key: a one-off session.
    """
    if sticky_key:
        day = day or date.today()
        session_id = hashlib.sha256(f"{sticky_key.lower()}|{day.isoformat()}".encode()).hexdigest()[:12]
        lifetime = "24h"
    else:
        session_id, lifetime = uuid.uuid4().hex[:12], "10m"
    return f"{proxy_pass}_country-{proxy_country.lower()}_session-{session_id}_lifetime-{lifetime}"


async def create_browser(
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_pass: str,
    proxy_country: str = "DE",
    sticky_key: str | None = None,
) -> tuple[Browser, BrowserContext, Page]:
    """Launch a stealth Patchright browser with IPRoyal residential proxy."""
    p = await async_playwright().start()

    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )

    composed_pass = proxy_password(proxy_pass, proxy_country, sticky_key)

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
        proxy={
            "server": f"http://{proxy_host}:{proxy_port}",
            "username": proxy_user,
            "password": composed_pass,
        },
    )

    page = await context.new_page()
    page.set_default_navigation_timeout(120_000)

    return browser, context, page


async def close_browser(browser: Browser) -> None:
    """Safely close the browser."""
    try:
        await browser.close()
    except Exception:
        pass

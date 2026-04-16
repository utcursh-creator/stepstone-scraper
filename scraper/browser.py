import uuid
from patchright.async_api import async_playwright, Browser, BrowserContext, Page


async def create_browser(
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_pass: str,
    proxy_country: str = "DE",
) -> tuple[Browser, BrowserContext, Page]:
    """Launch a stealth Patchright browser with IPRoyal residential proxy."""
    session_id = uuid.uuid4().hex[:12]
    p = await async_playwright().start()

    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        proxy={
            "server": f"http://{proxy_host}:{proxy_port}",
            "username": f"{proxy_user}_country-{proxy_country}_session-{session_id}",
            "password": proxy_pass,
        },
    )

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
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

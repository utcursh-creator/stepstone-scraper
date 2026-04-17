"""Probe DirectSearch filter panel selectors - with proper cookie accept."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser
from scraper.auth import authenticate, DIRECTSEARCH_URL

settings = Settings()


async def accept_cookies(page):
    """Click 'Alles akzeptieren' on the DirectSearch cookie modal."""
    for sel in [
        "button:has-text('Alles akzeptieren')",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "#onetrust-accept-btn-handler",
    ]:
        btn = await page.query_selector(sel)
        if btn:
            try:
                vis = await btn.is_visible()
            except Exception:
                vis = False
            if vis:
                await btn.click()
                await page.wait_for_timeout(1500)
                print(f"    Cookies accepted via: {sel}")
                return True
    return False


async def main():
    account = settings.get_accounts()[0]
    browser, context, page = await create_browser(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        proxy_user=settings.proxy_user,
        proxy_pass=settings.proxy_pass,
        proxy_country=settings.proxy_country,
    )
    try:
        await authenticate(context, page, account["email"], account["password"])
        print(f"[+] Logged in. URL: {page.url}")

        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)

        print("[+] Accept cookies if modal present")
        await accept_cookies(page)

        # Wait for page to settle after cookie accept
        await page.wait_for_timeout(2000)

        # Click "Filter anzeigen" with a proper scope
        print("\n[1] Click 'Filter anzeigen' toggle")
        toggle = await page.query_selector("a:has-text('Filter anzeigen')")
        if not toggle:
            toggle = await page.query_selector("span:has-text('Filter anzeigen')")
        if not toggle:
            # Look for it in any small link/button near the search field
            candidates = await page.query_selector_all("a, button, [role='button']")
            for c in candidates:
                try:
                    txt = (await c.inner_text()).strip()
                    if txt == "Filter anzeigen":
                        toggle = c
                        break
                except Exception:
                    continue

        if toggle:
            await toggle.click()
            await page.wait_for_timeout(3000)
            print(f"    Clicked. Toggle was: {await toggle.evaluate('el => el.tagName.toLowerCase()')}")

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/ds_filters_open.png", full_page=True)
        print("[+] Screenshot: screenshots/ds_filters_open.png")

        print("\n[2] All visible input fields after filter open:")
        inputs = await page.query_selector_all("input")
        for i, inp in enumerate(inputs):
            try:
                name = await inp.get_attribute("name") or ""
                typ = await inp.get_attribute("type") or ""
                ph = await inp.get_attribute("placeholder") or ""
                idv = await inp.get_attribute("id") or ""
                cls = await inp.get_attribute("class") or ""
                vis = await inp.is_visible()
                if vis:
                    print(f"    [{i}] type={typ}  name={name!r}  id={idv!r}  placeholder={ph!r}  class={cls[:60]!r}")
            except Exception:
                pass

        print("\n[3] All visible selects:")
        selects = await page.query_selector_all("select")
        for i, s in enumerate(selects):
            try:
                name = await s.get_attribute("name") or ""
                idv = await s.get_attribute("id") or ""
                cls = await s.get_attribute("class") or ""
                vis = await s.is_visible()
                if vis:
                    opts = await s.query_selector_all("option")
                    opt_texts = []
                    for o in opts[:12]:
                        t = (await o.inner_text()).strip()[:50]
                        v = await o.get_attribute("value") or ""
                        opt_texts.append(f"{t}={v}")
                    print(f"    [{i}] name={name!r}  id={idv!r}  class={cls[:40]!r}  opts={opt_texts}")
            except Exception:
                pass

        print("\n[4] Labels visible:")
        labels = await page.query_selector_all("label")
        for i, l in enumerate(labels):
            try:
                txt = (await l.inner_text()).strip()
                for_attr = await l.get_attribute("for") or ""
                vis = await l.is_visible()
                if vis and txt and "cc-ec" not in for_attr:
                    print(f"    [{i}] text={txt[:60]!r}  for={for_attr!r}")
            except Exception:
                pass

        print("\n[5] Body text after filter open (first 1500 chars):")
        body = await page.inner_text("body")
        print(body[:1500])

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

"""Submit a search for 'Burofachkraft Halle' and probe the results page."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser
from scraper.auth import authenticate, DIRECTSEARCH_URL

settings = Settings()


async def accept_cookies(page):
    for sel in [
        "button:has-text('Alles akzeptieren')",
        "button:has-text('Alle akzeptieren')",
        "#onetrust-accept-btn-handler",
    ]:
        btn = await page.query_selector(sel)
        if btn and await btn.is_visible():
            await btn.click()
            await page.wait_for_timeout(1500)
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
        await accept_cookies(page)
        await page.wait_for_timeout(2000)

        # Type search query
        print("\n[+] Type query into searchfield__textfield")
        field = await page.query_selector("#searchfield__textfield")
        if not field:
            print("    ERROR: searchfield__textfield not found")
            return
        await field.click()
        await page.wait_for_timeout(500)
        await field.fill("Burofachkraft Halle")
        await page.wait_for_timeout(1500)

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/ds_search_typed.png", full_page=True)

        # Check for autocomplete suggestions
        print("\n[+] Check for autocomplete suggestions")
        suggestions = await page.query_selector_all("[class*='suggestion'], [class*='autocomplete'], [role='option'], li[class*='item']")
        print(f"    Found {len(suggestions)} suggestion-like elements")
        for i, s in enumerate(suggestions[:10]):
            try:
                vis = await s.is_visible()
                if vis:
                    txt = (await s.inner_text()).strip()[:80]
                    cls = await s.get_attribute("class") or ""
                    print(f"    [{i}] class={cls[:60]!r}  text={txt!r}")
            except Exception:
                pass

        # Press Enter to submit (or find a submit button)
        print("\n[+] Press Enter to submit search")
        await field.press("Enter")
        await page.wait_for_timeout(6000)

        print(f"    URL after submit: {page.url}")
        print(f"    Title: {await page.title()}")
        await page.screenshot(path="screenshots/ds_results.png", full_page=True)
        print("[+] Screenshot: screenshots/ds_results.png")

        # Count results
        print("\n[+] Probe result structure")
        body = await page.inner_text("body")
        print(f"    Body length: {len(body)}")
        print(f"    First 500 chars of body: {body[:500]}")
        print(f"    Search for 'Treffer' or 'Ergebnisse' or result count indicator:")
        for keyword in ["Treffer", "Ergebnisse", "Kandidaten", "Profile"]:
            idx = body.find(keyword)
            if idx >= 0:
                print(f"      '{keyword}' found at position {idx}: ...{body[max(0,idx-30):idx+60]}...")

        # Look for candidate cards / rows
        print("\n[+] Try to find candidate result elements:")
        card_selectors = [
            ".miniprofile",
            "[class*='miniprofile']",
            "[class*='candidate']",
            "[class*='result-item']",
            "[class*='search-result']",
            "tr[class*='result']",
            "tr.result",
            "[class*='profile-card']",
            "div[ng-repeat*='candidate']",
            "div[ng-repeat*='profile']",
            "a[href*='profile']",
            "a[href*='miniprofile']",
        ]
        for sel in card_selectors:
            els = await page.query_selector_all(sel)
            if els:
                vis_count = 0
                for e in els[:20]:
                    try:
                        if await e.is_visible():
                            vis_count += 1
                    except Exception:
                        pass
                print(f"    {sel!r}: {len(els)} total, {vis_count} visible")

        # Look for sort/filter controls on results page
        print("\n[+] Sort / filter controls on results page:")
        ctrls = await page.query_selector_all("button, a, select")
        for c in ctrls[:40]:
            try:
                if not await c.is_visible():
                    continue
                txt = (await c.inner_text()).strip()[:60]
                if txt and any(kw in txt.lower() for kw in ["sortieren", "filter", "aktivit", "standort", "ort", "umkreis", "radius", "datum"]):
                    print(f"    text={txt!r}")
            except Exception:
                pass

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

"""Probe result card structure and profile dialog."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser
from scraper.auth import authenticate, DIRECTSEARCH_URL

settings = Settings()


async def kill_cookie_banner(page):
    """Click accept if present, AND inject CSS to nuke the banner via pointer-events: none."""
    for sel in ["button:has-text('Alles akzeptieren')", "button:has-text('Alle akzeptieren')", "#onetrust-accept-btn-handler"]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                try:
                    await btn.click(force=True, timeout=5000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
        except Exception:
            pass
    # Nuke with CSS injection - most reliable
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
    await page.wait_for_timeout(500)


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
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await kill_cookie_banner(page)

        # Search
        field = await page.query_selector("#searchfield__textfield")
        try:
            await field.click(force=True, timeout=10000)
        except Exception:
            await field.focus()
        await field.fill("Burofachkraft Halle")
        await field.press("Enter")
        await page.wait_for_timeout(7000)
        await kill_cookie_banner(page)

        print(f"[+] Results page: {page.url}")

        # Get the first miniprofile card
        cards = await page.query_selector_all(".miniprofile")
        print(f"[+] Found {len(cards)} .miniprofile cards (first 10 visible)")

        if not cards:
            print("    No cards found!")
            return

        # Dump HTML of first card
        print("\n[1] Inner HTML of first .miniprofile card (truncated):")
        first = cards[0]
        html = await first.inner_html()
        print(html[:2500])

        # Dump text of first card
        print("\n[2] Inner text of first card:")
        txt = await first.inner_text()
        print(txt[:1500])

        # Look for links in first card
        print("\n[3] All links in first card:")
        links = await first.query_selector_all("a")
        for i, link in enumerate(links):
            try:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()[:50]
                cls = await link.get_attribute("class") or ""
                print(f"    [{i}] href={href[:120]!r}")
                print(f"        text={text!r}  class={cls[:60]!r}")
            except Exception:
                pass

        # Look for buttons in first card
        print("\n[4] All buttons in first card:")
        btns = await first.query_selector_all("button")
        for i, b in enumerate(btns):
            try:
                cls = await b.get_attribute("class") or ""
                text = (await b.inner_text()).strip()[:50]
                title = await b.get_attribute("title") or ""
                print(f"    [{i}] text={text!r}  title={title!r}  class={cls[:60]!r}")
            except Exception:
                pass

        # Try to find the StepStone profile ID from card attrs
        print("\n[5] Attributes on the .miniprofile element itself:")
        attrs = await first.evaluate("el => { const r = {}; for (const a of el.attributes) r[a.name] = a.value; return r; }")
        for k, v in attrs.items():
            print(f"    {k} = {str(v)[:100]}")

        # Click the first profile to open dialog
        print("\n[6] Click the first candidate to open profile dialog")
        # Try common click targets in miniprofile cards
        clickable = await first.query_selector("a.miniprofile__actionlink[href*='tabName=profile'], a[href*='tabName=profile']")
        if not clickable:
            clickable = await first.query_selector("a.miniprofile__actionlink, a.miniprofile__link")
        if not clickable:
            # Last resort: first link in the card
            clickable = await first.query_selector("a[href]")
        if clickable:
            href = await clickable.get_attribute("href")
            print(f"    Clicking link: {href[:150]}")
            await clickable.click()
            await page.wait_for_timeout(5000)
        else:
            print("    No clickable found in card")
            return

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/ds_profile_dialog.png", full_page=True)
        print("[+] Screenshot: screenshots/ds_profile_dialog.png")

        # Probe the dialog
        print("\n[7] Find profile dialog elements")
        dialog_selectors = [
            "div.ngdialog:last-of-type",
            "div.ngdialog",
            "div[role='dialog']",
            ".profile-dialog",
            ".ngdialog-content",
            "[class*='dialog']",
            "[class*='modal']",
        ]
        dialog = None
        for sel in dialog_selectors:
            el = await page.query_selector(sel)
            if el:
                try:
                    vis = await el.is_visible()
                except Exception:
                    vis = False
                print(f"    {sel!r}: found, visible={vis}")
                if vis and not dialog:
                    dialog = el

        if dialog:
            print("\n[8] Dialog inner text (first 1500 chars):")
            dialog_text = await dialog.inner_text()
            print(dialog_text[:1500])
        else:
            print("\n[8] No visible dialog found. Body text:")
            body = await page.inner_text("body")
            print(body[:1500])

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

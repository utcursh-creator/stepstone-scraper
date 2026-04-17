"""Diagnostic: find the correct StepStone B2B login URL and form selectors."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser

settings = Settings()


async def main():
    print("=" * 70)
    print("DEBUG: Probe StepStone login flow")
    print("=" * 70)

    browser, context, page = await create_browser(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        proxy_user=settings.proxy_user,
        proxy_pass=settings.proxy_pass,
        proxy_country=settings.proxy_country,
    )

    try:
        # Step 1: go to homepage
        print("\n[1] Loading stepstone.de homepage")
        await page.goto("https://www.stepstone.de/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"    URL: {page.url}  Title: {await page.title()}")

        # Step 2: handle cookie banner aggressively — try lots of selectors
        print("\n[2] Dismiss cookie banner")
        cookie_selectors = [
            "#ccmgt_explicit_accept",
            "button[id*='accept']",
            "button[data-testid='cookie-accept']",
            "button[data-testid*='accept']",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Akzeptieren')",
            "button:has-text('Accept all')",
            "button:has-text('Accept')",
            "button:has-text('Alles akzeptieren')",
            "button:has-text('Einverstanden')",
            "#onetrust-accept-btn-handler",
            ".cookie-accept",
            "[class*='accept-all']",
        ]
        cookie_btn = None
        for sel in cookie_selectors:
            btn = await page.query_selector(sel)
            if btn:
                try:
                    is_visible = await btn.is_visible()
                except Exception:
                    is_visible = False
                print(f"    Found with {sel!r}  visible={is_visible}")
                if is_visible and not cookie_btn:
                    cookie_btn = btn
        if cookie_btn:
            await cookie_btn.click()
            await page.wait_for_timeout(1500)
            print("    Clicked cookie accept")
        else:
            print("    No cookie accept button found among tried selectors")

        # Step 3: find login link on homepage
        print("\n[3] Find login link on homepage")
        login_candidates = await page.query_selector_all("a[href*='login'], a[href*='recruiter'], a:has-text('Login'), a:has-text('Anmelden')")
        print(f"    Found {len(login_candidates)} candidate links:")
        for i, link in enumerate(login_candidates[:15]):
            try:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()[:50]
                is_vis = await link.is_visible()
                print(f"    [{i}] text={text!r}  href={href}  visible={is_vis}")
            except Exception:
                pass

        await page.screenshot(path="screenshots/debug_home.png", full_page=True)
        print("    Screenshot: screenshots/debug_home.png")

        # Step 4: click the top-right Login button (common pattern)
        print("\n[4] Try common login URLs directly")
        urls_to_try = [
            "https://www.stepstone.de/5/index.cfm?event=login",
            "https://www.stepstone.de/candidate/login",
            "https://www.stepstone.de/recruiter/login",
            "https://www.stepstone.de/5/loginanmeldung",
            "https://www.stepstone.de/recruiter-login",
            "https://recruiter.stepstone.de/",
            "https://login.stepstone.de/",
            "https://www.stepstone.de/b2b",
        ]
        for url in urls_to_try:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                status = response.status if response else "?"
                final = page.url
                title = await page.title()
                # Check for password field
                pw = await page.query_selector("input[type='password']")
                print(f"    [{url}]  status={status}  final={final}  title={title[:60]}  has_pw_field={pw is not None}")
                if pw:
                    # Found a real login page — dump selectors
                    print(f"    *** FOUND LOGIN PAGE ***")
                    all_inputs = await page.query_selector_all("input")
                    for inp in all_inputs[:20]:
                        try:
                            name = await inp.get_attribute("name") or ""
                            typ = await inp.get_attribute("type") or ""
                            ph = await inp.get_attribute("placeholder") or ""
                            idv = await inp.get_attribute("id") or ""
                            print(f"      input: type={typ}  name={name}  id={idv}  placeholder={ph}")
                        except Exception:
                            pass
                    all_btns = await page.query_selector_all("button")
                    for btn in all_btns[:10]:
                        try:
                            typ = await btn.get_attribute("type") or ""
                            txt = (await btn.inner_text()).strip()[:40]
                            print(f"      button: type={typ}  text={txt!r}")
                        except Exception:
                            pass
                    await page.screenshot(path=f"screenshots/debug_login_{url.replace('/', '_').replace(':', '_')[:40]}.png", full_page=True)
                    break
            except Exception as e:
                print(f"    [{url}]  ERROR: {type(e).__name__}: {str(e)[:80]}")

    finally:
        await close_browser(browser)
        print("\n[+] Browser closed")


if __name__ == "__main__":
    asyncio.run(main())

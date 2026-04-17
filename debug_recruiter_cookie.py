"""Find the cookie banner dismiss button on recruiter login page."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser

settings = Settings()


async def main():
    browser, context, page = await create_browser(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        proxy_user=settings.proxy_user,
        proxy_pass=settings.proxy_pass,
        proxy_country=settings.proxy_country,
    )
    try:
        print("[+] Navigate to recruiter login")
        await page.goto("https://www.stepstone.de/5/recruiterspace/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/recruiter_login_cookie.png", full_page=True)
        print("[+] Screenshot: screenshots/recruiter_login_cookie.png")

        # Explore the GDPRConsentManagerContainer
        print("\n[+] Probe #GDPRConsentManagerContainer")
        container = await page.query_selector("#GDPRConsentManagerContainer")
        if container:
            print("    Container found - dumping all buttons inside")
            btns = await container.query_selector_all("button, a, [role='button'], [class*='accept'], [class*='Accept']")
            for i, b in enumerate(btns):
                try:
                    tag = await b.evaluate("el => el.tagName.toLowerCase()")
                    cls = await b.get_attribute("class") or ""
                    idv = await b.get_attribute("id") or ""
                    txt = (await b.inner_text()).strip()[:80]
                    onclick = await b.get_attribute("onclick") or ""
                    vis = await b.is_visible()
                    print(f"    [{i}] <{tag}>  id={idv!r}  class={cls[:80]!r}  text={txt!r}  visible={vis}  onclick={onclick[:40]!r}")
                except Exception as e:
                    print(f"    [{i}] ERR: {e}")
        else:
            print("    Container NOT found")

        print("\n[+] All buttons with 'accept' or 'Akzeptieren' anywhere")
        all_btns = await page.query_selector_all("button, a[role='button'], [class*='accept']")
        for i, b in enumerate(all_btns[:30]):
            try:
                cls = await b.get_attribute("class") or ""
                idv = await b.get_attribute("id") or ""
                txt = (await b.inner_text()).strip()[:60]
                vis = await b.is_visible()
                if txt and ("akzep" in txt.lower() or "accept" in txt.lower() or "zustim" in txt.lower() or "einver" in txt.lower()):
                    print(f"    [{i}]  id={idv!r}  class={cls[:60]!r}  text={txt!r}  visible={vis}")
            except Exception:
                pass

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

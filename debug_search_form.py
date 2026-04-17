"""Probe DirectSearch form selectors (keyword, location, filters, submit)."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser
from scraper.auth import authenticate, DIRECTSEARCH_URL

settings = Settings()


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
        print("[+] Authenticating (should reuse saved session)")
        await authenticate(context, page, account["email"], account["password"])
        print(f"    URL after auth: {page.url}")

        print(f"[+] Navigate to DirectSearch: {DIRECTSEARCH_URL}")
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        # Hide any late cookie banner
        await page.evaluate("""
            document.querySelectorAll('#GDPRConsentManagerContainer, .cc-accordion, [class*="consent-manager"]').forEach(el => el.style.display = 'none');
        """)
        await page.wait_for_timeout(1000)

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/ds_form.png", full_page=True)
        print("[+] Screenshot: screenshots/ds_form.png")

        print(f"    Title: {await page.title()}")
        print(f"    URL:   {page.url}")

        print("\n[+] All visible input fields:")
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

        print("\n[+] All visible selects/dropdowns:")
        selects = await page.query_selector_all("select")
        for i, s in enumerate(selects):
            try:
                name = await s.get_attribute("name") or ""
                idv = await s.get_attribute("id") or ""
                vis = await s.is_visible()
                opts = await s.query_selector_all("option")
                opt_texts = []
                for o in opts[:8]:
                    t = (await o.inner_text()).strip()[:40]
                    v = await o.get_attribute("value") or ""
                    opt_texts.append(f"{t!r}={v!r}")
                if vis:
                    print(f"    [{i}] name={name!r}  id={idv!r}  options={opt_texts}")
            except Exception:
                pass

        print("\n[+] Buttons/submits near top of page:")
        btns = await page.query_selector_all("button, input[type='submit']")
        for i, b in enumerate(btns):
            try:
                typ = await b.get_attribute("type") or ""
                cls = await b.get_attribute("class") or ""
                idv = await b.get_attribute("id") or ""
                txt = (await b.inner_text()).strip()[:60]
                vis = await b.is_visible()
                if vis and txt:
                    print(f"    [{i}] type={typ}  id={idv!r}  text={txt!r}  class={cls[:50]!r}")
            except Exception:
                pass

        print("\n[+] Form structure:")
        forms = await page.query_selector_all("form")
        for i, f in enumerate(forms):
            try:
                action = await f.get_attribute("action") or ""
                method = await f.get_attribute("method") or ""
                idv = await f.get_attribute("id") or ""
                vis = await f.is_visible()
                print(f"    [{i}] action={action!r}  method={method!r}  id={idv!r}  visible={vis}")
            except Exception:
                pass

        print("\n[+] First 1000 chars of body text:")
        body = await page.inner_text("body")
        print(body[:1000])

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

"""Dump selectors for the recruiter login form."""
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
        print("[+] Navigate to recruiter space login")
        await page.goto("https://www.stepstone.de/5/recruiterspace/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"    URL: {page.url}")
        print(f"    Title: {await page.title()}")

        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/recruiter_login.png", full_page=True)

        print("\n[+] All inputs on page:")
        inputs = await page.query_selector_all("input")
        for i, inp in enumerate(inputs):
            try:
                name = await inp.get_attribute("name") or ""
                typ = await inp.get_attribute("type") or ""
                ph = await inp.get_attribute("placeholder") or ""
                idv = await inp.get_attribute("id") or ""
                cls = await inp.get_attribute("class") or ""
                vis = await inp.is_visible()
                print(f"    [{i}] type={typ}  name={name!r}  id={idv!r}  placeholder={ph!r}  class={cls[:50]!r}  visible={vis}")
            except Exception:
                pass

        print("\n[+] All buttons / submits:")
        btns = await page.query_selector_all("button, input[type='submit']")
        for i, b in enumerate(btns):
            try:
                typ = await b.get_attribute("type") or ""
                name = await b.get_attribute("name") or ""
                idv = await b.get_attribute("id") or ""
                txt = (await b.inner_text()).strip()[:60]
                val = await b.get_attribute("value") or ""
                vis = await b.is_visible()
                print(f"    [{i}] type={typ}  name={name!r}  id={idv!r}  text={txt!r}  value={val!r}  visible={vis}")
            except Exception:
                pass

        print("\n[+] All form elements:")
        forms = await page.query_selector_all("form")
        for i, f in enumerate(forms):
            try:
                action = await f.get_attribute("action") or ""
                method = await f.get_attribute("method") or ""
                idv = await f.get_attribute("id") or ""
                print(f"    [{i}] action={action!r}  method={method!r}  id={idv!r}")
            except Exception:
                pass

    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())

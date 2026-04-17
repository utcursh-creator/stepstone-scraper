"""Diagnostic: find the recruiter/B2B login for StepStone (not candidate)."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from models.config import Settings
from scraper.browser import create_browser, close_browser

settings = Settings()


async def main():
    print("=" * 70)
    print("DEBUG: Probe StepStone recruiter login")
    print("=" * 70)

    browser, context, page = await create_browser(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        proxy_user=settings.proxy_user,
        proxy_pass=settings.proxy_pass,
        proxy_country=settings.proxy_country,
    )

    try:
        # 1) Navigate directly to DirectSearch - see where it redirects when unauthed
        print("\n[1] Navigate directly to DirectSearch URL (unauthed)")
        await page.goto("https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        print(f"    Final URL: {page.url}")
        print(f"    Title: {await page.title()}")
        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path="screenshots/debug_ds_redirect.png", full_page=True)
        body_preview = (await page.inner_text("body"))[:500]
        print(f"    Body preview: {body_preview}")

        # 2) Try a bunch of potential recruiter URLs
        print("\n[2] Probe recruiter/B2B URLs")
        urls = [
            "https://www.stepstone.de/5/",
            "https://www.stepstone.de/5/index.cfm",
            "https://www.stepstone.de/5/index.cfm?event=home",
            "https://www.stepstone.de/5/recruiterhub",
            "https://www.stepstone.de/b2b",
            "https://www.stepstone.de/de-DE/recruiter/login",
            "https://www.stepstone.de/recruiter",
            "https://b2b.stepstone.de/",
            "https://recruiter.stepstone.de/",
            "https://login.stepstone.de/",
            "https://corporate.stepstone.de/",
            "https://www.stepstone.de/stellenanzeige-aufgeben",
            "https://www.stepstone.de/arbeitgeber",
            "https://www.stepstone.de/de-DE/arbeitgeber",
            "https://www.stepstone.de/recruiting",
        ]
        for url in urls:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(1500)
                status = response.status if response else "?"
                final = page.url
                title = await page.title()
                pw = await page.query_selector("input[type='password']")
                print(f"    [{url}]")
                print(f"        status={status}  final={final}")
                print(f"        title={title[:80]}  has_pw={pw is not None}")
            except Exception as e:
                print(f"    [{url}]  ERR: {str(e)[:80]}")

        # 3) Look for "Arbeitgeber" / "Für Arbeitgeber" / "Recruiter" links on the homepage
        print("\n[3] Look for employer/recruiter links on homepage")
        await page.goto("https://www.stepstone.de/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        # Dismiss cookie
        btn = await page.query_selector("#ccmgt_explicit_accept")
        if btn:
            await btn.click()
            await page.wait_for_timeout(1000)
        # Find all links with employer-related text
        all_links = await page.query_selector_all("a")
        print(f"    Total links on page: {len(all_links)}")
        interesting = []
        for link in all_links:
            try:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if any(kw in (text.lower() + " " + href.lower()) for kw in ["arbeitgeber", "recruiter", "b2b", "business", "stellenanzeige", "employer", "enterprise"]):
                    if text and text[:80] not in [i[0] for i in interesting]:
                        interesting.append((text[:80], href))
            except Exception:
                continue
        print(f"    Found {len(interesting)} employer-related links:")
        for text, href in interesting[:20]:
            print(f"      text={text!r}  href={href}")

    finally:
        await close_browser(browser)
        print("\n[+] Browser closed")


if __name__ == "__main__":
    asyncio.run(main())

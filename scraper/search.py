from patchright.async_api import Page
from utils.delays import human_delay

DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"
RADIUS_STEPS = [25, 50, 75, 100]


class SearchResult:
    def __init__(self, profile_id: str, preview_text: str):
        self.profile_id = profile_id
        self.preview_text = preview_text


async def _enter_keyword(page: Page, job_title: str) -> None:
    for selector in [
        "input[name='searchtext']",
        "input[placeholder*='Jobtitel']",
        "input[placeholder*='jobtitel']",
        "input[placeholder*='Suchbegriff']",
        "#searchtext",
    ]:
        field = await page.query_selector(selector)
        if field:
            await field.fill(job_title)
            await human_delay(500, 1000)
            return
    raise RuntimeError("Could not find keyword search field")


async def _enter_location(page: Page, location: str) -> None:
    for selector in [
        "input[name='location']",
        "input[placeholder*='Ort']",
        "input[placeholder*='Stadt']",
        "#location",
    ]:
        field = await page.query_selector(selector)
        if field:
            await field.fill("")
            await human_delay(200, 400)
            await field.type(location, delay=50)
            await human_delay(1000, 2000)
            suggestion = await page.query_selector(
                ".autocomplete-suggestion, .location-suggestion, [role='option']"
            )
            if suggestion:
                await suggestion.click()
                await human_delay(300, 600)
            return
    raise RuntimeError("Could not find location search field")


async def _set_activity_filter(page: Page, days: int = 60) -> None:
    try:
        filter_field = await page.query_selector(
            "input[name*='activity'], input[name*='seit'], select[name*='activity']"
        )
        if filter_field:
            tag = await filter_field.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await filter_field.select_option(label=f"{days} Tage")
            await human_delay(300, 600)
    except Exception:
        pass


async def _click_search(page: Page) -> None:
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Suchen')",
        "button:has-text('Search')",
        ".search-button",
    ]:
        btn = await page.query_selector(selector)
        if btn:
            await btn.click()
            await human_delay(2000, 4000)
            return
    await page.keyboard.press("Enter")
    await human_delay(2000, 4000)


async def _get_result_count(page: Page) -> int:
    for selector in [
        ".result-count",
        ".search-results-count",
        "[data-result-count]",
        ".resultcount",
    ]:
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            digits = "".join(c for c in text if c.isdigit())
            if digits:
                return int(digits)
    cards = await page.query_selector_all(
        ".candidate-card, .miniprofile, .search-result-item, tr.result"
    )
    return len(cards)


async def _scrape_preview_cards(page: Page) -> list[SearchResult]:
    results = []
    cards = await page.query_selector_all(
        ".miniprofile, .candidate-card, .search-result-item, tr.result"
    )
    for card in cards:
        try:
            link = await card.query_selector("a[href*='profile'], a[href*='miniprofile']")
            profile_id = ""
            if link:
                href = await link.get_attribute("href") or ""
                parts = href.split("/")
                for part in reversed(parts):
                    if part.isdigit():
                        profile_id = part
                        break
            preview_text = await card.inner_text()
            if profile_id:
                results.append(SearchResult(profile_id=profile_id, preview_text=preview_text))
        except Exception:
            continue
    return results


async def search_candidates(
    page: Page,
    job_title: str,
    location: str,
) -> tuple[list[SearchResult], int | None]:
    """Search DirectSearch for candidates. Returns (results, radius_used).

    Tries increasing radius if no results found.
    Returns empty list with None radius if no candidates at any radius.
    """
    await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
    await human_delay(1000, 2000)

    for radius in RADIUS_STEPS:
        await _enter_keyword(page, job_title)
        await _enter_location(page, location)
        await _set_activity_filter(page)
        await _click_search(page)

        count = await _get_result_count(page)
        if count > 0:
            results = await _scrape_preview_cards(page)
            return results, radius

        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
        await human_delay(1000, 2000)

    return [], None

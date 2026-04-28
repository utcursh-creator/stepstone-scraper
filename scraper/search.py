"""StepStone DirectSearch: search, scrape result cards.

Based on live probing 2026-04-17:
- Single combined search field: input#searchfield__textfield
- Submit via Enter key
- Results are .miniprofile cards (10 per page default)
- Profile ID extracted from miniprofile__name href query param profileID=XXX
"""
import logging
import re
from urllib.parse import urlparse, parse_qs
from patchright.async_api import Page
from utils.delays import human_delay

logger = logging.getLogger(__name__)

DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"


class SearchResult:
    def __init__(self, profile_id: str, preview_text: str, profile_url: str = "", cv_url: str = ""):
        self.profile_id = profile_id
        self.preview_text = preview_text
        self.profile_url = profile_url
        self.cv_url = cv_url


async def _kill_cookie_banner(page: Page) -> None:
    """Accept cookies + inject CSS to permanently hide any late-loading banner."""
    for sel in [
        "button:has-text('Alles akzeptieren')",
        "button:has-text('Alle akzeptieren')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                try:
                    await btn.click(force=True, timeout=5000)
                    await human_delay(800, 1500)
                except Exception:
                    pass
        except Exception:
            pass
    try:
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
    except Exception:
        pass


def _extract_profile_id(href: str) -> str:
    """Extract profileID query param from a profile link."""
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        return params.get("profileID", [""])[0]
    except Exception:
        return ""


async def _scrape_cards(page: Page) -> list[SearchResult]:
    """Extract candidate cards from the current results page."""
    results: list[SearchResult] = []
    cards = await page.query_selector_all(".miniprofile")
    if cards:
        first_html = await cards[0].inner_html()
        logger.info(f"DEBUG FIRST CARD HTML:\n{first_html[:3000]}")
        first_outer = await cards[0].outer_html()
        logger.info(f"DEBUG FIRST CARD OUTER:\n{first_outer[:500]}")
    for card in cards:
        try:
            # Profile URL + ID from .miniprofile__name link
            link = await card.query_selector("a.miniprofile__name")
            profile_url = ""
            profile_id = ""
            if link:
                profile_url = await link.get_attribute("href") or ""
                profile_id = _extract_profile_id(profile_url)

            # CV URL from the CV action link
            cv_url = ""
            cv_link = await card.query_selector("a.miniprofile__actionlink[href*='downloadAttachment'], a.miniprofile__attachmentdocument")
            if cv_link:
                cv_url = await cv_link.get_attribute("href") or ""

            preview_text = (await card.inner_text()).strip()

            if profile_id:
                results.append(SearchResult(
                    profile_id=profile_id,
                    preview_text=preview_text,
                    profile_url=profile_url,
                    cv_url=cv_url,
                ))
        except Exception:
            continue
    return results


async def search_candidates(
    page: Page,
    job_title: str,
    location: str,
) -> tuple[list[SearchResult], int | None]:
    """Search DirectSearch for candidates.

    Uses the combined standard-mode search field. Location is appended to the
    keyword string; StepStone's backend handles parsing.

    Returns (results, radius_used). Radius is informational only - combined field
    doesn't expose a discrete radius knob; StepStone uses relevance/proximity by default.
    """
    await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    await human_delay(2000, 3500)
    await _kill_cookie_banner(page)
    await human_delay(1000, 2000)

    field = await page.query_selector("#searchfield__textfield")
    if not field:
        raise RuntimeError("DirectSearch field #searchfield__textfield not found")

    query = f"{job_title} {location}".strip()
    try:
        await field.click(force=True, timeout=10000)
    except Exception:
        await field.focus()
    await human_delay(300, 700)
    await field.fill(query)
    await human_delay(1000, 2000)

    # Submit via Enter
    await field.press("Enter")
    await human_delay(5000, 7000)
    await _kill_cookie_banner(page)

    results = await _scrape_cards(page)
    radius = 25 if results else None  # informational
    return results, radius

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
    def __init__(
        self,
        profile_id: str,
        preview_text: str,
        profile_url: str = "",
        cv_url: str = "",
        wohnort: str = "",
        has_cv_attachment: bool = False,
        gewuenschte_arbeitsorte: list[str] | None = None,
    ):
        self.profile_id = profile_id
        self.preview_text = preview_text
        self.profile_url = profile_url
        self.cv_url = cv_url
        self.wohnort = wohnort
        self.has_cv_attachment = has_cv_attachment
        self.gewuenschte_arbeitsorte = gewuenschte_arbeitsorte or []


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


RE_WOHNORT_CARD = re.compile(r"([^\n]+?)\s*\(Wohnort\)", re.IGNORECASE)


def _extract_wohnort_from_card(text: str) -> str:
    """Extract city name from card text. Format: 'Dortmund (Wohnort)'"""
    m = RE_WOHNORT_CARD.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _extract_gewuenschte_from_card(text: str) -> list[str]:
    """Extract desired work locations from card text.

    Card format: 'bochum | Dortmund | essen (gewünschte Arbeitsorte)'
    Returns list of city names.
    """
    m = re.search(r"([^\n]+?)\s*\(gewünschte Arbeitsorte\)", text, re.IGNORECASE)
    if m:
        raw = m.group(1)
        return [loc.strip() for loc in raw.split("|") if loc.strip()]
    return []


def _has_cv_attachment(text: str) -> bool:
    """Check if the card has a CV attachment in the Anhänge section."""
    return bool(re.search(r"Anhänge.*?\.pdf", text, re.IGNORECASE | re.DOTALL))


async def _scrape_cards(page: Page) -> list[SearchResult]:
    """Extract candidate cards from the current results page."""
    results: list[SearchResult] = []

    # Proactive fix: inject JS to remove ng-hide from all card elements.
    # This is harmless if ng-hide is not present but fixes rendering issues
    # where Angular hasn't finished removing ng-hide classes.
    try:
        await page.evaluate("""
            document.querySelectorAll('.miniprofile [ng-hide]').forEach(el => {
                el.classList.remove('ng-hide');
                el.style.display = '';
            });
        """)
        await human_delay(500, 1000)
    except Exception as e:
        logger.warning(f"ng-hide removal failed (non-fatal): {e}")

    # Wait for Angular to finish rendering card details
    try:
        await page.wait_for_selector(
            "text=Persönliche Angaben",
            timeout=15000,
        )
        logger.info("Card details rendered (found 'Persönliche Angaben')")
    except Exception:
        logger.warning(
            "Timed out waiting for card details to render. "
            "Cards may still have ng-hide on personal details. "
            "Proceeding with available data."
        )
        # Second attempt: force ng-hide removal after timeout
        try:
            await page.evaluate("""
                document.querySelectorAll('.miniprofile [ng-hide]').forEach(el => {
                    el.classList.remove('ng-hide');
                    el.style.display = '';
                });
            """)
            await human_delay(500, 1000)
        except Exception:
            pass

    cards = await page.query_selector_all(".miniprofile")

    # Diagnostic logging for first card
    if cards:
        try:
            first_html = await cards[0].inner_html()
            first_text = await cards[0].inner_text()
            has_wohnort = "(Wohnort)" in first_text
            has_anhaenge = "Anhänge" in first_text
            logger.info(
                f"DEBUG FIRST CARD: "
                f"html_len={len(first_html)}, text_len={len(first_text)}, "
                f"has_wohnort={has_wohnort}, has_anhaenge={has_anhaenge}"
            )
            if not has_wohnort:
                logger.warning(
                    "First card does NOT contain '(Wohnort)' in inner_text. "
                    "ng-hide may still be active."
                )
                logger.info(f"DEBUG FIRST CARD HTML (first 2000):\n{first_html[:2000]}")
        except Exception as e:
            logger.warning(f"DEBUG card inspection failed: {e}")

    for card in cards:
        try:
            # Profile URL + ID
            link = await card.query_selector("a.miniprofile__name")
            profile_url = ""
            profile_id = ""
            if link:
                profile_url = await link.get_attribute("href") or ""
                profile_id = _extract_profile_id(profile_url)

            # CV URL from action link
            cv_url = ""
            cv_link = await card.query_selector(
                "a.miniprofile__actionlink[href*='downloadAttachment'], "
                "a.miniprofile__attachmentdocument"
            )
            if cv_link:
                cv_url = await cv_link.get_attribute("href") or ""

            preview_text = (await card.inner_text()).strip()

            # Extract pre-unlock gate data from card text
            wohnort = _extract_wohnort_from_card(preview_text)
            has_cv = _has_cv_attachment(preview_text)
            gewuenschte = _extract_gewuenschte_from_card(preview_text)

            if profile_id:
                results.append(SearchResult(
                    profile_id=profile_id,
                    preview_text=preview_text,
                    profile_url=profile_url,
                    cv_url=cv_url,
                    wohnort=wohnort,
                    has_cv_attachment=has_cv,
                    gewuenschte_arbeitsorte=gewuenschte,
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
    await human_delay(8000, 12000)
    await _kill_cookie_banner(page)

    results = await _scrape_cards(page)
    radius = 25 if results else None  # informational

    # Log pre-unlock gate stats
    with_wohnort = sum(1 for r in results if r.wohnort)
    with_cv = sum(1 for r in results if r.has_cv_attachment)
    logger.info(
        f"Card extraction stats: {len(results)} total, "
        f"{with_wohnort} with Wohnort, {with_cv} with CV attachment"
    )

    return results, radius

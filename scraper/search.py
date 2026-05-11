"""StepStone DirectSearch: structured-criteria search via autosuggest, with
backend radius filter (per-job `max_distance_km`) and page-size 50 pagination.

Pipeline (verified end-to-end against live DOM, see /tmp/stepstone_form_probe.md):
  1. Navigate to DirectSearch + dismiss cookie banner
  2. Type job_title (gender markers stripped) → ArrowDown → Enter → adds
     structured `job_title` criterion if autosuggest offers one, else falls
     back to `keyword` criterion. Either is better than free-text.
  3. Clear the search field (StepStone does NOT auto-clear after Enter).
  4. Type location → ArrowDown → Enter → adds `country/Ort` criterion with
     default 25 km radius. If autosuggest has no country section (unknown
     city), falls through to `keyword` — a warning is logged, the local
     distance gate in main.py compensates.
  5. If the requested radius != 25 km, hijack the AngularJS scope via a DOM-
     injected <script> tag (Patchright's evaluate runs in an isolated world
     that can't see window.angular; injecting a real <script> bypasses that)
     and set `query.criteria[country].value` to the matching slider index.
     This causes Angular's $watch chain to re-fire the search with the new
     radius — verified: 42 of 50 cards differ between 25 km and 100 km in
     Pulheim probe.
  6. Click the page-size = 50 toggle so we get 50 cards per request instead
     of the default 10. (`page.incrementCount()` aka "Mehr Suchergebnisse"
     was inert in our probe, so size-50 is the pagination mechanism we use.)
  7. Wait for results to settle, scrape `.miniprofile` cards.
"""
import asyncio
import logging
import re
from urllib.parse import urlparse, parse_qs

from patchright.async_api import Page

from utils.delays import human_delay

logger = logging.getLogger(__name__)

DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"

# StepStone slider supports exactly these km values; map to the directive's
# zero-based index. Verified live: chip text "+ 25km" with `criterion.value=3`.
RADIUS_KM_OPTIONS = (0, 5, 10, 25, 50, 75, 100)
RADIUS_KM_TO_INDEX = {km: idx for idx, km in enumerate(RADIUS_KM_OPTIONS)}
DEFAULT_RADIUS_KM = 25  # StepStone's own default — no hijack needed for this


def _km_to_radius_index(km: int) -> int:
    """Round `km` UP to the nearest StepStone slider value, return its index.

    Rounding up (rather than nearest) is deliberate: a recruiter tagging
    `30km` should NOT silently get the narrower `25km` filter. They get the
    next-larger available radius (`50km`), and the local distance gate
    in main.py still rejects anyone beyond their actual `max_distance_km`.
    """
    if km is None or km <= 0:
        return RADIUS_KM_TO_INDEX[DEFAULT_RADIUS_KM]
    for option in RADIUS_KM_OPTIONS:
        if option >= km:
            return RADIUS_KM_TO_INDEX[option]
    return RADIUS_KM_TO_INDEX[100]


# Strip gender markers like "(m/w/d)", "(w/m/d)", "(m/w)", " m/w/d " from
# job titles so StepStone's autosuggest can match the actual position name.
# The probe confirmed that "Bauleiter" → 3 structured `job_title` matches,
# whereas "Bauleiter (m/w/d)" would force a keyword fallback.
_GENDER_MARKER_RE = re.compile(r"\s*\(\s*[mwdfMWDF/\s]+\s*\)\s*")


def _strip_gender_marker(text: str) -> str:
    return _GENDER_MARKER_RE.sub(" ", text or "").strip()


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
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        return parse_qs(parsed.query).get("profileID", [""])[0]
    except Exception:
        return ""


RE_WOHNORT_CARD = re.compile(r"([^\n]+?)\s*\(Wohnort\)", re.IGNORECASE)


def _extract_wohnort_from_card(text: str) -> str:
    m = RE_WOHNORT_CARD.search(text)
    return m.group(1).strip() if m else ""


def _extract_gewuenschte_from_card(text: str) -> list[str]:
    m = re.search(r"([^\n]+?)\s*\(gewünschte Arbeitsorte\)", text, re.IGNORECASE)
    if m:
        return [loc.strip() for loc in m.group(1).split("|") if loc.strip()]
    return []


async def _scrape_cards(page: Page) -> list[SearchResult]:
    """Extract candidate cards from the current results page."""
    results: list[SearchResult] = []

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

    try:
        await page.wait_for_selector("text=Persönliche Angaben", timeout=15000)
        logger.info("Card details rendered (found 'Persönliche Angaben')")
    except Exception:
        logger.warning(
            "Timed out waiting for card details to render. "
            "Proceeding with available data."
        )
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

    if cards:
        try:
            first_text = await cards[0].inner_text()
            logger.info(
                f"DEBUG FIRST CARD: text_len={len(first_text)}, "
                f"has_wohnort={'(Wohnort)' in first_text}, "
                f"has_anhaenge={'Anhänge' in first_text}"
            )
        except Exception as e:
            logger.warning(f"DEBUG card inspection failed: {e}")

    for card in cards:
        try:
            link = await card.query_selector("a.miniprofile__name")
            profile_url = ""
            profile_id = ""
            if link:
                profile_url = await link.get_attribute("href") or ""
                profile_id = _extract_profile_id(profile_url)

            cv_url = ""
            cv_link = await card.query_selector(
                "a.miniprofile__actionlink[href*='downloadAttachment'], "
                "a.miniprofile__attachmentdocument"
            )
            if cv_link:
                cv_url = await cv_link.get_attribute("href") or ""

            preview_text = (await card.inner_text()).strip()

            wohnort = _extract_wohnort_from_card(preview_text)
            # Structural CV check: the .miniprofile__attachmentdocument element
            # exists in the card's DOM iff the candidate has a CV attached.
            # The old text-regex r"Anhänge.*?\.pdf" was DOTALL-greedy and produced
            # false positives when "Anhänge" appeared near any unrelated ".pdf"
            # string in the card text — letting CV-less candidates through the
            # pre-unlock gate and into Recruitee with no CV.
            has_cv = bool(cv_url)
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


async def _add_criterion_via_autosuggest(page: Page, term: str) -> None:
    """Type `term` into the search field, wait for autosuggest, ArrowDown to
    highlight the first match, Enter to commit it as a structured criterion.

    Why ArrowDown then Enter: the probe confirmed that pressing Enter without
    arrow-navigating leaves all autosuggest items unhighlighted, and StepStone
    treats that as a free-text keyword commit (the bug the old scraper had).
    One ArrowDown highlights the first item — for cities, that's section-country
    (Ort/Wohnort); for job titles, section-job_title; for unknowns, section-keyword.

    Field clear is REQUIRED — StepStone does NOT auto-clear after Enter, so
    typing the next criterion without `field.fill('')` produces a concatenated
    keyword like "MünchenBauleiter".
    """
    field = await page.query_selector("#searchfield__textfield")
    if not field:
        raise RuntimeError("DirectSearch field #searchfield__textfield not found")

    await field.click(force=True, timeout=10000)
    await field.fill("")  # see docstring — required between criteria
    await human_delay(300, 600)

    # type char-by-char with delay so the autosuggest debounce fires; bulk fill()
    # bypasses Angular's input watchers and the dropdown never renders.
    for ch in term:
        await field.type(ch, delay=80)

    await human_delay(2000, 3000)  # autosuggest debounce + render
    await field.press("ArrowDown")
    await human_delay(300, 600)
    await field.press("Enter")
    await human_delay(2500, 4000)  # criterion commit + auto re-search


async def _country_chip_present(page: Page) -> bool:
    """Returns True if a country/Ort chip is rendered in the query box.

    Used to detect whether structured location filtering is active. The chip's
    distinguishing text is "Umkreis (km)" — only the country criterion has
    that label (job_title shows "Berufserfahrung", keyword has no label).
    """
    return await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.querybox__criteria .criteria'))
            .some(c => (c.innerText || '').includes('Umkreis'));
    }""")


async def _set_radius_km(page: Page, km: int) -> bool:
    """Set the country criterion's radius to `km` via Angular scope hijack.

    Patchright's `page.evaluate` runs in an isolated world that cannot see
    `window.angular` or `window.DirectSearch`. The bypass: append a real
    `<script>` element via `document.createElement('script')`. Inline scripts
    execute synchronously in the page's main world where everything IS
    accessible. The script writes its result to a DOM attribute we then read
    back from the isolated world.

    The country criterion has no `type`/`code` field — we identify it by its
    `sliderValues` signature `[0,5,10,25,50,75,100]` which is unique among the
    StepStone facets (job_title's sliderValues are `[0,1,2,3,4,6,10+]`).

    Setting `criterion.value` triggers the directive's $watch chain, which
    fires the slider's `change` callback (in misc_directives.js), which calls
    `scope.$apply(scope.value = ui.value)`. That re-runs the search with the
    new radius. Verified live: 42 of 50 cards differ between 25 km and 100 km
    on Pulheim Bauleiter probe.

    Returns True on success.
    """
    target_idx = _km_to_radius_index(km)
    await page.evaluate(
        """(targetIdx) => {
            document.documentElement.removeAttribute('data-radius-result');
            const s = document.createElement('script');
            s.textContent = `
            (function() {
              try {
                const inj = angular.element(document.body).injector();
                const query = inj.get('query');
                const $rs = inj.get('$rootScope');
                const country = query.criteria.find(c =>
                    c && c.sliderValues
                    && JSON.stringify(c.sliderValues) === '["0","5","10","25","50","75","100"]'
                );
                if (!country) {
                    document.documentElement.setAttribute('data-radius-result', 'no_country_criterion');
                    return;
                }
                country.value = ${targetIdx};
                $rs.$apply();
                document.documentElement.setAttribute('data-radius-result', 'success:' + country.value);
              } catch (e) {
                document.documentElement.setAttribute('data-radius-result', 'err:' + e.message);
              }
            })();
            `;
            document.documentElement.appendChild(s);
            s.remove();
        }""",
        target_idx,
    )
    await asyncio.sleep(2)  # let Angular digest + the auto-research begin
    result = await page.evaluate(
        "() => document.documentElement.getAttribute('data-radius-result')"
    )
    if result and result.startswith("success"):
        logger.info(f"Radius set: {km}km (idx={target_idx}) — {result}")
        return True
    logger.warning(f"Radius hijack failed for {km}km: {result!r}")
    return False


async def _set_page_size(page: Page, size: int = 50) -> bool:
    """Click the page-size toggle (10 | 25 | 50). Defaults to 50."""
    clicked = await page.evaluate(
        """(size) => {
            const els = Array.from(document.querySelectorAll("[ng-click='page.setSize(option)']"));
            const target = els.find(el => el.innerText.trim() === String(size));
            if (target) { target.click(); return true; }
            return false;
        }""",
        size,
    )
    if clicked:
        logger.info(f"Page size set to {size}")
        return True
    logger.warning(f"Page-size {size} option not found in toggle")
    return False


async def search_candidates(
    page: Page,
    job_title: str,
    location: str,
    max_distance_km: int = DEFAULT_RADIUS_KM,
) -> tuple[list[SearchResult], int | None]:
    """Search DirectSearch for candidates with structured location + radius.

    Returns (results, used_radius_km). `used_radius_km` is the actual km
    applied to StepStone's backend (after rounding to the nearest available
    slider step), or None if the structured location filter could not be
    applied (city not in StepStone's gazetteer — caller falls back to local
    distance gate only).
    """
    await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    await human_delay(2000, 3500)
    await _kill_cookie_banner(page)
    await human_delay(1000, 2000)

    field = await page.query_selector("#searchfield__textfield")
    if not field:
        raise RuntimeError("DirectSearch field #searchfield__textfield not found")

    # 2. Add structured job_title criterion (drops "(m/w/d)" gender markers)
    clean_title = _strip_gender_marker(job_title)
    if not clean_title:
        clean_title = job_title  # don't strip everything; fall back to original
    logger.info(f"Adding criterion job_title={clean_title!r} (orig: {job_title!r})")
    await _add_criterion_via_autosuggest(page, clean_title)

    # 3. Add structured location criterion (defaults to 25 km radius)
    logger.info(f"Adding criterion location={location!r}")
    await _add_criterion_via_autosuggest(page, location)

    # 4. Verify country chip rendered — if not, structured radius can't apply
    has_country = await _country_chip_present(page)
    used_radius_km: int | None = None

    if not has_country:
        logger.warning(
            f"No country/Ort chip rendered for location={location!r}. "
            f"Likely cause: city not in StepStone's gazetteer. "
            f"Falling back to keyword-only filter; main.py distance gate "
            f"(max_distance_km={max_distance_km}) will reject far candidates."
        )
    else:
        # 5. Adjust radius if user requested non-default
        if max_distance_km == DEFAULT_RADIUS_KM:
            used_radius_km = DEFAULT_RADIUS_KM
        else:
            ok = await _set_radius_km(page, max_distance_km)
            if ok:
                target_idx = _km_to_radius_index(max_distance_km)
                used_radius_km = RADIUS_KM_OPTIONS[target_idx]
            else:
                used_radius_km = DEFAULT_RADIUS_KM
                logger.warning(
                    f"Radius hijack failed; StepStone backend keeps default 25km. "
                    f"main.py local gate (max_distance_km={max_distance_km}) compensates."
                )

    # 6. Bump page size to 50 — single click on the size selector
    await _set_page_size(page, 50)

    # 7. Wait for results to settle (page-size change + any pending re-search)
    await human_delay(8000, 12000)
    await _kill_cookie_banner(page)

    # 8. Scrape
    results = await _scrape_cards(page)
    with_wohnort = sum(1 for r in results if r.wohnort)
    with_cv = sum(1 for r in results if r.has_cv_attachment)
    logger.info(
        f"Card extraction stats: {len(results)} total, "
        f"{with_wohnort} with Wohnort, {with_cv} with CV attachment, "
        f"backend_radius_km={used_radius_km}, structured_location={has_country}"
    )

    return results, used_radius_km

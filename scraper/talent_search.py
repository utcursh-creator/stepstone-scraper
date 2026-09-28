"""Search Stepstone Recruit's Talent Finder.

Replaces the DirectSearch page-driving in scraper/search.py. Talent Finder is a
React app over a JSON API. The scraper asks that API for results the same way
the page does, from inside the logged-in page:

  POST /recruiter/talent-sourcing/api/v1/search?size=20&pageNumber=N&sortOrder=RELEVANCE
  {"keyword": ..., "locations": [{"locationName": ..., "radius": km}], "rawQuery": ...}

Mapped on 2026-09-25 from a structure-only probe of the live app and from its
public JavaScript (/static/gts/ui-talent-sourcing/):
  * radius is in km, and the app only offers 40/60/80/100/150 (0 = "Optimiert").
    StepStone gets the nearest option at or above the job's distance; the exact
    limit is applied locally, on the candidate's postcode.
  * keyword uses StepStone's boolean syntax: AND / OR / NOT, quotes, brackets.
  * a plain fetch() from the PAGE world is accepted (the app authenticates by
    session cookie). From patchright's isolated world it was refused.
  * totalElements is StepStone's own count, so a genuine zero can no longer be
    confused with a search that failed (the 2026-08-10 batch).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import quote, urlparse

from patchright.async_api import Page

from scraper.auth import APP_HOST, TALENT_FINDER_URL
from utils.delays import human_delay

logger = logging.getLogger(__name__)

SEARCH_PATH = "/recruiter/talent-sourcing/api/v1/search"
LOCATION_SUGGEST_PATH = "/recruiter/talent-sourcing/api/v1/autosuggestion/locations"
PROFILE_PATH = "/talent-sourcing/results/"
PAGE_SIZE = 20                      # what the app itself requests
MAX_PAGES = 5                       # at most 100 results per job
RADIUS_OPTIONS_KM = (40, 60, 80, 100, 150)
PAGE_DELAY_MS = (2500, 6000)        # between result pages, like a person paging
RETRY_DELAY_MS = (5000, 9000)
PREVIEW_MAX_CHARS = 4000

_GENDER_MARKER_RE = re.compile(r"\s*\(\s*[mwdfMWDF/\s]+\s*\)\s*")
_BOOLEAN_WORD_RE = re.compile(r"\b(and|or|not)\b", re.IGNORECASE)
_HOME_COUNTRIES = {"DE", "DEU", "GERMANY", "DEUTSCHLAND"}

_FETCH_JS = """
async ({ url, method, body }) => {
  // A request stalled at the proxy must fail, not hang the job (and the chain) forever.
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 60000);
  try {
    const init = { method, credentials: 'include', headers: { accept: 'application/json' }, signal: ctl.signal };
    if (body !== null && body !== undefined) {
      init.headers['content-type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const r = await fetch(url, init);
    let json = null;
    try { json = await r.json(); } catch (e) {}
    return { status: r.status, body: json };
  } catch (e) {
    return { status: 0, error: String(e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


class SearchError(Exception):
    """`code` is a stable token for routing; str() starts with it."""

    def __init__(self, message: str, code: str = "SEARCH_FAILED"):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class SearchResult:
    """One Talent Finder candidate, BEFORE any unlock. The first seven fields
    keep the names main.py's pre-unlock gates already read."""
    profile_id: str
    preview_text: str
    profile_url: str = ""
    cv_url: str = ""
    wohnort: str = ""
    has_cv_attachment: bool = False
    gewuenschte_arbeitsorte: list[str] = field(default_factory=list)
    is_locked: bool = True
    display_name: str = ""          # "Sarah J." before unlock; never sent to the LLM
    postal_code: str = ""
    city: str = ""
    languages: list[tuple[str, str]] = field(default_factory=list)
    last_activity: str = ""
    relevant_months: int | None = None
    score: float | None = None


# --------------------------------------------------------------- query parts

def clean_job_title(title: str) -> str:
    """'SAP Consultant Archivierung (m/w/d)' -> 'SAP Consultant Archivierung'.
    Also drops quotes and brackets, which are syntax in StepStone's keyword field."""
    text = _GENDER_MARKER_RE.sub(" ", title or "")
    text = re.sub(r'["()]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -,/")
    return text or (title or "").strip()


def _literal(text: str) -> str:
    """One term for StepStone's boolean syntax. Quoted when it has spaces or an
    and/or/not word, so 'Sales and Marketing' cannot turn into an AND."""
    t = re.sub(r'["()]', " ", text or "").replace(",", " ")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    return f'"{t}"' if (" " in t or _BOOLEAN_WORD_RE.search(t)) else t


def build_keyword(job_title: str, keywords: list[str] | None = None) -> str:
    """The job title, plus every job keyword as a MUST (the old STICHWORT meaning)."""
    title = clean_job_title(job_title)
    if _BOOLEAN_WORD_RE.search(title):
        title = f'"{title}"'
    terms = [t for t in (_literal(k) for k in (keywords or [])) if t]
    if not terms:
        return title
    head = f"({title})" if " " in title and not title.startswith('"') else title
    return " AND ".join([head, *terms])


def backend_radius_km(max_distance_km) -> int:
    """StepStone offers 40/60/80/100/150 km; 0 means 'Optimiert' (StepStone decides).
    The smallest option that still COVERS the job's distance, so no candidate
    inside the real limit is lost; the exact limit is enforced locally."""
    try:
        wanted = int(max_distance_km or 0)
    except (TypeError, ValueError):
        wanted = 0
    if wanted <= 0:
        return 0
    for option in RADIUS_OPTIONS_KM:
        if wanted <= option:
            return option
    return RADIUS_OPTIONS_KM[-1]


# ------------------------------------------------------------ record mapping

def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _names(values) -> list[str]:
    """Names from a list of strings or of objects (shape not pinned down yet)."""
    out = []
    for v in values or []:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, dict):
            for key in ("displayName", "displayString", "name", "locationName", "city", "title", "label", "value"):
                if isinstance(v.get(key), str) and v[key].strip():
                    out.append(v[key].strip())
                    break
    return out


def _languages(values) -> list[tuple[str, str]]:
    out = []
    for v in values or []:
        if isinstance(v, dict):
            name = _text(v.get("languageId")) or _text(v.get("name")) or _text(v.get("language"))
            if name:
                out.append((name, _text(v.get("level"))))
        elif isinstance(v, str) and v.strip():
            out.append((v.strip(), ""))
    return out


def _period(start, end, current) -> str:
    s = _text(start)
    e = "heute" if current else _text(end)
    return f" ({s} – {e})" if (s or e) else ""


def build_preview(raw: dict, wohnort: str, desired_locations: list[str],
                  languages: list[tuple[str, str]]) -> str:
    """What the AI evaluation reads. Structured facts only: no name, no email,
    no phone. Less personal data leaves for the LLM than with DirectSearch cards."""
    lines = []
    title = _text(raw.get("currentJobTitle"))
    if title:
        lines.append(f"Aktuelle Position: {title}")
    months, matches = raw.get("relevantMonthsCount"), raw.get("matchingExperienceCount")
    if isinstance(months, int):
        extra = f" ({matches} passende Stationen)" if isinstance(matches, int) else ""
        lines.append(f"Relevante Berufserfahrung laut StepStone: {months} Monate{extra}")
    experiences = [e for e in (raw.get("workExperiences") or []) if isinstance(e, dict)]
    if experiences:
        lines.append("Berufserfahrung:")
        for e in experiences[:12]:
            job, company = _text(e.get("jobTitle")), _text(e.get("companyName"))
            line = "- " + (job or "Position ohne Titel") + (f" bei {company}" if company else "")
            line += _period(e.get("startDate"), e.get("endDate"), e.get("isCurrentJob"))
            description = " ".join(_text(e.get("jobDescription")).split())[:280]
            tasks = [t for t in (e.get("tasks") or []) if isinstance(t, str) and t.strip()]
            if description:
                line += f": {description}"
            elif tasks:
                line += ": " + "; ".join(t.strip() for t in tasks[:5])
            lines.append(line)
    educations = [e for e in (raw.get("educations") or []) if isinstance(e, dict)]
    if educations:
        lines.append("Ausbildung:")
        for e in educations[:6]:
            parts = [p for p in (_text(e.get("qualification")), _text(e.get("courseTitle"))) if p]
            institution = _text(e.get("institution"))
            year = e.get("completionYear")
            line = "- " + (": ".join(parts) or "Ausbildung")
            if institution:
                line += f", {institution}"
            if isinstance(year, int):
                line += f" ({year})"
            lines.append(line)
    skills = [s.strip() for s in (raw.get("skills") or []) if isinstance(s, str) and s.strip()]
    if skills:
        lines.append("Kenntnisse: " + ", ".join(skills[:40]))
    if languages:
        lines.append("Sprachen: " + ", ".join(f"{n} ({lvl})" if lvl else n for n, lvl in languages))
    prefs = raw.get("jobPreferences") or {}
    wanted_titles = _names(prefs.get("desiredJobTitles"))
    if wanted_titles:
        lines.append("Gewünschte Jobtitel: " + ", ".join(wanted_titles))
    if desired_locations:
        lines.append("Gewünschte Arbeitsorte: " + ", ".join(desired_locations))
    for label, key in (("Gewünschte Arbeitszeit", "desiredWorkTypes"),
                       ("Gewünschte Vertragsart", "desiredContractTypes"),
                       ("Arbeitsplatztyp", "remoteWorkTypes")):
        values = _names(prefs.get(key))
        if values:
            lines.append(f"{label}: " + ", ".join(values))
    if wohnort:
        lines.append(f"Wohnort: {wohnort}")
    lines.append("Lebenslauf vorhanden: " + ("ja" if raw.get("hasCv") else "nein"))
    last_activity = _text(raw.get("lastActivity"))
    if last_activity:
        lines.append(f"Zuletzt aktiv: {last_activity[:10]}")
    urgency = _text(raw.get("jobSearchUrgency"))
    if urgency:
        lines.append(f"Suchstatus: {urgency}")
    notice = raw.get("noticePeriod") or raw.get("noticePeriodCategory")
    if isinstance(notice, (str, int)) and str(notice).strip():
        lines.append(f"Kündigungsfrist: {notice}")
    if raw.get("hasDrivingLicense"):
        classes = _names(raw.get("drivingLicenses"))
        lines.append("Führerschein: ja" + (f" ({', '.join(classes)})" if classes else ""))
    return "\n".join(lines)[:PREVIEW_MAX_CHARS]


def to_search_result(raw: dict) -> SearchResult | None:
    if not isinstance(raw, dict):
        return None
    profile_id = _text(raw.get("id"))
    if not profile_id:
        return None
    info = raw.get("personalInfo") or {}
    address = info.get("address") or {}
    postal, city, country = (_text(address.get("postalCode")), _text(address.get("city")),
                             _text(address.get("country")))
    wohnort = " ".join(p for p in (postal, city) if p)
    if wohnort and country and country.upper() not in _HOME_COUNTRIES:
        wohnort = f"{wohnort}, {country}"
    desired = _names((raw.get("jobPreferences") or {}).get("desiredJobLocations"))
    languages = _languages(raw.get("languages"))
    first, last = _text(info.get("firstName")), _text(info.get("lastName"))
    score = raw.get("score")
    months = raw.get("relevantMonthsCount")
    return SearchResult(
        profile_id=profile_id,
        preview_text=build_preview(raw, wohnort, desired, languages),
        profile_url=f"https://{APP_HOST}{PROFILE_PATH}{profile_id}",
        wohnort=wohnort,
        has_cv_attachment=bool(raw.get("hasCv")),
        gewuenschte_arbeitsorte=desired,
        is_locked=raw.get("isLocked") is not False,   # unknown counts as locked
        display_name=" ".join(p for p in (first, last) if p),
        postal_code=postal,
        city=city,
        languages=languages,
        last_activity=_text(raw.get("lastActivity")),
        relevant_months=months if isinstance(months, int) else None,
        score=score if isinstance(score, (int, float)) else None,
    )


# ----------------------------------------------------------------- transport

async def _call(page: Page, url: str, method: str = "GET", body=None) -> dict:
    """One request from the PAGE world (the only one StepStone accepted), with a
    single retry on a network error or a 5xx."""
    for attempt in (1, 2):
        try:
            res = await page.evaluate(_FETCH_JS, {"url": url, "method": method, "body": body},
                                      isolated_context=False)
        except Exception as e:
            res = {"status": 0, "error": f"{type(e).__name__}: {e}"}
        status = res.get("status") if isinstance(res, dict) else 0
        if status == 0 or (isinstance(status, int) and status >= 500):
            if attempt == 1:
                logger.warning(f"Talent Finder request {method} {urlparse(url).path} -> {status or 'network error'}; retrying once")
                await human_delay(*RETRY_DELAY_MS)
                continue
        return res if isinstance(res, dict) else {"status": 0, "error": "no result"}
    return res


def _raise_for(res: dict, what: str) -> None:
    status = res.get("status")
    if status == 200:
        return
    detail = res.get("error") or str(res.get("body"))[:200]
    if status in (401, 403):
        raise SearchError(f"{what}: StepStone answered {status}; the session is no longer valid. {detail}",
                          code="SEARCH_SESSION_LOST")
    if status == 429:
        raise SearchError(f"{what}: StepStone rate-limited the search (429). Do not re-run immediately.",
                          code="SEARCH_RATE_LIMITED")
    if status == 0 or (isinstance(status, int) and status >= 500):
        raise SearchError(f"{what}: StepStone unavailable ({status or 'network error'}) after a retry. {detail}",
                          code="SEARCH_UNAVAILABLE")
    raise SearchError(f"{what}: StepStone rejected the request ({status}): {detail}", code="SEARCH_REJECTED")


async def _ensure_talent_finder(page: Page) -> None:
    p = urlparse(page.url or "")
    if (p.hostname or "") != APP_HOST or not p.path.startswith("/talent-sourcing"):
        await page.goto(TALENT_FINDER_URL, wait_until="domcontentloaded")
        await human_delay(1500, 3000)
    if (urlparse(page.url or "").hostname or "") != APP_HOST:
        raise SearchError(f"not in the app (at {urlparse(page.url or '').hostname}); the session was lost",
                          code="SEARCH_SESSION_LOST")


async def resolve_location(page: Page, location: str) -> str:
    """The location name as StepStone knows it, from its own suggestions. A place
    StepStone has no suggestion for would otherwise search as a silent zero."""
    wanted = (location or "").strip()
    if not wanted:
        raise SearchError("the job has no location", code="SEARCH_LOCATION_UNKNOWN")
    url = f"{LOCATION_SUGGEST_PATH}?prefix={quote(wanted)}&language=de&country=DE"
    res = await _call(page, url)
    if res.get("status") != 200:
        logger.warning(f"Location suggestions unavailable ({res.get('status')}); searching '{wanted}' as typed")
        return wanted
    body = res.get("body")
    if isinstance(body, dict):
        body = body.get("content") or body.get("suggestions") or body.get("items") or []
    names = _names(body if isinstance(body, list) else [])
    if not names:
        raise SearchError(f"StepStone does not know the location {wanted!r} (no suggestions). "
                          f"Fix the job's location in Recruitee.", code="SEARCH_LOCATION_UNKNOWN")
    lower = wanted.lower()
    for name in names:
        if name.lower() == lower:
            return name
    for name in names:
        if name.lower().startswith(lower) or lower.startswith(name.lower()):
            return name
    logger.warning(f"No exact location match for {wanted!r}; using StepStone's first suggestion {names[0]!r}")
    return names[0]


# -------------------------------------------------------------------- search

@dataclass
class SearchOutcome:
    """A search whose first page is loaded. Further pages load lazily in
    iterate(), so a job that fills its cap early never requests them."""
    keyword: str
    location_name: str
    radius_km: int
    total: int
    total_pages: int
    criteria_id: int | None
    first_page: list[SearchResult]
    fetch_page: Callable[[int], Awaitable[dict]]
    max_pages: int = MAX_PAGES
    keyword_fallback: bool = False
    pages_fetched: int = 1
    skipped_records: int = 0

    async def iterate(self) -> AsyncIterator[SearchResult]:
        for result in self.first_page:
            yield result
        page_number = 1
        while page_number < min(self.total_pages, self.max_pages):
            await human_delay(*PAGE_DELAY_MS)
            data = await self.fetch_page(page_number)
            self.pages_fetched += 1
            content = data.get("content") or []
            if not content:
                break
            for raw in content:
                result = to_search_result(raw)
                if result is None:
                    self.skipped_records += 1
                    continue
                yield result
            page_number += 1


async def _run_search(page: Page, keyword: str, location_name: str, radius_km: int,
                      max_pages: int) -> SearchOutcome:
    location = {"locationName": location_name}
    if radius_km > 0:
        location["radius"] = radius_km
    body = {"keyword": keyword, "locations": [location], "rawQuery": keyword}

    async def fetch_page(page_number: int) -> dict:
        url = f"{SEARCH_PATH}?size={PAGE_SIZE}&pageNumber={page_number}&sortOrder=RELEVANCE"
        res = await _call(page, url, method="POST", body=body)
        _raise_for(res, f"search page {page_number + 1}")
        data = res.get("body")
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise SearchError(f"search page {page_number + 1}: unexpected response shape "
                              f"({type(data).__name__})", code="SEARCH_BAD_RESPONSE")
        return data

    first = await fetch_page(0)
    total = first.get("totalElements")
    if not isinstance(total, int):
        raise SearchError("the response carries no totalElements", code="SEARCH_BAD_RESPONSE")
    if total > 0 and not first["content"]:
        raise SearchError(f"StepStone reported {total} results but returned none",
                          code="SEARCH_BAD_RESPONSE")
    results, skipped = [], 0
    for raw in first["content"]:
        result = to_search_result(raw)
        if result is None:
            skipped += 1
        else:
            results.append(result)
    total_pages = first.get("totalPages")
    if not isinstance(total_pages, int):
        total_pages = -(-total // PAGE_SIZE)
    criteria = first.get("uniqueSearchCriteriaId")
    return SearchOutcome(keyword=keyword, location_name=location_name, radius_km=radius_km,
                         total=total, total_pages=total_pages,
                         criteria_id=criteria if isinstance(criteria, int) else None,
                         first_page=results, fetch_page=fetch_page, max_pages=max_pages,
                         skipped_records=skipped)


async def search_talents(page: Page, job_title: str, location: str, max_distance_km: int = 25,
                         keywords: list[str] | None = None, max_pages: int = MAX_PAGES) -> SearchOutcome:
    """Search Talent Finder for a job. Raises SearchError (with a code) on any
    failure; a genuine empty result is an outcome with total == 0, never an error."""
    await _ensure_talent_finder(page)
    location_name = await resolve_location(page, location)
    radius_km = backend_radius_km(max_distance_km)
    keyword = build_keyword(job_title, keywords)
    await human_delay(1500, 3500)  # the time a person takes to type the query
    outcome = await _run_search(page, keyword, location_name, radius_km, max_pages)
    if outcome.total == 0 and keywords:
        logger.warning(
            f"Keyworded search found 0 (keywords={keywords}); retrying without keywords as a safety fallback.")
        await human_delay(2500, 5000)
        outcome = await _run_search(page, build_keyword(job_title, None), location_name, radius_km, max_pages)
        outcome.keyword_fallback = True
    logger.info(
        f"Talent Finder: {outcome.total} candidates for {outcome.keyword!r} in {location_name!r} "
        f"(radius sent: {radius_km or 'Optimiert'} km; {outcome.total_pages} pages; "
        f"search id {outcome.criteria_id})")
    if outcome.skipped_records:
        logger.warning(f"{outcome.skipped_records} result records had no id and were skipped")
    return outcome

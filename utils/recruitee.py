"""Recruitee API client.

Three operations after a StepStone profile is unlocked:
  1. create_candidate  - POST /candidates  (offer_ids at ROOT level, CRITICAL)
  2. upload_cv         - PATCH /candidates/<id>/update_cv
  3. set_stage         - PATCH /placements/<id>

Each call retries up to MAX_RETRIES times with RETRY_DELAY_SECONDS backoff.
"""
import asyncio
import json
import logging
import httpx

logger = logging.getLogger(__name__)

RECRUITEE_API = "https://api.recruitee.com"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT = 30.0
CANDIDATES_PAGE_SIZE = 100  # Recruitee /candidates default; we paginate explicitly

# Per-scrape cache of all Recruitee candidates. Populated on first dedup check,
# reused for the rest of the run. Cleared via clear_candidates_cache() at the
# start of every scrape job (called from main.run_scrape).
_candidates_cache: list[dict] | None = None


def clear_candidates_cache() -> None:
    """Reset the Recruitee candidate cache between scrape jobs."""
    global _candidates_cache
    _candidates_cache = None


class RecruiteeError(Exception):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            if attempt == MAX_RETRIES:
                raise
            logger.warning(f"POST {url} attempt {attempt} failed: {e}. Retrying in {RETRY_DELAY_SECONDS}s")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise RecruiteeError("Unreachable")  # pragma: no cover


async def _patch_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.patch(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            if attempt == MAX_RETRIES:
                raise
            logger.warning(f"PATCH {url} attempt {attempt} failed: {e}. Retrying in {RETRY_DELAY_SECONDS}s")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise RecruiteeError("Unreachable")  # pragma: no cover


async def create_candidate(
    token: str,
    company_id: str,
    name: str,
    emails: list[str],
    phones: list[str],
    offer_id: int,
) -> tuple[int, int]:
    """Create a candidate in Recruitee and link to the offer.

    Returns (candidate_id, placement_id).
    Raises RecruiteeError on failure after retries.

    CRITICAL: offer_ids must be at ROOT level of the payload body,
    NOT nested inside the candidate object. Nesting is silently ignored
    by Recruitee (returns 201 but placements is empty).
    """
    url = f"{RECRUITEE_API}/c/{company_id}/candidates"
    body = {
        "candidate": {
            "name": name,
            "emails": emails,
            "phones": phones,
            "sources": ["StepStone Automation"],
        },
        "offer_ids": [offer_id],  # ROOT level — not inside candidate
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await _post_with_retry(client, url, json=body, headers=_headers(token))
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            raise RecruiteeError(f"create_candidate failed after {MAX_RETRIES} attempts: {e}") from e

    data = resp.json()
    candidate = data.get("candidate", {})
    candidate_id: int = candidate.get("id")
    placements: list = candidate.get("placements", [])

    if not candidate_id:
        raise RecruiteeError(f"create_candidate: missing candidate.id in response: {data}")
    if not placements:
        raise RecruiteeError(
            f"create_candidate: placements list is empty — offer_ids likely not linked. "
            f"candidate_id={candidate_id}"
        )

    placement_id: int = placements[0]["id"]
    logger.info(f"Recruitee candidate created: candidate_id={candidate_id} placement_id={placement_id}")
    return candidate_id, placement_id


async def upload_cv(
    token: str,
    company_id: str,
    candidate_id: int,
    cv_bytes: bytes,
    filename: str,
) -> bool:
    """Upload a CV PDF to an existing Recruitee candidate.

    Returns True on success, False on failure (non-fatal, logged).
    Uses PATCH /candidates/<id>/update_cv with multipart form data.
    """
    url = f"{RECRUITEE_API}/c/{company_id}/candidates/{candidate_id}/update_cv"
    files = {"candidate[cv]": (filename, cv_bytes, "application/pdf")}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            await _patch_with_retry(client, url, files=files, headers=_headers(token))
            logger.info(f"Recruitee CV uploaded: candidate_id={candidate_id} file={filename}")
            return True
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.error(f"Recruitee upload_cv failed for candidate_id={candidate_id}: {e}")
            return False


async def set_stage(
    token: str,
    company_id: str,
    placement_id: int,
    stage_id: int,
) -> bool:
    """Move a placement to the given pipeline stage (e.g. Gesourct).

    Returns True on success, False on failure (non-fatal, logged).
    """
    url = f"{RECRUITEE_API}/c/{company_id}/placements/{placement_id}/change_stage"
    body = {"stage_id": stage_id}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            await _patch_with_retry(client, url, json=body, headers=_headers(token))
            logger.info(f"Recruitee stage set: placement_id={placement_id} stage_id={stage_id}")
            return True
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.error(f"Recruitee set_stage failed for placement_id={placement_id}: {e}")
            return False


async def _fetch_all_candidates(token: str, company_id: str) -> list[dict]:
    """Fetch every candidate in the Recruitee account, paginated.

    Cached for the lifetime of a scrape run (see _candidates_cache + clear).
    First call paginates through all candidates; subsequent calls reuse the
    cache. The /candidates endpoint returns full candidate objects including
    emails[] and placements[] so we can filter locally.

    Why fetch-all-and-filter instead of a server-side email filter:
    - GET /candidates `query` param only searches name/offer (per docs), NOT email.
    - GET /search/new/candidates `filters_json` field name + operator for email
      is not publicly documented; testing in production is risky.
    - Local filter is unambiguous and works regardless of Recruitee's internal
      filter syntax. With ~hundreds of candidates per Aramaz account, the cost
      is acceptable (one paginated fetch per scrape run, then in-memory filter).
    """
    global _candidates_cache
    if _candidates_cache is not None:
        return _candidates_cache

    url = f"{RECRUITEE_API}/c/{company_id}/candidates"
    all_candidates: list[dict] = []
    offset = 0

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        while True:
            params = {"limit": CANDIDATES_PAGE_SIZE, "offset": offset}
            try:
                resp = await client.get(url, params=params, headers=_headers(token))
                resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
                logger.warning(
                    f"Recruitee /candidates page fetch failed at offset={offset}: {e}. "
                    f"Returning {len(all_candidates)} candidates collected so far."
                )
                # Fail open — don't break the scrape if dedup fetch fails partially
                break

            data = resp.json()
            batch = data.get("candidates", [])
            all_candidates.extend(batch)

            if len(batch) < CANDIDATES_PAGE_SIZE:
                # Last page reached
                break
            offset += CANDIDATES_PAGE_SIZE

    logger.info(
        f"Recruitee dedup cache: fetched {len(all_candidates)} candidates "
        f"across {(offset // CANDIDATES_PAGE_SIZE) + 1} page(s)"
    )
    _candidates_cache = all_candidates
    return all_candidates


async def check_candidate_exists_in_recruitee(
    token: str,
    company_id: str,
    email: str,
) -> tuple[bool, int | None, list[int]]:
    """Check if a candidate with this email exists ANYWHERE in Recruitee.

    Returns (True, candidate_id, [offer_ids the candidate is placed on]) on
    match, or (False, None, []) if not found / on dedup fetch failure.

    Match is case-insensitive across the candidate's `emails` array, so
    multiple emails per candidate are all checked.

    Catches both manually-added candidates AND candidates placed on different
    offers in the past — anyone the recruiter has already seen.
    """
    if not email:
        return False, None, []

    candidates = await _fetch_all_candidates(token, company_id)
    email_lower = email.lower()

    for candidate in candidates:
        candidate_emails = [
            e.lower() for e in (candidate.get("emails") or [])
            if isinstance(e, str)
        ]
        if email_lower not in candidate_emails:
            continue

        existing_id = candidate.get("id")
        placements = candidate.get("placements") or []
        offer_ids = [p.get("offer_id") for p in placements if p.get("offer_id")]
        logger.info(
            f"Recruitee dedup HIT: email={email} matches candidate_id={existing_id} "
            f"(placements on offers: {offer_ids})"
        )
        return True, existing_id, offer_ids

    logger.info(f"Recruitee dedup MISS: email={email} not found in {len(candidates)} candidates")
    return False, None, []

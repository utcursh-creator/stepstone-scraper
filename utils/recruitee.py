"""Recruitee API client.

Three operations after a StepStone profile is unlocked:
  1. create_candidate  - POST /candidates  (offer_ids at ROOT level, CRITICAL)
  2. upload_cv         - PATCH /candidates/<id>/update_cv
  3. set_stage         - PATCH /placements/<id>

Each call retries up to MAX_RETRIES times with RETRY_DELAY_SECONDS backoff.
"""
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

RECRUITEE_API = "https://api.recruitee.com"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT = 30.0


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
    url = f"{RECRUITEE_API}/c/{company_id}/placements/{placement_id}"
    body = {"stage_id": stage_id}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            await _patch_with_retry(client, url, json=body, headers=_headers(token))
            logger.info(f"Recruitee stage set: placement_id={placement_id} stage_id={stage_id}")
            return True
        except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.error(f"Recruitee set_stage failed for placement_id={placement_id}: {e}")
            return False

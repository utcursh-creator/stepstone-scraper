"""Recruitee API client.

Three operations after a StepStone profile is unlocked:
  1. create_candidate  - POST /candidates  (offer_ids at ROOT level, CRITICAL)
  2. upload_cv         - PATCH /candidates/<id>/update_cv
  3. set_stage         - PATCH /placements/<id>

Plus a global dedup check (check_candidate_exists_in_recruitee) that matches
existing Recruitee candidates by EITHER email OR normalized phone. Email-only
matching was insufficient because recruiters often enter a candidate manually
using the email from the CV, while our StepStone scraper extracts the email
the candidate registered on StepStone with — these can differ. Phone is the
stable identifier across both data sources. A third signal — exact normalized
name PLUS a matching phone digit-suffix — catches duplicates whose phone
formatting defeats _normalize_phone.

Deliberately NOT a signal: name plus a "similar" email local-part. German
addresses overwhelmingly follow vorname.nachname@provider, so a normalized
local-part is a restatement of the name and carries no independent
information — pairing the two is name-only matching in disguise, and would
merge two unrelated people who share a common name. This gate runs AFTER the
unlock, so a false merge silently discards a real candidate we already paid
for. Always bias toward the split: a false split costs one duplicate row a
recruiter deletes in seconds; a false merge costs a credit and the candidate.

Each call retries up to MAX_RETRIES times with RETRY_DELAY_SECONDS backoff.
"""
import asyncio
import json
import logging
import re
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
    sources: list[str] | None = None,
) -> tuple[int, int]:
    """Create a candidate in Recruitee and link to the offer.

    Returns (candidate_id, placement_id).
    Raises RecruiteeError on failure after retries.

    CRITICAL: offer_ids must be at ROOT level of the payload body,
    NOT nested inside the candidate object. Nesting is silently ignored
    by Recruitee (returns 201 but placements is empty).

    `sources` defaults to ["StepStone Automation"]. Talent-pool pushes
    override this with a richer label so the recruiter can see why a
    candidate landed in the pool and which offer triggered it.
    """
    url = f"{RECRUITEE_API}/c/{company_id}/candidates"
    body = {
        "candidate": {
            "name": name,
            "emails": emails,
            "phones": phones,
            "sources": sources or ["StepStone Automation"],
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


# CV files arrive in several formats (PDF, Word, image). The MIME sent on upload
# must match the real bytes or Recruitee stores an unopenable file — see the
# scraper's _sniff_cv_type, which puts the correct extension on `filename`.
_EXT_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _mime_for_filename(filename: str) -> str:
    """Map a filename's extension to its MIME type (octet-stream if unknown)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_MIME.get(ext, "application/octet-stream")


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
    files = {"candidate[cv]": (filename, cv_bytes, _mime_for_filename(filename))}
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


_PHONE_STRIP_RE = re.compile(r"[\s\-\(\)\.]+")
_NAME_PUNCT_RE = re.compile(r"[.\-]+")
_WHITESPACE_RE = re.compile(r"\s+")
_NON_DIGIT_RE = re.compile(r"\D+")

# Minimum digit-suffix overlap for the soft phone signal (name matching).
PHONE_SUFFIX_MIN_DIGITS = 7

_UMLAUT_FOLDS = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))


def _normalize_name(name: str | None) -> str:
    """Normalize a person's full name for exact comparison.

    Lowercase, strip, collapse internal whitespace, remove dots/hyphens,
    fold German umlauts (ä→ae, ö→oe, ü→ue, ß→ss) so 'Müller' == 'Mueller'.

    Examples:
      "Max Mustermann"     → "max mustermann"
      "  Max  MUSTERMANN " → "max mustermann"
      "Hans-Peter Müller"  → "hanspeter mueller"
      None / ""            → ""
    """
    if not name:
        return ""
    n = name.lower().strip()
    for src, dst in _UMLAUT_FOLDS:
        n = n.replace(src, dst)
    n = _NAME_PUNCT_RE.sub("", n)
    n = _WHITESPACE_RE.sub(" ", n).strip()
    return n


def _phone_suffix_match(a: str | None, b: str | None) -> bool:
    """True if the last PHONE_SUFFIX_MIN_DIGITS digits of both phones match.

    Compares digit-only forms, so formatting and country-prefix differences
    that defeat _normalize_phone (e.g. '+49 (0) 171 ...' vs '0171 ...')
    still corroborate. Both sides need at least the minimum digit count.
    """
    digits_a = _NON_DIGIT_RE.sub("", a or "")
    digits_b = _NON_DIGIT_RE.sub("", b or "")
    if len(digits_a) < PHONE_SUFFIX_MIN_DIGITS or len(digits_b) < PHONE_SUFFIX_MIN_DIGITS:
        return False
    return digits_a[-PHONE_SUFFIX_MIN_DIGITS:] == digits_b[-PHONE_SUFFIX_MIN_DIGITS:]


def _normalize_phone(phone: str | None) -> str:
    """Normalize a phone number for case-insensitive comparison.

    Rules (tuned for German contract recruiting):
      - Strip whitespace, dashes, parentheses, dots
      - Convert German country-code prefixes to leading 0:
          +49 / 0049 / 49 (when followed by enough digits) → 0
      - Keep non-German country codes as-is with their leading +
      - Drop anything that doesn't reduce to at least one digit (returns "")

    Examples:
      "+49 171 6109508"  → "01716109508"
      "0049-171-6109508" → "01716109508"
      "0171 6109508"     → "01716109508"
      "(0171) 610-9508"  → "01716109508"
      "+1 555 1234"      → "+15551234"
      ""                 → ""
      None               → ""
      "abc"              → ""
    """
    if not phone:
        return ""
    # Strip separators
    stripped = _PHONE_STRIP_RE.sub("", phone).strip()
    if not stripped:
        return ""
    # Must contain at least one digit
    if not any(c.isdigit() for c in stripped):
        return ""
    # German country-code normalization → leading 0
    if stripped.startswith("+49"):
        return "0" + stripped[3:]
    if stripped.startswith("0049"):
        return "0" + stripped[4:]
    # Heuristic: starts with "49" + 9-11 more digits = bare DE number without +
    if stripped.startswith("49") and len(stripped) >= 11 and stripped[2:].isdigit():
        return "0" + stripped[2:]
    return stripped


def _dedup_hit(candidate: dict, match_reason: str) -> tuple[bool, int | None, list[int]]:
    """Log a dedup match and build the (True, candidate_id, offer_ids) result."""
    existing_id = candidate.get("id")
    placements = candidate.get("placements") or []
    offer_ids = [p.get("offer_id") for p in placements if p.get("offer_id")]
    logger.info(
        f"Recruitee dedup HIT ({match_reason}): candidate_id={existing_id} "
        f"(placements on offers: {offer_ids})"
    )
    return True, existing_id, offer_ids


async def check_candidate_exists_in_recruitee(
    token: str,
    company_id: str,
    email: str | None = None,
    phone: str | None = None,
    name: str = "",
) -> tuple[bool, int | None, list[int]]:
    """Check if a candidate matching this email OR phone (or name + matching
    phone suffix) exists in Recruitee.

    Returns (True, candidate_id, [offer_ids the candidate is placed on]) on
    match, or (False, None, []) if not found / on dedup fetch failure.

    Matching rules (any one signature is sufficient):
      - Email: case-insensitive, whitespace-trimmed, across the candidate's
        `emails` array.
      - Phone: normalized via _normalize_phone (strips separators, unifies
        German country-code variants to a single "0..." form), across the
        candidate's `phones` array.
      - Name + phone digit-suffix (only checked when both exact signals miss,
        and only when `name` is passed): normalized full name matches exactly
        AND the last PHONE_SUFFIX_MIN_DIGITS digits of a phone match. A name
        match with no phone corroboration is never a duplicate — two
        different people who share a common name must not be merged.

    Why email+phone: a recruiter might add a candidate manually with the
    email from their CV, but our StepStone scraper extracts the email the
    candidate registered with on StepStone (which can differ). Phone is the
    stable identifier — it rarely changes across a candidate's data-source
    representations.

    Why the phone suffix and nothing softer: this gate runs AFTER the unlock,
    so a false merge is unrecoverable — the credit is spent, the candidate is
    never pushed, and the pre-unlock Airtable dedup skips them on every future
    run. Seven trailing digits are independent evidence. A "similar" email
    local-part is not: German addresses follow vorname.nachname@provider, so
    the local-part restates the name and adds nothing. Requiring both would be
    name-only matching, which merges unrelated people who share a name.
    Known accepted gap: a genuine duplicate whose email AND phone both differ
    across sources stays a duplicate — visible in Recruitee, and the recruiter
    deletes the row in seconds. That is the cheaper error.

    Catches both manually-added candidates AND candidates placed on different
    offers in the past — anyone the recruiter has already seen.
    """
    if not email and not phone:
        return False, None, []

    candidates = await _fetch_all_candidates(token, company_id)
    email_lower = email.lower().strip() if email else None
    phone_norm = _normalize_phone(phone) if phone else ""

    # Pass 1: exact signals (email / normalized phone)
    for candidate in candidates:
        # Email match
        if email_lower:
            candidate_emails = [
                e.lower().strip() for e in (candidate.get("emails") or [])
                if isinstance(e, str)
            ]
            if email_lower in candidate_emails:
                return _dedup_hit(candidate, f"email={email_lower!r}")

        # Phone match
        if phone_norm:
            candidate_phones_norm = [
                _normalize_phone(p) for p in (candidate.get("phones") or [])
                if isinstance(p, str)
            ]
            candidate_phones_norm = [p for p in candidate_phones_norm if p]
            if phone_norm in candidate_phones_norm:
                return _dedup_hit(candidate, f"phone={phone_norm!r}")

    # Pass 2: exact name + phone digit-suffix. Only reached when both exact
    # signals missed on every candidate. The phone suffix must corroborate —
    # a name match alone is never enough (see the docstring on why nothing
    # softer, e.g. email local-part similarity, may be substituted here).
    name_norm = _normalize_name(name)
    if name_norm:
        for candidate in candidates:
            if _normalize_name(candidate.get("name")) != name_norm:
                continue

            if phone:
                for cand_phone in (candidate.get("phones") or []):
                    if isinstance(cand_phone, str) and _phone_suffix_match(phone, cand_phone):
                        return _dedup_hit(
                            candidate,
                            f"name+phone-suffix (name={name_norm!r}, "
                            f"phone={phone!r} ~ {cand_phone!r})",
                        )

    logger.info(
        f"Recruitee dedup MISS: email={email!r} phone={phone!r} name={name!r} "
        f"(phone_norm={phone_norm!r}) not found in {len(candidates)} candidates"
    )
    return False, None, []

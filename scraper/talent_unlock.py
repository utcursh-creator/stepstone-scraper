"""Unlock a Talent Finder candidate: the step that SPENDS a StepStone credit.

Replaces the DirectSearch dialog scraping in scraper/profile.py. The contract
was read from Stepstone Recruit's own public JavaScript (2026-09-25), so no
credit was spent to learn it:

  * the Freischalten button opens NO confirmation dialog. It sends
      POST /recruiter/talent-sourcing/api/v1/talent/unlock
      {"talentId": ..., "uniqueSearchCriteriaId": ...}
    and the scraper sends exactly that, from the page world, like the search.
  * the response carries the contact details (personalInfo) and
    `alreadyUnlocked`. An already-unlocked candidate is returned again for free.
  * out of credits: 403 with a `creditState` in the body.
  * the CV is GET /talents/{id}/cv/view?uniqueSearchCriteriaId=... (a PDF).

Credit accounting follows StepStone, not us. `credit_spent` is what the unlock
response says (alreadyUnlocked or not); StepStone's own balance is read before
and after as a cross-check; and when the outcome is unclear (a timeout, a 5xx)
the candidate's unlock history decides. The lesson of scraper/profile.py stands:
"StepStone charged us" and "we got the data" are different events.
"""
import base64
import logging
from datetime import datetime, timedelta, timezone

from patchright.async_api import Page

from models.candidate import CandidateResult
from scraper.auth import CREDITS_PATH
from scraper.talent_search import SearchResult
from utils.cv_file import sniff_cv_type

logger = logging.getLogger(__name__)

UNLOCK_PATH = "/recruiter/talent-sourcing/api/v1/talent/unlock"
UNLOCK_HISTORY_PATH = "/recruiter/talent-sourcing/api/v1/actions/talents/{id}/unlocks/latest"
CV_PATH = "/recruiter/talent-sourcing/api/v1/talents/{id}/cv/view"
UNLOCK_TIMEOUT_MS = 30_000
CV_TIMEOUT_MS = 60_000
CV_MAX_BYTES = 15 * 1024 * 1024
RECENT_UNLOCK = timedelta(minutes=15)

_JSON_JS = """
async ({ url, method, body, timeoutMs }) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
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

_FILE_JS = """
async ({ url, timeoutMs, maxBytes }) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { credentials: 'include', signal: ctl.signal });
    const contentType = r.headers.get('content-type') || '';
    if (!r.ok) return { status: r.status, contentType };
    const bytes = new Uint8Array(await r.arrayBuffer());
    if (bytes.length > maxBytes) return { status: r.status, contentType, tooLarge: bytes.length };
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return { status: r.status, contentType, size: bytes.length, base64: btoa(bin) };
  } catch (e) {
    return { status: 0, error: String(e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


class UnlockError(Exception):
    """A reason to STOP the job (no credits, session gone, unlock refused).
    `code` routes it. `credit_spent`: StepStone may have charged for the failed
    attempt (unverifiable counts as spent), so the caller still records it."""

    def __init__(self, message: str, code: str, credit_spent: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.credit_spent = credit_spent


async def _json(page: Page, url: str, method: str = "GET", body=None,
                timeout_ms: int = UNLOCK_TIMEOUT_MS) -> dict:
    try:
        res = await page.evaluate(_JSON_JS, {"url": url, "method": method, "body": body,
                                             "timeoutMs": timeout_ms}, isolated_context=False)
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {e}"}
    return res if isinstance(res, dict) else {"status": 0, "error": "no result"}


async def _balance(page: Page) -> int | None:
    res = await _json(page, CREDITS_PATH)
    body = res.get("body") if res.get("status") == 200 else None
    if isinstance(body, dict):
        if body.get("unlimited") is True:
            return None
        remaining = body.get("remainingCredits")
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            return remaining
    return None


async def _unlocked_by_us_recently(page: Page, talent_id: str) -> bool | None:
    """True/False from StepStone's unlock history; None when it can't be read."""
    res = await _json(page, UNLOCK_HISTORY_PATH.format(id=talent_id))
    body = res.get("body") if res.get("status") == 200 else None
    if not isinstance(body, dict) or not isinstance(body.get("actions"), list):
        return None
    now = datetime.now(timezone.utc)
    for action in body["actions"]:
        if not isinstance(action, dict) or action.get("actionType") != "UNLOCKED":
            continue
        try:
            when = datetime.fromisoformat(str(action.get("actionDate")).replace("Z", "+00:00"))
        except ValueError:
            when = None
        if action.get("isCurrentUser") and when and now - when <= RECENT_UNLOCK:
            return True
    return False


async def _download_cv(page: Page, talent_id: str, criteria_id: int | None) -> tuple[str, str] | None:
    """(base64, filename) of the candidate's CV, or None. Never raises: by now
    the credit is spent, and a missing CV must not also lose the contact."""
    url = CV_PATH.format(id=talent_id) + (f"?uniqueSearchCriteriaId={criteria_id}" if criteria_id else "")
    try:
        res = await page.evaluate(_FILE_JS, {"url": url, "timeoutMs": CV_TIMEOUT_MS, "maxBytes": CV_MAX_BYTES},
                                  isolated_context=False)
    except Exception as e:
        logger.warning(f"CV download for {talent_id} failed: {type(e).__name__}: {e}")
        return None
    if not isinstance(res, dict) or not res.get("base64"):
        detail = (res or {}).get("error") or (res or {}).get("tooLarge") or (res or {}).get("status")
        logger.warning(f"CV download for {talent_id} returned no file ({detail})")
        return None
    raw = base64.b64decode(res["base64"])
    sniffed = sniff_cv_type(raw)
    if sniffed is None:
        logger.warning(f"CV for {talent_id}: {len(raw)} bytes that are not a recognised document; treated as no CV")
        return None
    ext, _mime = sniffed
    return res["base64"], f"Lebenslauf.{ext}"


def _contact(personal: dict) -> tuple[str, str, str, str]:
    """(name, email, phone, 'PLZ City') from the unlock response."""
    first = str(personal.get("firstName") or "").strip()
    last = str(personal.get("lastName") or "").strip()
    email = str(personal.get("email") or "").strip()
    phone = str(personal.get("mobilePhoneNumber") or personal.get("phoneNumber") or "").strip()
    address = personal.get("address") or {}
    home = " ".join(p for p in (str(address.get("postalCode") or "").strip(),
                                str(address.get("city") or "").strip()) if p)
    return " ".join(p for p in (first, last) if p), email, phone, home


async def unlock_talent(page: Page, candidate: SearchResult, account_label: str,
                        criteria_id: int | None) -> tuple[CandidateResult | None, bool]:
    """Unlock one candidate. Returns (result or None, credit_spent), the same
    contract as the old extract_profile, so main.py's accounting is unchanged.
    Raises UnlockError when the job must stop (no credits, session gone)."""
    talent_id = candidate.profile_id
    before = await _balance(page)
    if before == 0 and candidate.is_locked:
        raise UnlockError("StepStone reports 0 credits left; nothing more can be unlocked", code="UNLOCK_NO_CREDITS")

    body = {"talentId": talent_id}
    if criteria_id:
        body["uniqueSearchCriteriaId"] = criteria_id
    res = await _json(page, UNLOCK_PATH, method="POST", body=body)
    status, data = res.get("status"), res.get("body")

    if status == 403 and isinstance(data, dict) and data.get("creditState"):
        raise UnlockError(f"StepStone refused the unlock: creditState={data['creditState']}",
                          code="UNLOCK_NO_CREDITS")
    if status == 401:
        raise UnlockError("StepStone answered 401 to the unlock; the session is no longer valid",
                          code="UNLOCK_SESSION_LOST")
    if isinstance(status, int) and 400 <= status < 500:
        # StepStone refused the request itself (wrong shape, gone, forbidden).
        # It will refuse every further unlock the same way: stop the job instead
        # of sending one refused unlock per matching candidate. The unlock
        # history says whether it charged anyway; unverifiable counts as spent.
        charged = await _unlocked_by_us_recently(page, talent_id)
        raise UnlockError(
            f"StepStone refused the unlock with HTTP {status} ({str(data)[:160] if data else 'no body'}); "
            f"charged={charged}. Every further unlock would be refused the same way.",
            code="UNLOCK_REJECTED", credit_spent=charged is not False,
        )

    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("personalInfo"), dict):
        # The outcome is unclear (timeout, 5xx, odd body). Ask StepStone whether it
        # charged us. If it did, the unlock can be re-read for free (alreadyUnlocked).
        charged = await _unlocked_by_us_recently(page, talent_id)
        logger.warning(f"Unlock of {talent_id} unclear (status {status}, {res.get('error') or ''}); "
                       f"StepStone's unlock history says charged={charged}")
        if charged is False:
            return None, False
        if charged is None:
            # Unverifiable. Count it as spent: a daily counter that runs BELOW
            # real spend lets the cap authorise extra unlocks on top of credits
            # already burned (the lesson of scraper/profile.py). Over-counting
            # only stops the day early.
            logger.error(f"Unlock of {talent_id}: charge could not be verified; counting it as spent")
            return None, True
        again = await _json(page, UNLOCK_PATH, method="POST", body=body)
        data = again.get("body") if again.get("status") == 200 else None
        if not isinstance(data, dict) or not isinstance(data.get("personalInfo"), dict):
            logger.error(f"Unlock of {talent_id} charged but the details could not be read back")
            return None, True
        data = {**data, "alreadyUnlocked": False}   # the charge was ours, moments ago

    credit_spent = data.get("alreadyUnlocked") is not True
    after = await _balance(page)
    if before is not None and after is not None:
        delta = before - after
        if delta != (1 if credit_spent else 0):
            logger.warning(f"Credit cross-check for {talent_id}: balance {before} -> {after} "
                           f"(expected a change of {1 if credit_spent else 0}); trusting the unlock response")

    try:
        name, email, phone, home = _contact(data["personalInfo"])
    except Exception as e:
        # The credit is spent by now: report it, never crash the job over it.
        logger.error(f"Unlock of {talent_id}: contact details unreadable ({type(e).__name__}); counting it as spent")
        return None, credit_spent
    home = home or candidate.wohnort
    cv = await _download_cv(page, talent_id, criteria_id) if candidate.has_cv_attachment else None
    profile_text = (f"Wohnadresse {home}\n" if home else "") + candidate.preview_text
    logger.info(f"Unlocked {talent_id}: credit_spent={credit_spent}, email={'yes' if email else 'no'}, "
                f"phone={'yes' if phone else 'no'}, cv={'yes' if cv else 'no'}, balance {before} -> {after}")
    return CandidateResult(
        name=name or candidate.display_name,
        stepstone_profile_id=talent_id,
        email=email,
        phone=phone,
        profile_text=profile_text,
        cv_base64=cv[0] if cv else None,
        cv_filename=cv[1] if cv else "",
        account_used=account_label,
        unlocked=True,
        unlock_reason="success" if credit_spent else "already_unlocked",
    ), credit_spent

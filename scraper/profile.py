"""StepStone profile extraction from DirectSearch modal dialog.

Based on live probing 2026-04-17:
- Clicking a.miniprofile__name opens div.ngdialog:last-of-type AND unlocks the profile
- Dialog inner text contains labeled fields: Email, Mobil, Wohnadresse, StepStone ID, CV
- Each profile unlock consumes one credit from the recruiter account
"""
import asyncio
import base64
import logging
import re
import time
from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from models.candidate import CandidateResult
from utils.delays import human_delay

logger = logging.getLogger(__name__)

# The unlock modal. Angular inserts this element and only THEN fills it from the
# unlock response, so its presence and its content are two separate waits.
DIALOG_SELECTOR = "div.ngdialog:last-of-type"

# How long to wait for the dialog to appear AFTER the click that spends the
# credit. Deliberately generous: by the time we are waiting here the credit is
# already gone, so giving up early converts a slow render into a paid-for
# nothing. The old code waited a blind human_delay(3000, 4500) and then probed
# once with a non-waiting query_selector — a TIGHTER budget than the 3.0-5.5s
# nap that demonstrably lost the race in DirectSearch on 2026-07-31.
DIALOG_TIMEOUT_MS = 30_000

# How long to wait for the unlock RESPONSE to populate the dialog. The shell can
# be visible with empty fields; reading then makes every regex miss and the
# candidate is emitted with unlocked=True and no email/phone — a paid-for
# contact record with no contact details, which also bypasses the Recruitee
# dedup in main.py (it is gated on `profile.email or profile.phone`).
DIALOG_CONTENT_TIMEOUT_MS = 15_000

# How often to re-read the dialog while waiting for it to populate.
DIALOG_POLL_INTERVAL_S = 0.5


# Regex patterns for extracting structured fields from dialog text
RE_EMAIL = re.compile(r"Email\s+([\w.+-]+@[\w-]+\.[\w.]+)", re.IGNORECASE)
RE_MOBIL = re.compile(r"Mobil\s+(\+?\d[\d\s\-()]+)")
RE_PHONE_HOME = re.compile(r"Telefon\s+(\+?\d[\d\s\-()]+)")
RE_ADDRESS = re.compile(r"Wohnadresse\s+(\d{5}\s+[^\n]+?)(?:\s+Deutschland)?", re.IGNORECASE)
RE_STEPSTONE_ID = re.compile(r"StepStone ID\s+(\d+)", re.IGNORECASE)
RE_NAME_HEADER = re.compile(r"^\s*([^\n]+?)\s*\n", re.MULTILINE)


async def _click_candidate(page: Page, profile_id: str) -> tuple[bool, bool]:
    """Click the miniprofile name link to unlock + open the dialog.

    Returns `(dialog_open, credit_spent)`.

    The two flags are separate because the caller must be able to tell "we never
    clicked, so this cost nothing" from "we clicked, the credit is gone, and the
    dialog never rendered". Collapsing them into one bool (the old behaviour) is
    what let a spent credit go unrecorded: main.py only called record_unlock
    inside `if profile:`, so a dialog that failed to render left the daily
    counter BELOW real spend — the cap then permitted extra unlocks on top.
    """
    # Find the card whose profile link contains this profile ID
    link = await page.query_selector(f"a.miniprofile__name[href*='profileID={profile_id}']")
    if not link:
        # Fallback: any link with this profile ID
        link = await page.query_selector(f"a[href*='profileID={profile_id}']")
    if not link:
        return False, False
    try:
        await link.click(force=True, timeout=10000)
    except Exception as e:
        # The click never landed, so StepStone never charged us.
        logger.warning(f"  Unlock click failed for {profile_id} (no credit spent): {e}")
        return False, False

    # PAST THIS POINT THE CREDIT IS SPENT. Wait for the dialog on a condition,
    # never on a nap.
    try:
        await page.wait_for_selector(
            DIALOG_SELECTOR, state="visible", timeout=DIALOG_TIMEOUT_MS
        )
        return True, True
    except PlaywrightTimeoutError:
        logger.error(
            f"  UNLOCK DIALOG TIMEOUT for {profile_id}: the click spent a credit but "
            f"{DIALOG_SELECTOR} never became visible within {DIALOG_TIMEOUT_MS}ms. "
            f"The credit is gone and the profile data was not obtained."
        )
        return False, True


async def _wait_for_dialog_content(dialog, timeout_ms: int) -> str:
    """Poll the dialog's text until the unlock response has populated it.

    `StepStone ID <digits>` is the readiness sentinel: it is present on every
    unlocked profile, unlike email/phone which some candidates genuinely lack,
    so it cannot be confused with a real candidate having no contact details.

    Returns whatever text we ended up with — the caller decides whether it is
    usable, so a wrong sentinel can never turn every unlock into a total loss.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    text = ""
    while True:
        try:
            text = await dialog.inner_text()
        except Exception:
            text = ""
        if text and RE_STEPSTONE_ID.search(text):
            return text
        if time.monotonic() >= deadline:
            return text
        await asyncio.sleep(DIALOG_POLL_INTERVAL_S)


async def _extract_name(dialog_text: str) -> str:
    """Name appears on the first non-empty line of the dialog."""
    lines = [l.strip() for l in dialog_text.split("\n") if l.strip()]
    return lines[0] if lines else ""


async def _find_cv_link(dialog) -> tuple[str, str]:
    """Find the CV download URL and original filename inside the dialog."""
    # Dialog has an ANHÄNGE section with the CV link
    cv_link = await dialog.query_selector(
        "a[href*='profile.downloadAttachment'], a[href*='downloadAttachment']"
    )
    if not cv_link:
        return "", ""
    href = await cv_link.get_attribute("href") or ""
    if href.startswith("/"):
        href = f"https://www.stepstone.de{href}"
    link_text = (await cv_link.inner_text()).strip()
    filename = link_text if link_text.lower().endswith(".pdf") else f"{link_text}.pdf" if link_text else "CV.pdf"
    return href, filename


def _sniff_cv_type(buffer: bytes) -> tuple[str, str] | None:
    """Detect (extension, mime_type) from a file's magic bytes.

    Returns None when the bytes are not a usable document — empty, too small, or
    an HTML error page (StepStone occasionally answers a download with an HTTP-200
    interstitial). Sniffing exists because candidates upload CVs as PDF *or* Word
    *or* image; storing a Word/image CV under a .pdf name + application/pdf MIME is
    exactly why Recruitee could not open some scraped CVs.
    """
    if not buffer or len(buffer) < 64:
        return None
    head = buffer[:16]
    if head[:4] == b"%PDF":
        return ("pdf", "application/pdf")
    if head[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if head[:5] == b"{\\rtf":
        return ("rtf", "application/rtf")
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 → legacy MS Office .doc
        return ("doc", "application/msword")
    if head[:4] == b"PK\x03\x04":  # zip container → OOXML (.docx) or ODF (.odt)
        if b"opendocument.text" in buffer[:1024]:
            return ("odt", "application/vnd.oasis.opendocument.text")
        if b"word/" in buffer:
            return ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if b"xl/" in buffer:
            return ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if b"ppt/" in buffer:
            return ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        # Unknown zip — for a CV the overwhelmingly likely case is a Word doc.
        return ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    # Anything else (HTML interstitial, login page, garbage) is not a CV.
    return None


async def _download_cv_bytes(page: Page, cv_url: str) -> tuple[str, str] | None:
    """Download the CV via the authenticated browser session.

    Returns (base64_str, file_extension) where the extension is sniffed from the
    real bytes, or None if the download failed or the bytes are not a usable
    document. The caller uses the sniffed extension so the file is stored in
    Recruitee with a correct name + MIME and stays openable.
    """
    if not cv_url:
        return None
    try:
        response = await page.request.get(cv_url)
        if not response.ok:
            return None
        buffer = await response.body()
    except Exception:
        return None
    sniffed = _sniff_cv_type(buffer)
    if sniffed is None:
        logger.warning(
            "CV download returned %d bytes that are not a recognised document "
            "(first bytes: %r); treating as no CV.",
            len(buffer),
            buffer[:8],
        )
        return None
    ext, _mime = sniffed
    return base64.b64encode(buffer).decode("utf-8"), ext


async def _close_dialog(page: Page) -> None:
    """Close the profile dialog to return to results."""
    for sel in [
        "button.ngdialog-close",
        "div.ngdialog:last-of-type button[aria-label*='chlie']",
        ".ngdialog-content button:has-text('×')",
        "button:has-text('×')",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click(force=True, timeout=5000)
                await human_delay(500, 1000)
                return
        except Exception:
            continue
    # Fallback: Escape key
    try:
        await page.keyboard.press("Escape")
        await human_delay(500, 1000)
    except Exception:
        pass


async def extract_profile(
    page: Page,
    profile_id: str,
    account_used: str,
    preview_cv_url: str = "",
) -> CandidateResult | None:
    """Click into candidate, extract data from modal dialog, download CV.

    Args:
        preview_cv_url: CV URL from the search card (if available, avoids re-finding in dialog)

    Returns `(result, credit_spent)`. `result` is None when the profile could
    not be extracted; `credit_spent` is True whenever the unlock click landed,
    INDEPENDENT of whether extraction succeeded, so the caller can charge the
    daily budget for every credit StepStone actually took.
    """
    dialog_open, credit_spent = await _click_candidate(page, profile_id)
    if not dialog_open:
        return None, credit_spent

    dialog = await page.query_selector(DIALOG_SELECTOR)
    if not dialog:
        return None, credit_spent

    try:
        # The shell can be visible with empty fields — wait for the unlock
        # response to land before reading, or every regex below silently misses
        # and we emit a paid-for candidate with no contact details.
        dialog_text = await _wait_for_dialog_content(dialog, DIALOG_CONTENT_TIMEOUT_MS)
        if not RE_STEPSTONE_ID.search(dialog_text):
            # Sentinel never appeared. Accept the text anyway IF it clearly holds
            # real data — a wrong sentinel must never turn every unlock into a
            # total loss — otherwise treat it as an extraction failure.
            if not (RE_EMAIL.search(dialog_text) or RE_MOBIL.search(dialog_text)
                    or RE_PHONE_HOME.search(dialog_text)):
                logger.error(
                    f"  UNLOCK CONTENT TIMEOUT for {profile_id}: dialog rendered but "
                    f"never populated within {DIALOG_CONTENT_TIMEOUT_MS}ms "
                    f"({len(dialog_text)} chars). Credit spent, no data extracted."
                )
                return None, credit_spent
            logger.warning(
                f"  {profile_id}: 'StepStone ID' sentinel absent but contact fields "
                f"are present — proceeding on the contact fields."
            )

        # Name from first line of dialog text
        name = await _extract_name(dialog_text)

        # Regex-extract fields
        email_match = RE_EMAIL.search(dialog_text)
        email = email_match.group(1).strip() if email_match else ""

        mobil_match = RE_MOBIL.search(dialog_text)
        phone_mobil = mobil_match.group(1).strip() if mobil_match else ""

        phone_home_match = RE_PHONE_HOME.search(dialog_text)
        phone_home = phone_home_match.group(1).strip() if phone_home_match else ""

        phone = phone_mobil or phone_home

        # CV: prefer the dialog's link (authoritative), fall back to preview card's
        cv_url, cv_original_filename = await _find_cv_link(dialog)
        if not cv_url and preview_cv_url:
            cv_url = preview_cv_url
            cv_original_filename = "CV.pdf"

        if cv_url and cv_url.startswith("/"):
            cv_url = f"https://www.stepstone.de{cv_url}"

        downloaded = await _download_cv_bytes(page, cv_url) if cv_url else None
        cv_base64 = None
        cv_ext = "pdf"
        if downloaded:
            cv_base64, cv_ext = downloaded

        # Build a safe filename using the candidate's name and the REAL file type
        # (sniffed above) so Recruitee stores it with the correct extension/MIME
        # and the CV stays openable — a Word/image CV named .pdf will not open.
        cv_filename = ""
        if cv_base64:
            if name:
                safe_name = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß]+", "_", name).strip("_")
                cv_filename = f"{safe_name}_CV.{cv_ext}"
            else:
                cv_filename = f"CV.{cv_ext}"

        return CandidateResult(
            name=name,
            stepstone_profile_id=profile_id,
            email=email,
            phone=phone,
            profile_text=dialog_text,
            unlocked=True,
            unlock_reason="success",
            cv_base64=cv_base64,
            cv_filename=cv_filename,
            account_used=account_used,
        ), credit_spent
    finally:
        await _close_dialog(page)

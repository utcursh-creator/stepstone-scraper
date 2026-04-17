"""StepStone profile extraction from DirectSearch modal dialog.

Based on live probing 2026-04-17:
- Clicking a.miniprofile__name opens div.ngdialog:last-of-type AND unlocks the profile
- Dialog inner text contains labeled fields: Email, Mobil, Wohnadresse, StepStone ID, CV
- Each profile unlock consumes one credit from the recruiter account
"""
import base64
import re
from patchright.async_api import Page
from models.candidate import CandidateResult
from utils.delays import human_delay


# Regex patterns for extracting structured fields from dialog text
RE_EMAIL = re.compile(r"Email\s+([\w.+-]+@[\w-]+\.[\w.]+)", re.IGNORECASE)
RE_MOBIL = re.compile(r"Mobil\s+(\+?\d[\d\s\-()]+)")
RE_PHONE_HOME = re.compile(r"Telefon\s+(\+?\d[\d\s\-()]+)")
RE_ADDRESS = re.compile(r"Wohnadresse\s+(\d{5}\s+[^\n]+?)(?:\s+Deutschland)?", re.IGNORECASE)
RE_STEPSTONE_ID = re.compile(r"StepStone ID\s+(\d+)", re.IGNORECASE)
RE_NAME_HEADER = re.compile(r"^\s*([^\n]+?)\s*\n", re.MULTILINE)


async def _click_candidate(page: Page, profile_id: str) -> bool:
    """Click the miniprofile name link to unlock + open dialog."""
    # Find the card whose profile link contains this profile ID
    link = await page.query_selector(f"a.miniprofile__name[href*='profileID={profile_id}']")
    if not link:
        # Fallback: any link with this profile ID
        link = await page.query_selector(f"a[href*='profileID={profile_id}']")
    if not link:
        return False
    try:
        await link.click(force=True, timeout=10000)
    except Exception:
        return False
    await human_delay(3000, 4500)
    # Check dialog opened
    dialog = await page.query_selector("div.ngdialog:last-of-type")
    return dialog is not None


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


async def _download_cv_bytes(page: Page, cv_url: str) -> str | None:
    """Download CV via the authenticated browser session, return base64."""
    if not cv_url:
        return None
    try:
        response = await page.request.get(cv_url)
        if response.ok:
            buffer = await response.body()
            return base64.b64encode(buffer).decode("utf-8")
    except Exception:
        pass
    return None


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

    Returns CandidateResult with unlocked=True if successful, None on click failure.
    """
    if not await _click_candidate(page, profile_id):
        return None

    dialog = await page.query_selector("div.ngdialog:last-of-type")
    if not dialog:
        return None

    try:
        dialog_text = await dialog.inner_text()

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

        cv_base64 = await _download_cv_bytes(page, cv_url) if cv_url else None

        # Build a safe filename using the candidate's name
        cv_filename = ""
        if cv_base64:
            if name:
                safe_name = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß]+", "_", name).strip("_")
                cv_filename = f"{safe_name}_CV.pdf"
            else:
                cv_filename = cv_original_filename or "CV.pdf"

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
        )
    finally:
        await _close_dialog(page)

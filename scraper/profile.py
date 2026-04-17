import base64
import re
from patchright.async_api import Page
from models.candidate import CandidateResult
from utils.delays import human_delay


async def _click_candidate(page: Page, profile_id: str) -> bool:
    link = await page.query_selector(f"a[href*='{profile_id}']")
    if not link:
        return False
    await link.click()
    await human_delay(2000, 3000)
    dialog = await page.query_selector("div.ngdialog, div[role='dialog'], .profile-dialog")
    return dialog is not None


async def _extract_name(dialog) -> str:
    for selector in [".profile__name", ".candidate-name", "h2", "h3"]:
        el = await dialog.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    return ""


async def _extract_email(dialog) -> str:
    el = await dialog.query_selector("[data-profile-email]")
    if el:
        return (await el.get_attribute("data-profile-email")) or ""
    mailto = await dialog.query_selector("a[href^='mailto:']")
    if mailto:
        href = await mailto.get_attribute("href") or ""
        return href.replace("mailto:", "").strip()
    text = await dialog.inner_text()
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return match.group(0) if match else ""


async def _extract_phone(dialog) -> str:
    tel = await dialog.query_selector("a[href^='tel:']")
    if tel:
        href = await tel.get_attribute("href") or ""
        return href.replace("tel:", "").strip()
    el = await dialog.query_selector("[onclick*='tel:']")
    if el:
        onclick = await el.get_attribute("onclick") or ""
        match = re.search(r"tel:([\d\s+\-()]+)", onclick)
        return match.group(1).strip() if match else ""
    return ""


async def _download_cv(page: Page, dialog) -> tuple[str | None, str]:
    cv_link = await dialog.query_selector(
        "a[href*='downloadAttachment'], a.miniprofile__actionlink[href*='download']"
    )
    if not cv_link:
        return None, ""
    href = await cv_link.get_attribute("href") or ""
    if not href:
        return None, ""
    if href.startswith("/"):
        href = f"https://www.stepstone.de{href}"
    try:
        response = await page.request.get(href)
        if response.ok:
            buffer = await response.body()
            b64 = base64.b64encode(buffer).decode("utf-8")
            link_text = await cv_link.inner_text()
            filename = link_text.strip() if link_text.strip() else "CV.pdf"
            return b64, filename
    except Exception:
        pass
    return None, ""


async def _close_dialog(page: Page) -> None:
    for selector in [
        "button.ngdialog-close",
        "button[aria-label='Close']",
        "button[aria-label='Schließen']",
        ".dialog-close",
        "button:has-text('×')",
    ]:
        btn = await page.query_selector(selector)
        if btn:
            await btn.click()
            await human_delay(500, 1000)
            return
    await page.keyboard.press("Escape")
    await human_delay(500, 1000)


async def extract_profile(
    page: Page,
    profile_id: str,
    account_used: str,
) -> CandidateResult | None:
    """Click into a candidate profile, extract data, download CV, close dialog.

    Returns CandidateResult with unlocked=True if successful, or None if dialog didn't open.
    """
    if not await _click_candidate(page, profile_id):
        return None

    dialog = await page.query_selector(
        "div.ngdialog:last-of-type, div[role='dialog']:last-of-type, .profile-dialog"
    )
    if not dialog:
        return None

    try:
        name = await _extract_name(dialog)
        email = await _extract_email(dialog)
        phone = await _extract_phone(dialog)
        profile_text = ""
        try:
            profile_text = await dialog.inner_text()
        except Exception:
            pass
        cv_base64, cv_filename = await _download_cv(page, dialog)
        if cv_base64 and name:
            safe_name = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß]", "_", name)
            cv_filename = f"{safe_name}_CV.pdf"

        return CandidateResult(
            name=name,
            stepstone_profile_id=profile_id,
            email=email,
            phone=phone,
            profile_text=profile_text,
            unlocked=True,
            unlock_reason="success",
            cv_base64=cv_base64,
            cv_filename=cv_filename or "",
            account_used=account_used,
        )
    finally:
        await _close_dialog(page)

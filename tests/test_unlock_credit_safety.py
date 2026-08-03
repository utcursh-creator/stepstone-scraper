"""Credit-safety guards for the unlock path — the only place real money moves.

Clicking a candidate's name BOTH unlocks the profile and opens the modal, so the
StepStone credit is spent the instant the click lands. Two defects lived in the
gap between that click and reading the modal:

  1. LOST CREDIT, LOST DATA. The code napped a blind human_delay(3000, 4500) and
     then probed once with a non-waiting query_selector. A slow modal returned
     None, main.py recorded 'profile_extraction_failed', and record_unlock —
     which sat inside `if profile:` — never ran. The credit was gone AND the
     daily counter stayed below real spend, so the cap authorised further
     unlocks on top of credits already burned. Note the 3.0-4.5s budget was
     TIGHTER than the 3.0-5.5s nap that demonstrably lost this same race in
     DirectSearch on 2026-07-31.

  2. PAID-FOR EMPTY RECORD. Angular inserts the modal shell before the unlock
     response populates it. Reading immediately made every regex miss, yet the
     function still returned unlocked=True / unlock_reason='success'. That
     candidate reached Recruitee with no email and no phone — and because
     main.py gates its Recruitee dedup on `profile.email or profile.phone`, it
     also skipped the duplicate check.

The invariant: `credit_spent` reflects what StepStone charged, never what we
managed to extract.
"""
import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from scraper import profile as profile_mod
from scraper.profile import DIALOG_SELECTOR, extract_profile

POPULATED = (
    "Maria Musterfrau\n"
    "Persönliche Angaben\n"
    "Email maria.musterfrau@example.de\n"
    "Mobil +49 151 12345678\n"
    "Wohnadresse 94469 Deggendorf\n"
    "StepStone ID 12345678\n"
)

# The modal shell before the unlock response lands: chrome, no candidate data.
EMPTY_SHELL = "Profile\nCV\nE-Mail\nTelefon\nNotiz\nSpeichern\n"


class _FakeLink:
    def __init__(self, raises=False):
        self._raises = raises

    async def click(self, *a, **k):
        if self._raises:
            raise RuntimeError("element is not attached to the DOM")


class _FakeDialog:
    """`texts` is consumed one call at a time, modelling Angular filling the
    shell in after the fact; the last value repeats forever."""

    def __init__(self, texts):
        self._texts = list(texts)

    async def inner_text(self):
        return self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]

    async def query_selector(self, *a, **k):
        return None  # no CV link


class _FakePage:
    def __init__(self, *, link=None, dialog_appears=True, dialog=None):
        self._link = link
        self._dialog_appears = dialog_appears
        self._dialog = dialog

    async def query_selector(self, selector, *a, **k):
        if "profileID" in selector:
            return self._link
        if selector == DIALOG_SELECTOR:
            return self._dialog
        return None

    async def wait_for_selector(self, selector, **k):
        if selector == DIALOG_SELECTOR and self._dialog_appears:
            return self._dialog
        raise PlaywrightTimeoutError("Timeout exceeded")

    async def evaluate(self, *a, **k):
        return None

    async def keyboard_press(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real sleeping, and never touch the network for a CV."""
    async def _no_delay(*a, **k):
        return None

    async def _no_close(*a, **k):
        return None

    async def _no_cv(*a, **k):
        return None

    monkeypatch.setattr(profile_mod, "human_delay", _no_delay)
    monkeypatch.setattr(profile_mod, "_close_dialog", _no_close)
    monkeypatch.setattr(profile_mod, "_download_cv_bytes", _no_cv)
    # Keep the content wait real (several polls) but fast.
    monkeypatch.setattr(profile_mod, "DIALOG_CONTENT_TIMEOUT_MS", 200)
    monkeypatch.setattr(profile_mod, "DIALOG_POLL_INTERVAL_S", 0.01)


@pytest.mark.asyncio
async def test_a_click_that_never_lands_costs_nothing():
    """No link on the page — StepStone was never asked to unlock anything."""
    page = _FakePage(link=None)

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert result is None
    assert credit_spent is False, "no click means no charge"


@pytest.mark.asyncio
async def test_a_failed_click_costs_nothing():
    """The click raised, so it never reached StepStone."""
    page = _FakePage(link=_FakeLink(raises=True))

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert result is None
    assert credit_spent is False


@pytest.mark.asyncio
async def test_a_dialog_that_never_opens_still_charged_us():
    """THE credit-loss guard. The click landed — the credit is gone — and the
    modal never rendered. The old code reported this as a plain failure and the
    daily counter never moved."""
    page = _FakePage(link=_FakeLink(), dialog_appears=False)

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert result is None, "no data was obtained"
    assert credit_spent is True, "the click spent a credit — it MUST be recorded"


@pytest.mark.asyncio
async def test_an_unpopulated_dialog_is_a_failure_not_a_success():
    """THE empty-record guard. The shell renders but the unlock response never
    lands. Emitting unlocked=True here puts a contact-less 'contact record' into
    Recruitee and bypasses the email/phone-gated dedup."""
    dialog = _FakeDialog([EMPTY_SHELL])
    page = _FakePage(link=_FakeLink(), dialog=dialog)

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert result is None, "an empty dialog must not be reported as success"
    assert credit_spent is True


@pytest.mark.asyncio
async def test_the_dialog_is_read_only_after_it_populates():
    """The shell arrives first and fills in later — the reader must wait for the
    content, not for the element."""
    dialog = _FakeDialog([EMPTY_SHELL, EMPTY_SHELL, POPULATED])
    page = _FakePage(link=_FakeLink(), dialog=dialog)

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert credit_spent is True
    assert result is not None, "a late-populating dialog must still be extracted"
    assert result.email == "maria.musterfrau@example.de"
    assert result.phone.startswith("+49")
    assert result.unlocked is True


@pytest.mark.asyncio
async def test_contact_fields_are_accepted_even_without_the_sentinel():
    """A wrong sentinel must never turn every paid unlock into a total loss."""
    no_sentinel = POPULATED.replace("StepStone ID 12345678", "")
    dialog = _FakeDialog([no_sentinel])
    page = _FakePage(link=_FakeLink(), dialog=dialog)

    result, credit_spent = await extract_profile(page, "99", "Account 1")

    assert credit_spent is True
    assert result is not None, "real contact data must be used even if the sentinel moves"
    assert result.email == "maria.musterfrau@example.de"


@pytest.mark.asyncio
async def test_the_happy_path_is_unchanged():
    dialog = _FakeDialog([POPULATED])
    page = _FakePage(link=_FakeLink(), dialog=dialog)

    result, credit_spent = await extract_profile(page, "12345678", "Account 2")

    assert credit_spent is True
    assert result.name == "Maria Musterfrau"
    assert result.stepstone_profile_id == "12345678"
    assert result.account_used == "Account 2"
    assert result.unlock_reason == "success"

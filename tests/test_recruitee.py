import pytest
import httpx
import respx
import utils.recruitee as _recruitee_mod
from utils.recruitee import (
    create_candidate,
    upload_cv,
    set_stage,
    check_candidate_exists_in_recruitee,
    clear_candidates_cache,
    _normalize_phone,
    RecruiteeError,
)

TOKEN = "bearer_test"
COMPANY = "61932"
BASE = f"https://api.recruitee.com/c/{COMPANY}"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Zero out retry backoff so tests don't sleep 2s per attempt."""
    monkeypatch.setattr(_recruitee_mod, "RETRY_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def reset_dedup_cache():
    """Clear the per-scrape candidates cache so each test starts fresh."""
    clear_candidates_cache()
    yield
    clear_candidates_cache()


# -- create_candidate --

@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_success():
    respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(
            201,
            json={
                "candidate": {
                    "id": 111,
                    "placements": [{"id": 222}],
                }
            },
        )
    )
    cand_id, placement_id = await create_candidate(
        token=TOKEN,
        company_id=COMPANY,
        name="Maria Muster",
        emails=["maria@example.com"],
        phones=["+49123456"],
        offer_id=2525450,
    )
    assert cand_id == 111
    assert placement_id == 222


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_request_body():
    """offer_ids must be at ROOT level, not nested inside candidate."""
    route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(
            201,
            json={"candidate": {"id": 1, "placements": [{"id": 2}]}},
        )
    )
    await create_candidate(
        token=TOKEN,
        company_id=COMPANY,
        name="Test",
        emails=[],
        phones=[],
        offer_id=9999,
    )
    body = route.calls[0].request.read()
    import json
    parsed = json.loads(body)
    # offer_ids must be at root, NOT inside candidate
    assert "offer_ids" in parsed
    assert parsed["offer_ids"] == [9999]
    assert "offer_ids" not in parsed.get("candidate", {})


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_http_error_raises():
    respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(422, json={"error": "Invalid"})
    )
    with pytest.raises(RecruiteeError, match="create_candidate"):
        await create_candidate(
            token=TOKEN, company_id=COMPANY, name="X",
            emails=[], phones=[], offer_id=1,
        )


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_empty_placements_raises():
    respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(
            201,
            json={"candidate": {"id": 1, "placements": []}},
        )
    )
    with pytest.raises(RecruiteeError, match="placement"):
        await create_candidate(
            token=TOKEN, company_id=COMPANY, name="X",
            emails=[], phones=[], offer_id=1,
        )


# -- upload_cv --

@respx.mock
@pytest.mark.asyncio
async def test_upload_cv_success():
    respx.patch(f"{BASE}/candidates/111/update_cv").mock(
        return_value=httpx.Response(200, json={"candidate": {"id": 111}})
    )
    ok = await upload_cv(
        token=TOKEN,
        company_id=COMPANY,
        candidate_id=111,
        cv_bytes=b"%PDF-fake",
        filename="cv.pdf",
    )
    assert ok is True


@respx.mock
@pytest.mark.asyncio
async def test_upload_cv_failure_returns_false():
    respx.patch(f"{BASE}/candidates/111/update_cv").mock(
        return_value=httpx.Response(500, json={"error": "oops"})
    )
    ok = await upload_cv(
        token=TOKEN, company_id=COMPANY,
        candidate_id=111, cv_bytes=b"data", filename="cv.pdf",
    )
    assert ok is False


# -- set_stage --

@respx.mock
@pytest.mark.asyncio
async def test_set_stage_success():
    respx.patch(f"{BASE}/placements/222/change_stage").mock(
        return_value=httpx.Response(200, json={"placement": {"id": 222}})
    )
    ok = await set_stage(
        token=TOKEN,
        company_id=COMPANY,
        placement_id=222,
        stage_id=13055288,
    )
    assert ok is True


@respx.mock
@pytest.mark.asyncio
async def test_set_stage_failure_returns_false():
    respx.patch(f"{BASE}/placements/222/change_stage").mock(
        return_value=httpx.Response(422, json={"error": "bad stage"})
    )
    ok = await set_stage(
        token=TOKEN, company_id=COMPANY,
        placement_id=222, stage_id=0,
    )
    assert ok is False


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_source_tag():
    """Candidate payload must include sources: ['StepStone Automation']."""
    route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(
            201,
            json={"candidate": {"id": 1, "placements": [{"id": 2}]}},
        )
    )
    await create_candidate(
        token=TOKEN,
        company_id=COMPANY,
        name="Test Candidate",
        emails=[],
        phones=[],
        offer_id=1,
    )
    import json as _json
    body = _json.loads(route.calls[0].request.read())
    assert body["candidate"]["sources"] == ["StepStone Automation"]


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_custom_sources():
    """Talent-pool path: caller can override sources with a richer label."""
    route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(
            201,
            json={"candidate": {"id": 1, "placements": [{"id": 2}]}},
        )
    )
    custom = ["StepStone Automation", "Talent Pool: Aus Radius (Offer 2468686)"]
    await create_candidate(
        token=TOKEN,
        company_id=COMPANY,
        name="Test",
        emails=[],
        phones=[],
        offer_id=2592624,
        sources=custom,
    )
    import json as _json
    body = _json.loads(route.calls[0].request.read())
    assert body["candidate"]["sources"] == custom


# -- phone normalization --

@pytest.mark.parametrize("raw, normalized", [
    ("+49 171 6109508", "01716109508"),
    ("0049-171-6109508", "01716109508"),
    ("49 171 6109508", "01716109508"),
    ("0171 6109508", "01716109508"),
    ("(0171) 610-9508", "01716109508"),
    ("0171.610.9508", "01716109508"),
    ("  +49  171  6109508  ", "01716109508"),
    ("+49-1604066423", "01604066423"),
    ("", ""),
    (None, ""),
    ("+1 555 1234", "+15551234"),  # non-DE country code preserved
    ("abc", ""),  # garbage → empty (no digits)
])
def test_normalize_phone(raw, normalized):
    assert _normalize_phone(raw) == normalized


# -- check_candidate_exists_in_recruitee (email OR phone) --

def _candidates_page(items: list[dict]) -> dict:
    """Wrap a list of candidate dicts in the /candidates response envelope."""
    return {"candidates": items}


@respx.mock
@pytest.mark.asyncio
async def test_dedup_matches_by_email_only():
    """Email-only input matches existing candidate by email (case-insensitive)."""
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {"id": 111, "emails": ["Foo@Bar.com"], "phones": [], "placements": [{"offer_id": 999}]},
        ])),
    )
    exists, cid, offers = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY, email="foo@bar.com",
    )
    assert exists is True
    assert cid == 111
    assert offers == [999]


@respx.mock
@pytest.mark.asyncio
async def test_dedup_matches_by_phone_when_email_differs():
    """If StepStone email != Recruitee email but phones match → still a duplicate.

    This is the fix for Behiye Aktürk / Mark Khudevich: their StepStone-side email
    didn't match the email Julia/Umair manually entered, but the phone is the same.
    """
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {
                "id": 222,
                "emails": ["aktuerk.b@hotmail.com"],
                "phones": ["+49 171 6109508"],
                "placements": [{"offer_id": 2189981}],
            },
        ])),
    )
    # StepStone gave us a DIFFERENT email, but the same phone (formatted differently)
    exists, cid, offers = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY,
        email="behiye.aktuerk@some-other-email.de",
        phone="0171 6109508",
    )
    assert exists is True, "phone match should have caught this duplicate"
    assert cid == 222
    assert 2189981 in offers


@respx.mock
@pytest.mark.asyncio
async def test_dedup_matches_by_email_when_phone_differs():
    """If phones differ but emails match → still a duplicate (email channel)."""
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {"id": 333, "emails": ["match@example.com"], "phones": ["+49 999"], "placements": []},
        ])),
    )
    exists, cid, _ = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY,
        email="match@example.com",
        phone="0123 456789",
    )
    assert exists is True
    assert cid == 333


@respx.mock
@pytest.mark.asyncio
async def test_dedup_miss_when_neither_email_nor_phone_match():
    """No email match + no phone match → miss."""
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {"id": 444, "emails": ["someone@else.com"], "phones": ["+49 888"], "placements": []},
        ])),
    )
    exists, cid, offers = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY,
        email="brand_new@candidate.com",
        phone="0179 1111111",
    )
    assert exists is False
    assert cid is None
    assert offers == []


@respx.mock
@pytest.mark.asyncio
async def test_dedup_returns_false_when_no_inputs():
    """If both email and phone are missing → return (False, None, []) without HTTP call."""
    # No respx mock — if the function tries to fetch, it will raise.
    exists, cid, offers = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY, email=None, phone=None,
    )
    assert exists is False and cid is None and offers == []


@respx.mock
@pytest.mark.asyncio
async def test_dedup_email_case_insensitive_and_whitespace_tolerant():
    """Emails with mixed case or surrounding whitespace still match."""
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {"id": 555, "emails": ["  MIXED@CASE.com  "], "phones": [], "placements": []},
        ])),
    )
    exists, _, _ = await check_candidate_exists_in_recruitee(
        token=TOKEN, company_id=COMPANY, email="mixed@case.com",
    )
    assert exists is True


@respx.mock
@pytest.mark.asyncio
async def test_dedup_phone_normalisation_matches_all_german_variants():
    """A single candidate phone in Recruitee should match all common German variants."""
    respx.get(f"{BASE}/candidates").mock(
        return_value=httpx.Response(200, json=_candidates_page([
            {"id": 666, "emails": [], "phones": ["+49 171 1234567"], "placements": []},
        ])),
    )
    for variant in ["+49 171 1234567", "0049 171 1234567", "0171 1234567", "01711234567", "(0171) 123-4567"]:
        clear_candidates_cache()
        respx.get(f"{BASE}/candidates").mock(
            return_value=httpx.Response(200, json=_candidates_page([
                {"id": 666, "emails": [], "phones": ["+49 171 1234567"], "placements": []},
            ])),
        )
        exists, _, _ = await check_candidate_exists_in_recruitee(
            token=TOKEN, company_id=COMPANY, email=None, phone=variant,
        )
        assert exists, f"phone variant {variant!r} should match"

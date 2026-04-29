import pytest
import httpx
import respx
import utils.recruitee as _recruitee_mod
from utils.recruitee import create_candidate, upload_cv, set_stage, RecruiteeError

TOKEN = "bearer_test"
COMPANY = "61932"
BASE = f"https://api.recruitee.com/c/{COMPANY}"


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Zero out retry backoff so tests don't sleep 2s per attempt."""
    monkeypatch.setattr(_recruitee_mod, "RETRY_DELAY_SECONDS", 0.0)


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

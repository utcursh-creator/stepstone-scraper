"""Tests for the _push_to_recruitee CV-gate in main.py.

Key invariant: a candidate without a CV must NEVER be created in Recruitee.
Verified by counting HTTP calls to the /candidates endpoint under respx.
"""
import pytest
import httpx
import respx

from main import _push_to_recruitee
from models.candidate import CandidateResult


TOKEN = "recruitee_test_token"
COMPANY = "61932"
BASE = f"https://api.recruitee.com/c/{COMPANY}"


def _profile(cv_base64: str | None) -> CandidateResult:
    return CandidateResult(
        name="Test Candidate",
        stepstone_profile_id="12345",
        email="test@example.com",
        phone="+49 171 1234567",
        cv_base64=cv_base64,
        cv_filename="cv.pdf",
        unlocked=True,
        unlock_reason="success",
        account_used="Account 1",
    )


@pytest.mark.asyncio
@respx.mock
async def test_push_aborts_when_cv_base64_is_none():
    """Missing CV → no create_candidate call, status=cv_missing."""
    # Mock all 3 endpoints. We expect ZERO calls.
    create_route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(201, json={"candidate": {"id": 1, "placements": [{"id": 2}]}})
    )
    upload_route = respx.patch(f"{BASE}/candidates/1/update_cv").mock(
        return_value=httpx.Response(200, json={})
    )
    stage_route = respx.patch(f"{BASE}/placements/2/change_stage").mock(
        return_value=httpx.Response(200, json={})
    )

    profile = _profile(cv_base64=None)
    await _push_to_recruitee(
        profile=profile, offer_id=2189981, stage_id=13055288,
        token=TOKEN, company_id=COMPANY,
    )

    assert profile.recruitee_status == "cv_missing"
    assert create_route.call_count == 0, "Should NOT have created a candidate"
    assert upload_route.call_count == 0
    assert stage_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_push_aborts_when_cv_base64_is_empty_string():
    """Empty-string cv_base64 is also a no-CV signal — abort."""
    create_route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(201, json={"candidate": {"id": 1, "placements": [{"id": 2}]}})
    )
    profile = _profile(cv_base64="")
    await _push_to_recruitee(
        profile=profile, offer_id=2189981, stage_id=13055288,
        token=TOKEN, company_id=COMPANY,
    )
    assert profile.recruitee_status == "cv_missing"
    assert create_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_push_proceeds_when_cv_base64_present():
    """Happy path: with cv_base64, all 3 endpoints are called."""
    import base64
    cv_bytes_b64 = base64.b64encode(b"%PDF-fake-content").decode()

    create_route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(201, json={"candidate": {"id": 1, "placements": [{"id": 2}]}})
    )
    upload_route = respx.patch(f"{BASE}/candidates/1/update_cv").mock(
        return_value=httpx.Response(200, json={"candidate": {"id": 1}})
    )
    stage_route = respx.patch(f"{BASE}/placements/2/change_stage").mock(
        return_value=httpx.Response(200, json={"placement": {"id": 2}})
    )

    profile = _profile(cv_base64=cv_bytes_b64)
    await _push_to_recruitee(
        profile=profile, offer_id=2189981, stage_id=13055288,
        token=TOKEN, company_id=COMPANY,
    )

    assert create_route.call_count == 1
    assert upload_route.call_count == 1
    assert stage_route.call_count == 1
    assert profile.recruitee_status == "stage_set"
    assert profile.recruitee_candidate_id == 1
    assert profile.recruitee_placement_id == 2
    assert profile.cv_uploaded is True

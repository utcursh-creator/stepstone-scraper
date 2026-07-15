import logging

import pytest
import httpx
import respx
from utils.airtable import is_duplicate


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_found():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(200, json={"records": [{"id": "rec123", "fields": {}}]})
    )
    result = await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id="2525450", profile_id="99999")
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_not_found():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    result = await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id="2525450", profile_id="99999")
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_api_error_returns_false():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(500, json={"error": "Server Error"})
    )
    result = await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id="2525450", profile_id="99999")
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_formula_coerces_fields_to_string():
    route = respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id="2517044", profile_id="20445142")
    request = route.calls.last.request
    formula = httpx.QueryParams(request.url.query).get("filterByFormula")
    # CSV-imported bases re-type columns as number; &"" coerces the field to
    # string so the comparison works for both number and text columns.
    assert '{Offer ID}&""="2517044"' in formula
    assert '{StepStone Profile ID}&""="20445142"' in formula


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_strips_double_quotes_from_ids():
    route = respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id='25"17044', profile_id='2044"5142')
    request = route.calls.last.request
    formula = httpx.QueryParams(request.url.query).get("filterByFormula")
    assert '{Offer ID}&""="2517044"' in formula
    assert '{StepStone Profile ID}&""="20445142"' in formula


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_401_returns_false_and_warns(caplog):
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(401, json={"error": {"type": "AUTHENTICATION_REQUIRED"}})
    )
    with caplog.at_level(logging.WARNING, logger="utils.airtable"):
        result = await is_duplicate(pat="pat_bad", base_id="appTEST", table_id="tblTEST", offer_id="2525450", profile_id="99999")
    assert result is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "dedup fail-open" in message
    assert "401" in message
    assert "appTEST" in message
    assert "tblTEST" in message


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_timeout_returns_false_and_warns(caplog):
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    with caplog.at_level(logging.WARNING, logger="utils.airtable"):
        result = await is_duplicate(pat="pat_test", base_id="appTEST", table_id="tblTEST", offer_id="2525450", profile_id="99999")
    assert result is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "dedup fail-open" in message
    assert "appTEST" in message

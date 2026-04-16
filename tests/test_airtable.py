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

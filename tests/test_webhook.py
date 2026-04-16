import pytest
import httpx
import respx
from utils.webhook import send_webhook
from models.candidate import ScrapeResult


@respx.mock
@pytest.mark.asyncio
async def test_send_webhook_success():
    route = respx.post("https://example.com/webhook").mock(return_value=httpx.Response(200, json={"ok": True}))
    result = ScrapeResult(offer_id="1", stage_id="2", job_title="Test", location="Berlin", account_used="Account 1")
    success = await send_webhook("https://example.com/webhook", result)
    assert success is True
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_send_webhook_failure():
    respx.post("https://example.com/webhook").mock(return_value=httpx.Response(500))
    result = ScrapeResult(offer_id="1", stage_id="2", job_title="Test", location="Berlin", account_used="Account 1")
    success = await send_webhook("https://example.com/webhook", result)
    assert success is False

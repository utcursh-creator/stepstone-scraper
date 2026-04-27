import json
import pytest
import httpx
import respx
from utils.openrouter import evaluate_candidate, EvalResult


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_match():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "match": True,
                                    "confidence": 0.9,
                                    "reasoning": "Strong fit",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Senior Bürofachkraft in Halle, 5 years experience",
        job_title="Bürofachkraft",
        location="Halle",
        requirements="Erfahrung im Büro",
    )
    assert result.match is True
    assert result.confidence == 0.9
    assert result.reasoning == "Strong fit"


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_no_match():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "match": False,
                                    "confidence": 0.3,
                                    "reasoning": "Wrong field",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Software Engineer in Munich",
        job_title="Bürofachkraft",
        location="Halle",
        requirements="",
    )
    assert result.match is False


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_markdown_fenced_json():
    """Claude Haiku 4.5 wraps JSON in ```json ... ``` fences."""
    fenced = '```json\n{"match": true, "confidence": 0.8, "reasoning": "Passt gut"}\n```'
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": fenced}}]},
        )
    )
    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Test",
        job_title="Test",
        location="Test",
        requirements="",
    )
    assert result.match is True
    assert result.confidence == 0.8
    assert result.reasoning == "Passt gut"


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_bare_fenced_json():
    """Some variants use ``` without json language tag."""
    fenced = '```\n{"match": false, "confidence": 0.2, "reasoning": "Nein"}\n```'
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": fenced}}]},
        )
    )
    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Test",
        job_title="Test",
        location="Test",
        requirements="",
    )
    assert result.match is False
    assert result.reasoning == "Nein"


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_malformed_response():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Test",
        job_title="Test",
        location="Test",
        requirements="",
    )
    assert result.match is False
    assert result.confidence == 0.0
    assert "parse" in result.reasoning.lower() or "error" in result.reasoning.lower()


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_with_distance_data():
    """When distance_km and wohnadresse are provided, prompt includes LOCATION DATA."""
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"match": true, "confidence": 0.9, "reasoning": "gut"}'}}]},
        )
    )
    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Senior Burofachkraft in Halle",
        job_title="Burofachkraft",
        location="Halle",
        requirements="",
        distance_km=42.5,
        wohnadresse="40880 Ratingen",
        gewuenschte_arbeitsorte="Halle Dortmund",
        max_distance_km=200,
    )
    assert result.match is True
    sent_body = route.calls[0].request.read()
    import json as _json
    payload = _json.loads(sent_body)
    prompt = payload["messages"][0]["content"]
    assert "LOCATION DATA" in prompt
    assert "42" in prompt
    assert "40880 Ratingen" in prompt


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_without_distance_data():
    """When distance_km is None, prompt does NOT include LOCATION DATA."""
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"match": false, "confidence": 0.2, "reasoning": "nein"}'}}]},
        )
    )
    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Test candidate",
        job_title="Test",
        location="Berlin",
        requirements="",
        distance_km=None,
    )
    assert result.match is False
    sent_body = route.calls[0].request.read()
    import json as _json
    payload = _json.loads(sent_body)
    prompt = payload["messages"][0]["content"]
    assert "LOCATION DATA" not in prompt

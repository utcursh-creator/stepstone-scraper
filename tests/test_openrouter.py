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

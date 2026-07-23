import json
import pytest
import httpx
import respx
from utils.openrouter import EVAL_PROMPT, evaluate_candidate, EvalResult


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
async def test_truncated_json_match_true_is_salvaged():
    """A response truncated by the token limit (unterminated reasoning string)
    must still recover match=true/confidence via regex, not be dropped as
    match=False. Regression for the 0.92/0.85/0.75 matches lost on 2026-06-01.
    """
    truncated = (
        '```json\n{\n  "match": true,\n  "confidence": 0.92,\n  '
        '"reasoning": "Kandidatin hat relevante Berufserfahrung als '
        'Rechtsanwaltsfachangestellte seit Januar 2024 bei Dr. Eick und Partner '
        'und die Stellenbezeichnung passt exakt zur Zielposition. Wohnort Dortmund '
        'liegt 31km'  # <-- cut off here, no closing quote/brace
    )
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": truncated}}]})
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x",
        job_title="Rechtsanwaltsfachangestellte", location="Hamm", requirements="",
    )
    assert result.match is True
    assert result.confidence == 0.92
    assert "Rechtsanwaltsfachangestellte" in result.reasoning
    assert result.error is False, "a salvaged verdict is a real verdict, not an error"


@respx.mock
@pytest.mark.asyncio
async def test_truncated_json_match_false_is_salvaged():
    truncated = '```json\n{"match": false, "confidence": 0.15, "reasoning": "Nur Ausbildung'
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": truncated}}]})
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x", job_title="X", location="Y", requirements="",
    )
    assert result.match is False
    assert result.confidence == 0.15
    assert result.error is False, "a salvaged match=false is a verdict, not an error"


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
    assert result.error is True, "an unparseable, unsalvageable response is an error, not a verdict"


# -- the error flag: an eval that could not be OBTAINED must be distinguishable
#    from a genuine match=False verdict, so the caller never emits (and n8n never
#    logs) a candidate that was silently burned by an outage. Prod 2026-07-22. --

@respx.mock
@pytest.mark.asyncio
async def test_402_payment_required_sets_error_flag():
    """The exact prod failure: OpenRouter out of funds → 402 on every call.
    Must come back error=True, NOT a match=False verdict."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": "Insufficient credits"})
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="strong candidate",
        job_title="X", location="Y", requirements="",
    )
    assert result.error is True
    assert result.match is False
    assert "402" in result.reasoning


@respx.mock
@pytest.mark.asyncio
async def test_timeout_sets_error_flag():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x", job_title="X", location="Y", requirements="",
    )
    assert result.error is True
    assert result.match is False


@respx.mock
@pytest.mark.asyncio
async def test_transport_error_sets_error_flag():
    """A connection failure must be a skippable eval error, not an unhandled
    exception that crashes the whole job."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x", job_title="X", location="Y", requirements="",
    )
    assert result.error is True
    assert result.match is False


@respx.mock
@pytest.mark.asyncio
async def test_non_json_envelope_sets_error_flag():
    """A 200 whose BODY isn't JSON (e.g. an HTML error page from a proxy) must
    be a skippable eval error, not an exception that crashes the whole job."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, text="<html>502 Bad Gateway</html>")
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x", job_title="X", location="Y", requirements="",
    )
    assert result.error is True
    assert result.match is False


@respx.mock
@pytest.mark.asyncio
async def test_empty_choices_sets_error_flag():
    """A 200 with an empty `choices` array (IndexError on [0]) must be a
    skippable eval error, not a job-crashing exception."""
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    result = await evaluate_candidate(
        api_key="sk-test", candidate_text="x", job_title="X", location="Y", requirements="",
    )
    assert result.error is True
    assert result.match is False


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


@respx.mock
@pytest.mark.asyncio
async def test_prompt_contains_internship_rejection_rule():
    """The prompt must instruct Claude to reject internship-only candidates.

    Regression test for Marlon Gehrmann (PTA Rüdesheim) and the LKW Bendestorf
    set — candidates whose only role-relevant experience was a 6-month Praktikum
    were matched and pushed to Recruitee under the old prompt's permissive Rules
    3 + 5 ("lean toward MATCH").
    """
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"match": false, "confidence": 0.2, "reasoning": "nur Praktikum"}'}}]},
        )
    )
    await evaluate_candidate(
        api_key="sk-test",
        candidate_text="01.02.2022-01.08.2022 PTA Apotheke Nastätten (Praktikum)",
        job_title="Pharmazeutisch-technische Assistenz",
        location="Rüdesheim am Rhein",
        requirements="",
    )
    sent_body = route.calls[0].request.read()
    import json as _json
    payload = _json.loads(sent_body)
    prompt = payload["messages"][0]["content"]
    # The prompt must explicitly mention internships are not qualifying
    assert "Praktikum" in prompt
    assert "Trainee" in prompt or "Werkstudent" in prompt
    assert "Ausbildung" in prompt
    # And NOT contain the old "lean toward MATCH" instruction
    assert "lean toward MATCH" not in prompt
    assert "cast a wide net" not in prompt


def test_prompt_contains_occupation_function_rule():
    """The prompt must enforce occupation/function match, not just industry/keyword
    overlap.

    Regression test for the LKW-Fahrer matched to an LKW Mechaniker job with
    confidence 0.75 — both profiles said "LKW", but a driver is not a mechanic.
    Recruiter Umair asked that candidates whose actual occupation differs from
    the target role be rejected even when they share an industry or keyword.
    """
    # Function-vs-industry wording
    assert "FUNCTION" in EVAL_PROMPT
    assert "industry" in EVAL_PROMPT
    # Concrete driver-vs-mechanic contrast (the LKW case)
    assert "Fahrer" in EVAL_PROMPT
    assert "Mechaniker" in EVAL_PROMPT
    # Salesperson-vs-producer contrast
    assert "Verkäufer" in EVAL_PROMPT
    assert "Bäcker" in EVAL_PROMPT
    # Assistant-vs-specialist and using-vs-servicing contrasts
    assert "assistant" in EVAL_PROMPT.lower()
    assert "specialist" in EVAL_PROMPT.lower()
    # Explicit consequence: differing core function -> match=false
    assert "match=false" in EVAL_PROMPT
    assert "keyword" in EVAL_PROMPT


def test_eval_prompt_format_placeholders_intact():
    """EVAL_PROMPT.format(...) with the exact kwargs used in evaluate_candidate
    must render without KeyError/IndexError — guards against prompt edits that
    add un-doubled literal braces or rename/remove placeholders.
    """
    rendered = EVAL_PROMPT.format(
        job_title="LKW Mechaniker (m/w/d)",
        location="Bendestorf",
        requirements="Erfahrung als Mechaniker",
        location_context="",
        candidate_text="LKW-Fahrer im Fernverkehr seit 2019",
    )
    assert "LKW Mechaniker (m/w/d)" in rendered
    assert "Bendestorf" in rendered
    assert "LKW-Fahrer im Fernverkehr seit 2019" in rendered
    # The literal JSON format instruction must survive formatting with single braces
    assert '{"match": true/false' in rendered
    # No unresolved placeholders left behind
    assert "{job_title}" not in rendered
    assert "{candidate_text}" not in rendered


@respx.mock
@pytest.mark.asyncio
async def test_sent_prompt_contains_occupation_function_rule():
    """The prompt actually sent to OpenRouter carries the occupation-strictness
    rule (mirrors test_prompt_contains_internship_rejection_rule)."""
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"match": false, "confidence": 0.2, "reasoning": "Fahrer, kein Mechaniker"}'}}]},
        )
    )
    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="LKW-Fahrer im Fernverkehr, 8 Jahre Erfahrung",
        job_title="LKW Mechaniker (m/w/d)",
        location="Bendestorf",
        requirements="Wartung und Reparatur von LKW",
    )
    assert result.match is False
    sent_body = route.calls[0].request.read()
    import json as _json
    payload = _json.loads(sent_body)
    prompt = payload["messages"][0]["content"]
    assert "Fahrer" in prompt
    assert "Mechaniker" in prompt
    assert "match=false" in prompt
    assert "industry" in prompt

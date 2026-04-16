import json
import httpx
from pydantic import BaseModel

MODEL = "anthropic/claude-haiku-4-5"
MAX_TOKENS = 300
TIMEOUT_SECONDS = 30

EVAL_PROMPT = """You are a German recruitment specialist evaluating candidates for a specific job.

JOB TITLE: {job_title}
JOB LOCATION: {location}
JOB REQUIREMENTS: {requirements}

CANDIDATE PREVIEW:
{candidate_text}

EVALUATION RULES:
1. Match the candidate's current or recent job title against the target job title
2. Consider location proximity (candidate should be within reasonable commuting distance)
3. CRITICAL: Do NOT penalize short tenures. If a candidate has been at their current job for only 1-2 months, this is POSITIVE — they are likely in probation period and open to new opportunities
4. Focus on role alignment, not exact keyword matching
5. When in doubt, lean toward MATCH (we want to cast a wide net; human recruiter reviews later)

Respond in JSON format only, no other text:
{{"match": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation in German"}}"""


class EvalResult(BaseModel):
    match: bool = False
    confidence: float = 0.0
    reasoning: str = ""


async def evaluate_candidate(
    api_key: str,
    candidate_text: str,
    job_title: str,
    location: str,
    requirements: str,
) -> EvalResult:
    """Evaluate a candidate using Claude Haiku 4.5 via OpenRouter."""
    prompt = EVAL_PROMPT.format(
        job_title=job_title,
        location=location,
        requirements=requirements,
        candidate_text=candidate_text,
    )

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": MAX_TOKENS,
                },
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            return EvalResult(
                match=data.get("match", False),
                confidence=data.get("confidence", 0.0),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            return EvalResult(reasoning="Error: could not parse evaluation response")
        except httpx.HTTPStatusError as e:
            return EvalResult(reasoning=f"Error: OpenRouter returned {e.response.status_code}")
        except httpx.TimeoutException:
            return EvalResult(reasoning="Error: evaluation timed out")

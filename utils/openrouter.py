import json
import logging
import re
import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MODEL = "anthropic/claude-haiku-4-5"
MAX_TOKENS = 300
TIMEOUT_SECONDS = 30

# Claude Haiku 4.5 wraps JSON in markdown fences like ```json ... ``` or ``` ... ```.
# Strip them before json.loads.
_MD_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

EVAL_PROMPT = """You are a German recruitment specialist evaluating candidates for a specific job.

JOB TITLE: {job_title}
JOB LOCATION: {location}
JOB REQUIREMENTS: {requirements}
{location_context}
CANDIDATE PREVIEW:
{candidate_text}

EVALUATION RULES:
1. CRITICAL — INTERNSHIPS / TRAINEE / AUSBILDUNG DO NOT COUNT AS QUALIFYING EXPERIENCE.
   The candidate must have at least one REAL, full-time professional position in
   the target role. If their ONLY experience in this field is a Praktikum, Trainee,
   Werkstudent, Ausbildung, Volontariat, or similar temporary/educational placement,
   set match=false. Look for the words "Praktikum", "Praktikant", "Trainee",
   "Werkstudent", "Ausbildung", "Volontariat", "Hospitanz" — if those are the only
   role-relevant entries on the candidate's CV/preview, REJECT.
2. Match the candidate's current or recent FULL-TIME job title against the target.
3. Consider location proximity (use the calculated distance shown below when
   provided; do NOT estimate distances yourself).
4. Short tenure in a real full-time role is acceptable — 1-2 months in a regular
   position can signal probation-period openness to new opportunities. But a short
   tenure that is itself labeled Praktikum/Ausbildung/Trainee does NOT qualify
   under Rule 1.
5. Focus on role alignment, not exact keyword matching, but require evidence of
   actual professional work in the field — not aspirational, not in-training,
   not as a side-project.
6. Make a defensible call. Do NOT default to match without clear professional
   evidence in the target role. If you have to argue yourself into a match
   ("they might have done X..."), that's a REJECT.

Respond in JSON format only, no other text:
{{"match": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation in German — if rejecting due to internship-only, say so explicitly"}}"""


class EvalResult(BaseModel):
    match: bool = False
    confidence: float = 0.0
    reasoning: str = ""


def _extract_json(content: str) -> str:
    """Extract JSON from Claude's response, stripping markdown fences if present."""
    m = _MD_FENCE_RE.match(content)
    if m:
        return m.group(1)
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start:end + 1]
    return stripped


def _build_location_context(
    distance_km: float | None,
    wohnadresse: str | None,
    gewuenschte_arbeitsorte: str | None,
    job_location: str,
    max_distance_km: int,
) -> str:
    """Build the LOCATION DATA section appended to the evaluation prompt.

    Returns an empty string when distance data is unavailable, so the prompt
    degrades gracefully to the original behaviour.
    """
    if distance_km is None or not wohnadresse:
        return ""
    return (
        f"\nLOCATION DATA (calculated, do not estimate distances yourself):\n"
        f"- Candidate home address: {wohnadresse} ({distance_km:.0f}km from {job_location})\n"
        f"- Candidate desired work locations: {gewuenschte_arbeitsorte or 'Not specified'}\n"
        f"- Job location: {job_location}\n"
        f"- Hard distance limit: {max_distance_km}km\n"
        f"\nUse this factual distance in your evaluation. If the candidate listed the job\n"
        f"city as a desired work location, treat this as a positive relocation signal.\n"
        f"Do NOT estimate distances yourself - use the calculated value above.\n"
    )


async def evaluate_candidate(
    api_key: str,
    candidate_text: str,
    job_title: str,
    location: str,
    requirements: str,
    distance_km: float | None = None,
    wohnadresse: str | None = None,
    gewuenschte_arbeitsorte: str | None = None,
    max_distance_km: int = 200,
) -> EvalResult:
    """Evaluate a candidate using Claude Haiku 4.5 via OpenRouter.

    When distance_km and wohnadresse are provided, a LOCATION DATA block is
    injected into the prompt so Claude can make an informed distance decision.
    When they are None, the prompt is identical to the original behaviour.
    """
    location_context = _build_location_context(
        distance_km=distance_km,
        wohnadresse=wohnadresse,
        gewuenschte_arbeitsorte=gewuenschte_arbeitsorte,
        job_location=location,
        max_distance_km=max_distance_km,
    )
    prompt = EVAL_PROMPT.format(
        job_title=job_title,
        location=location,
        requirements=requirements,
        location_context=location_context,
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
            json_str = _extract_json(content)
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                logger.warning(
                    f"Claude response could not be parsed as JSON. Raw content (first 400 chars): {content[:400]!r}"
                )
                return EvalResult(reasoning="Error: could not parse evaluation response")
            return EvalResult(
                match=bool(data.get("match", False)),
                confidence=float(data.get("confidence", 0.0)),
                reasoning=str(data.get("reasoning", "")),
            )
        except KeyError as e:
            logger.warning(f"Unexpected OpenRouter response shape: missing key {e}")
            return EvalResult(reasoning="Error: unexpected response shape from OpenRouter")
        except httpx.HTTPStatusError as e:
            return EvalResult(reasoning=f"Error: OpenRouter returned {e.response.status_code}")
        except httpx.TimeoutException:
            return EvalResult(reasoning="Error: evaluation timed out")

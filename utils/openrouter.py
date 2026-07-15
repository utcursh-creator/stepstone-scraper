import json
import logging
import re
import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MODEL = "anthropic/claude-haiku-4-5"
# 300 was too small: the German reasoning routinely ran past it, the JSON got
# truncated mid-string, json.loads failed, and genuine match=true candidates
# (seen 0.92/0.85/0.75 on 2026-06-01) were dropped as match=False. 1000 gives
# ample headroom; well-formed responses stop early so this costs nothing extra.
MAX_TOKENS = 1000
TIMEOUT_SECONDS = 30

# Claude Haiku 4.5 wraps JSON in markdown fences like ```json ... ``` or ``` ... ```.
# Strip them before json.loads.
_MD_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

# Salvage regexes — recover the decision from a response whose JSON didn't parse
# (e.g. truncated by the token limit). match/confidence are simple enough to pull
# out directly even when the trailing reasoning string is unterminated.
_SALVAGE_MATCH_RE = re.compile(r'"match"\s*:\s*(true|false)', re.IGNORECASE)
_SALVAGE_CONF_RE = re.compile(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)')
_SALVAGE_REASON_RE = re.compile(r'"reasoning"\s*:\s*"(.*)', re.DOTALL)

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
2. CRITICAL — THE CANDIDATE'S OCCUPATION/FUNCTION MUST MATCH THE TARGET ROLE'S
   FUNCTION. Sharing an industry, employer type, or keyword is NOT enough.
   A candidate whose day-to-day job function differs from the target role's
   function is NOT a match, no matter how much surface vocabulary overlaps:
   - A driver (Fahrer, e.g. LKW-Fahrer) is NOT a mechanic (Mechaniker,
     e.g. LKW-Mechaniker) — both mention "LKW", but driving a truck is a
     different occupation from repairing one.
   - A salesperson (Verkäufer/in, e.g. Bäckereifachverkäuferin) is NOT a
     producer/craftsman (e.g. Bäcker) — selling baked goods is not baking them.
   - An assistant is NOT a specialist in the same field.
   - Working WITH a system or product (operating/using it) is NOT the same as
     servicing, repairing, or consulting on it.
   If the candidate's core function differs from the role's core function, set
   match=false regardless of industry overlap or keyword hits.
3. Match the candidate's current or recent FULL-TIME job title against the target.
4. Consider location proximity (use the calculated distance shown below when
   provided; do NOT estimate distances yourself).
5. Short tenure in a real full-time role is acceptable — 1-2 months in a regular
   position can signal probation-period openness to new opportunities. But a short
   tenure that is itself labeled Praktikum/Ausbildung/Trainee does NOT qualify
   under Rule 1.
6. Focus on role alignment, not exact keyword matching, but require evidence of
   actual professional work in the field — not aspirational, not in-training,
   not as a side-project. Role alignment means FUNCTION alignment (Rule 2), not
   industry proximity.
7. Make a defensible call. Do NOT default to match without clear professional
   evidence in the target role. If you have to argue yourself into a match
   ("they might have done X..."), that's a REJECT.

Respond in JSON format only, no other text. Keep "reasoning" to ONE or TWO
short sentences in German (if rejecting due to internship-only, say so):
{{"match": true/false, "confidence": 0.0-1.0, "reasoning": "1-2 short sentences in German"}}"""


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


def _salvage_eval(content: str) -> "EvalResult | None":
    """Recover an EvalResult from a response whose JSON failed to parse.

    Pulls match/confidence/reasoning out with regexes so a truncated response
    (token-limit cut-off mid-reasoning) still yields the candidate's decision
    instead of being silently dropped as match=False. Returns None only when
    even the `match` field can't be found (genuinely unparseable).
    """
    m_match = _SALVAGE_MATCH_RE.search(content)
    if not m_match:
        return None
    matched = m_match.group(1).lower() == "true"
    m_conf = _SALVAGE_CONF_RE.search(content)
    confidence = float(m_conf.group(1)) if m_conf else 0.0
    m_reason = _SALVAGE_REASON_RE.search(content)
    reasoning = m_reason.group(1).strip().rstrip('`"}\n ').strip() if m_reason else ""
    return EvalResult(
        match=matched,
        confidence=confidence,
        reasoning=reasoning or "(salvaged from truncated response)",
    )


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
                salvaged = _salvage_eval(content)
                if salvaged is not None:
                    logger.warning(
                        f"Claude JSON did not parse (likely truncated); salvaged "
                        f"match={salvaged.match} conf={salvaged.confidence}. "
                        f"Raw (first 200): {content[:200]!r}"
                    )
                    return salvaged
                logger.warning(
                    f"Claude response could not be parsed as JSON and could not be "
                    f"salvaged. Raw content (first 400 chars): {content[:400]!r}"
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

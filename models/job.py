import re

from pydantic import BaseModel, ConfigDict, Field, AliasChoices, field_validator


# Recruitee tags that are NOT search keywords even though they carry a hashtag.
# The selector tag marks a job for the automation; radius tags set the distance.
# Both must be dropped if they leak into the keywords list.
_SELECTOR_TAGS = {"bensourcing"}
_RADIUS_TOKEN_RE = re.compile(r"^\d+\s*km$", re.IGNORECASE)
_FIRST_INT_RE = re.compile(r"\d+")


class JobInput(BaseModel):
    # Accept both 'job_title' (canonical) and 'title' (curl/n8n template)
    # `account` is honoured if provided (n8n picks based on credit balance);
    # `credits_remaining` is informational only — scraper does not use it.
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    offer_id: str
    stage_id: str
    job_title: str = Field(validation_alias=AliasChoices("job_title", "title"))
    location: str
    requirements: str = ""
    max_candidates: int = 50
    max_distance_km: int = 25  # Hard ceiling for distance rejection (km)
    # Job-specific StepStone keywords (e.g. "Armatur", "Wundversorgung"). Umair
    # tags the Recruitee offer (hashtag-prefixed under his scheme); n8n forwards
    # them as a list or comma-separated string. Each keyword becomes a STICHWORT
    # criterion ANDed into the StepStone search (narrows results, saves credits).
    # The validator below is intentionally forgiving about format — see
    # _normalize_keywords — so unexpected n8n output cannot crash the job.
    keywords: list[str] = []
    account: str | None = None  # Optional: email, "Account N", or "N" — n8n decides which account has credits

    @field_validator("max_distance_km", mode="before")
    @classmethod
    def _parse_distance(cls, v):
        """Accept int, "50", "50km", "#50km", "#50" → 50. Unparseable → 25.

        n8n derives this from a Recruitee tag like "#50km"; depending on its
        config it may forward the raw tag string rather than a clean integer.
        Parsing the first integer out keeps a messy tag from 422-crashing the
        whole job. Anything with no digits (missing / "keine Angabe") → the
        default 25 km.
        """
        if v is None or v == "":
            return 25
        if isinstance(v, bool):  # guard: bool is a subclass of int
            return 25
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            m = _FIRST_INT_RE.search(v)
            return int(m.group()) if m else 25
        return 25

    @field_validator("keywords", mode="before")
    @classmethod
    def _normalize_keywords(cls, v):
        """Normalize keywords from any n8n shape into a clean list of search terms.

        Handles: list or comma-separated string; strips leading hashtag(s) so
        "#Wundversorgung" → "Wundversorgung"; drops the automation selector
        (#BenSourcing) and radius tokens (#50km / "75 km") if they leak in;
        drops empties. Whatever survives is used verbatim as a STICHWORT term.

        It deliberately does NOT try to distinguish a real keyword from ATS
        noise (JOIN, Zimeda) when there's no hashtag — that is n8n's job (only
        forward relevant tags). The search layer has a 0-results fallback that
        retries without keywords, so even a leaked noise term cannot silently
        zero out a job.
        """
        if v is None or v == "":
            return []
        if isinstance(v, str):
            raw = v.split(",")
        elif isinstance(v, list):
            raw = [str(k) for k in v]
        else:
            return []

        out: list[str] = []
        for token in raw:
            k = token.strip().lstrip("#").strip()
            if not k:
                continue
            if k.lower() in _SELECTOR_TAGS:
                continue
            if _RADIUS_TOKEN_RE.match(k):
                continue
            out.append(k)
        return out

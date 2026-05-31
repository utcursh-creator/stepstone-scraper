from pydantic import BaseModel, ConfigDict, Field, AliasChoices, field_validator


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
    # Job-specific StepStone keywords (e.g. "Armatur"). Umair tags the Recruitee
    # offer; n8n forwards as either a list or a comma-separated string. Each
    # keyword becomes a STICHWORT criterion ANDed into the StepStone search,
    # narrowing results (which also REDUCES unlock spend).
    keywords: list[str] = []
    account: str | None = None  # Optional: email, "Account N", or "N" — n8n decides which account has credits

    @field_validator("keywords", mode="before")
    @classmethod
    def _normalize_keywords(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        if isinstance(v, list):
            return [str(k).strip() for k in v if str(k).strip()]
        return []

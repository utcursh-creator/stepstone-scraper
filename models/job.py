from pydantic import BaseModel, ConfigDict, Field, AliasChoices


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
    account: str | None = None  # Optional: email, "Account N", or "N" — n8n decides which account has credits

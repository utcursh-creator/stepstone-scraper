from pydantic import BaseModel, computed_field


class CandidateResult(BaseModel):
    name: str
    stepstone_profile_id: str
    email: str = ""
    phone: str = ""
    profile_text: str = ""
    matched: bool = False
    match_confidence: float = 0.0
    match_reasoning: str = ""
    unlocked: bool = False
    unlock_reason: str = ""
    cv_base64: str | None = None
    cv_filename: str = ""
    account_used: str = ""
    # Recruitee fields — populated after direct upload during scrape
    recruitee_candidate_id: int | None = None
    recruitee_placement_id: int | None = None
    cv_uploaded: bool = False
    recruitee_status: str = ""  # "created" | "cv_uploaded" | "stage_set" | "failed" | ""


class ScrapeResult(BaseModel):
    offer_id: str
    stage_id: str
    job_title: str
    location: str
    requirements: str = ""
    account_used: str
    candidates: list[CandidateResult] = []
    partial: bool = False

    @computed_field
    @property
    def candidates_scraped(self) -> int:
        return len(self.candidates)

    @computed_field
    @property
    def candidates_matched(self) -> int:
        return sum(1 for c in self.candidates if c.matched)

    @computed_field
    @property
    def candidates_unlocked(self) -> int:
        return sum(1 for c in self.candidates if c.unlocked)

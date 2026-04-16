from pydantic import BaseModel


class JobInput(BaseModel):
    offer_id: str
    stage_id: str
    job_title: str
    location: str
    requirements: str = ""
    max_candidates: int = 50

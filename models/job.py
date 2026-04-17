from pydantic import BaseModel, ConfigDict, Field, AliasChoices


class JobInput(BaseModel):
    # Accept both 'job_title' (canonical) and 'title' (curl/n8n template)
    # Ignore 'account' / 'credits_remaining' (handled internally by rotation.py)
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    offer_id: str
    stage_id: str
    job_title: str = Field(validation_alias=AliasChoices("job_title", "title"))
    location: str
    requirements: str = ""
    max_candidates: int = 50

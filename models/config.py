import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    proxy_host: str
    proxy_port: int = 12321
    proxy_user: str
    proxy_pass: str
    proxy_country: str = "DE"

    stepstone_email_1: str
    stepstone_pass_1: str
    stepstone_email_2: str = ""
    stepstone_pass_2: str = ""

    openrouter_api_key: str

    airtable_pat: str
    airtable_base_id: str
    airtable_candidates_table: str
    airtable_credit_table: str

    n8n_webhook_url: str

    twocaptcha_api_key: str = ""

    # Recruitee — used to push candidates directly during scrape
    recruitee_api_token: str = ""
    recruitee_company_id: str = "61932"

    # Talent pool — post-unlock rejects (too far / location unknown) get
    # pushed here instead of being silently dropped, so a recruiter can
    # manually review borderline candidates whose workplace city matches
    # but whose Wohnort doesn't (or is missing). Both must be set for the
    # feature to activate; either unset disables talent pool push.
    recruitee_talent_pool_offer_id: int | None = None
    recruitee_talent_pool_stage_id: int | None = None

    scrape_timeout_seconds: int = 1200
    max_candidates_per_job: int = 50

    # Distance gate — second-tier "relocation feasibility" cap.
    # When a candidate's Wohnort exceeds the per-job max_distance_km BUT they
    # listed the job city under "Gewünschter Arbeitsort" (relocation signal),
    # the older softening logic let them through regardless of actual distance.
    # Umair flagged Suraj Gajbhar (Koch, ~120 km from Apfeltrang) as having
    # been pushed via this softening despite living too far to realistically
    # commute or relocate quickly. This cap REJECTS relocation-signal
    # candidates whose Wohnort is beyond `relocation_max_distance_km`,
    # regardless of their stated desire. Set to 0 to disable softening
    # entirely (pure Wohnort-only mode, no relocation acceptance).
    relocation_max_distance_km: int = 200

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    def get_accounts(self) -> list[dict]:
        accounts = [
            {"email": self.stepstone_email_1, "password": self.stepstone_pass_1},
        ]
        if self.stepstone_email_2 and self.stepstone_pass_2:
            accounts.append(
                {"email": self.stepstone_email_2, "password": self.stepstone_pass_2}
            )
        return accounts

from models.config import Settings
from models.job import JobInput
from models.candidate import CandidateResult, ScrapeResult


def test_job_input_valid():
    job = JobInput(
        offer_id="2525450",
        stage_id="12951264",
        job_title="Bürofachkraft",
        location="Halle",
        requirements="Bürofachkraft mit Erfahrung",
    )
    assert job.offer_id == "2525450"
    assert job.job_title == "Bürofachkraft"


def test_job_input_defaults():
    job = JobInput(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
    )
    assert job.requirements == ""
    assert job.max_candidates == 50


def test_candidate_result():
    c = CandidateResult(
        name="Test User",
        stepstone_profile_id="12345",
        matched=True,
        match_confidence=0.85,
        match_reasoning="Good fit",
    )
    assert c.name == "Test User"
    assert c.unlocked is False
    assert c.cv_base64 is None


def test_scrape_result():
    r = ScrapeResult(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
        account_used="Account 1",
        candidates=[],
    )
    assert r.candidates_scraped == 0
    assert r.candidates_matched == 0
    assert r.partial is False


def test_settings_accounts_parsing(monkeypatch):
    monkeypatch.setenv("PROXY_HOST", "geo.iproyal.com")
    monkeypatch.setenv("PROXY_PORT", "12321")
    monkeypatch.setenv("PROXY_USER", "user")
    monkeypatch.setenv("PROXY_PASS", "pass")
    monkeypatch.setenv("PROXY_COUNTRY", "DE")
    monkeypatch.setenv("STEPSTONE_EMAIL_1", "a@test.com")
    monkeypatch.setenv("STEPSTONE_PASS_1", "pw1")
    monkeypatch.setenv("STEPSTONE_EMAIL_2", "b@test.com")
    monkeypatch.setenv("STEPSTONE_PASS_2", "pw2")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("AIRTABLE_PAT", "pat_test")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")
    monkeypatch.setenv("AIRTABLE_CANDIDATES_TABLE", "tbl_test")
    monkeypatch.setenv("AIRTABLE_CREDIT_TABLE", "tbl_test2")
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setenv("TWOCAPTCHA_API_KEY", "key")
    s = Settings()
    accounts = s.get_accounts()
    assert len(accounts) == 2
    assert accounts[0]["email"] == "a@test.com"
    assert accounts[1]["email"] == "b@test.com"


def test_candidate_result_recruitee_defaults():
    c = CandidateResult(
        name="Maria Muster",
        stepstone_profile_id="99999",
    )
    assert c.recruitee_candidate_id is None
    assert c.recruitee_placement_id is None
    assert c.cv_uploaded is False
    assert c.recruitee_status == ""


def test_candidate_result_recruitee_populated():
    c = CandidateResult(
        name="Maria Muster",
        stepstone_profile_id="99999",
        recruitee_candidate_id=12345,
        recruitee_placement_id=67890,
        cv_uploaded=True,
        recruitee_status="stage_set",
    )
    assert c.recruitee_candidate_id == 12345
    assert c.recruitee_placement_id == 67890
    assert c.cv_uploaded is True
    assert c.recruitee_status == "stage_set"


def test_settings_recruitee_fields(monkeypatch):
    # Set all required fields
    for k, v in {
        "PROXY_HOST": "geo.iproyal.com", "PROXY_PORT": "12321",
        "PROXY_USER": "user", "PROXY_PASS": "pass", "PROXY_COUNTRY": "DE",
        "STEPSTONE_EMAIL_1": "a@test.com", "STEPSTONE_PASS_1": "pw1",
        "OPENROUTER_API_KEY": "sk-test", "AIRTABLE_PAT": "pat_test",
        "AIRTABLE_BASE_ID": "app_test", "AIRTABLE_CANDIDATES_TABLE": "tbl_test",
        "AIRTABLE_CREDIT_TABLE": "tbl_test2", "N8N_WEBHOOK_URL": "https://x.com/wh",
        "RECRUITEE_API_TOKEN": "bearer_test", "RECRUITEE_COMPANY_ID": "61932",
    }.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.recruitee_api_token == "bearer_test"
    assert s.recruitee_company_id == "61932"


def test_job_input_max_distance_km_default():
    job = JobInput(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
    )
    assert job.max_distance_km == 200


def test_job_input_max_distance_km_custom():
    job = JobInput(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
        max_distance_km=75,
    )
    assert job.max_distance_km == 75

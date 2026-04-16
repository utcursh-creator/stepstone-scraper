# StepStone DirectSearch Scraper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI service that scrapes StepStone DirectSearch for matching candidates, evaluates them with Claude, and POSTs results to n8n.

**Architecture:** Self-hosted Patchright browser with IPRoyal residential proxy on Hetzner CX22. Direct login to StepStone with session persistence and account rotation. Claude Haiku 4.5 for candidate evaluation. Airtable for dedup.

**Tech Stack:** Python 3.11+, FastAPI, Patchright, IPRoyal, OpenRouter (Claude Haiku 4.5), Airtable, 2captcha, Docker

**Spec:** `docs/specs/2026-04-17-stepstone-scraper-design.md`

---

## File Map

```
aramas-stepstone-scraper/
├── main.py                     # FastAPI app — 3 endpoints, background scrape task
├── scraper/
│   ├── __init__.py
│   ├── browser.py              # create_browser() — Patchright + proxy + German locale
│   ├── auth.py                 # authenticate() — login + session persist + CAPTCHA
│   ├── search.py               # search_candidates() — keyword + location + radius + pagination
│   ├── evaluate.py             # evaluate_candidate() — Claude Haiku 4.5 via OpenRouter
│   ├── profile.py              # extract_profile() — modal dialog + CV download
│   ├── dedup.py                # is_duplicate() — Airtable check
│   └── rotation.py             # next_account() — round-robin with disk counter
├── models/
│   ├── __init__.py
│   ├── job.py                  # JobInput pydantic model
│   ├── candidate.py            # CandidateResult pydantic model
│   └── config.py               # Settings from .env via pydantic-settings
├── utils/
│   ├── __init__.py
│   ├── airtable.py             # AirtableClient — dedup + credit ledger
│   ├── openrouter.py           # OpenRouterClient — Claude eval
│   ├── webhook.py              # send_webhook() — POST results to n8n
│   └── delays.py               # human_delay(), random_delay()
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_rotation.py
│   ├── test_delays.py
│   ├── test_openrouter.py
│   ├── test_airtable.py
│   └── test_webhook.py
├── sessions/                   # gitignored — saved login cookies
├── state/                      # gitignored — account counter
├── screenshots/                # gitignored — debug screenshots
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── pytest.ini
```

---

### Task 1: Project Scaffold

**Files:**
- Create: `.gitignore`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.env.example`
- Create: `pytest.ini`
- Create: `sessions/.gitkeep`
- Create: `state/.gitkeep`
- Create: `screenshots/.gitkeep`
- Create: `scraper/__init__.py`
- Create: `models/__init__.py`
- Create: `utils/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Initialize git repo**

```bash
cd D:/aramas-stepstone-scraper
git init
```

- [ ] **Step 2: Create .gitignore**

```
# .gitignore
__pycache__/
*.pyc
.env
sessions/
state/
screenshots/
*.png
*.txt
!requirements*.txt
.pytest_cache/
venv/
.venv/
stepstone_cookies.json
step0_*.py
```

- [ ] **Step 3: Create requirements.txt**

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
patchright>=1.58.0
httpx>=0.28.0
python-dotenv>=1.0.1
pydantic>=2.10.0
pydantic-settings>=2.5.0
python-multipart>=0.0.9
2captcha-python>=1.0.3
```

- [ ] **Step 4: Create requirements-dev.txt**

```
-r requirements.txt
pytest>=8.0.0
pytest-asyncio>=0.24.0
respx>=0.22.0
```

- [ ] **Step 5: Create .env.example**

```bash
# Proxy
PROXY_HOST=geo.iproyal.com
PROXY_PORT=12321
PROXY_USER=your_iproyal_username
PROXY_PASS=your_iproyal_password
PROXY_COUNTRY=DE

# StepStone Accounts (round-robin rotation)
STEPSTONE_EMAIL_1=ba@aramaz-digital.de
STEPSTONE_PASS_1=changeme
STEPSTONE_EMAIL_2=mj@aramaz-digital.de
STEPSTONE_PASS_2=changeme

# OpenRouter (Claude Haiku 4.5)
OPENROUTER_API_KEY=sk-or-v1-changeme

# Airtable
AIRTABLE_PAT=pat_changeme
AIRTABLE_BASE_ID=app_changeme
AIRTABLE_CANDIDATES_TABLE=tbl_changeme
AIRTABLE_CREDIT_TABLE=tbl_changeme

# n8n
N8N_WEBHOOK_URL=https://aramazdigital.app.n8n.cloud/webhook/stepstone-results

# Recruitee
RECRUITEE_API_KEY=changeme
RECRUITEE_COMPANY_ID=61932

# 2captcha (fallback)
TWOCAPTCHA_API_KEY=changeme

# App
SCRAPE_TIMEOUT_SECONDS=1200
MAX_CANDIDATES_PER_JOB=50
```

- [ ] **Step 6: Create pytest.ini**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 7: Create directory structure + __init__.py files**

```bash
mkdir -p sessions state screenshots scraper models utils tests
touch scraper/__init__.py models/__init__.py utils/__init__.py tests/__init__.py
touch sessions/.gitkeep state/.gitkeep screenshots/.gitkeep
```

- [ ] **Step 8: Install dependencies**

```bash
pip install -r requirements-dev.txt
patchright install chromium
```

Expected: all packages install successfully, Chromium downloads.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: project scaffold with deps, .env template, test config"
```

---

### Task 2: Pydantic Models + Config

**Files:**
- Create: `models/config.py`
- Create: `models/job.py`
- Create: `models/candidate.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write tests for models**

```python
# tests/test_models.py
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
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_models.py -v
```

Expected: `ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: Create models/config.py**

```python
# models/config.py
import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Proxy
    proxy_host: str
    proxy_port: int = 12321
    proxy_user: str
    proxy_pass: str
    proxy_country: str = "DE"

    # StepStone accounts
    stepstone_email_1: str
    stepstone_pass_1: str
    stepstone_email_2: str = ""
    stepstone_pass_2: str = ""

    # OpenRouter
    openrouter_api_key: str

    # Airtable
    airtable_pat: str
    airtable_base_id: str
    airtable_candidates_table: str
    airtable_credit_table: str

    # n8n
    n8n_webhook_url: str

    # 2captcha
    twocaptcha_api_key: str = ""

    # App
    scrape_timeout_seconds: int = 1200
    max_candidates_per_job: int = 50

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def get_accounts(self) -> list[dict]:
        accounts = [
            {"email": self.stepstone_email_1, "password": self.stepstone_pass_1},
        ]
        if self.stepstone_email_2 and self.stepstone_pass_2:
            accounts.append(
                {"email": self.stepstone_email_2, "password": self.stepstone_pass_2}
            )
        return accounts
```

- [ ] **Step 4: Create models/job.py**

```python
# models/job.py
from pydantic import BaseModel


class JobInput(BaseModel):
    offer_id: str
    stage_id: str
    job_title: str
    location: str
    requirements: str = ""
    max_candidates: int = 50
```

- [ ] **Step 5: Create models/candidate.py**

```python
# models/candidate.py
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
```

- [ ] **Step 6: Run tests — expect pass**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_models.py -v
```

Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add models/ tests/test_models.py
git commit -m "feat: pydantic models for job input, candidate output, and settings"
```

---

### Task 3: Utility Modules — Delays + Account Rotation

**Files:**
- Create: `utils/delays.py`
- Create: `scraper/rotation.py`
- Create: `tests/test_delays.py`
- Create: `tests/test_rotation.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_delays.py
import asyncio
from utils.delays import random_delay, human_delay


def test_random_delay_in_range():
    for _ in range(100):
        d = random_delay(1000, 2000)
        assert 1.0 <= d <= 2.0


def test_random_delay_returns_float():
    d = random_delay(500, 1500)
    assert isinstance(d, float)
```

```python
# tests/test_rotation.py
import json
import os
import tempfile
from scraper.rotation import next_account, read_counter, write_counter


def test_read_counter_missing_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        assert read_counter(path) == 0


def test_write_and_read_counter():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        write_counter(path, 5)
        assert read_counter(path) == 5


def test_next_account_round_robin():
    accounts = [
        {"email": "a@test.com", "password": "pw1"},
        {"email": "b@test.com", "password": "pw2"},
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        a1 = next_account(accounts, path)
        a2 = next_account(accounts, path)
        a3 = next_account(accounts, path)
        assert a1["email"] == "a@test.com"
        assert a2["email"] == "b@test.com"
        assert a3["email"] == "a@test.com"
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_delays.py tests/test_rotation.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create utils/delays.py**

```python
# utils/delays.py
import asyncio
import random


def random_delay(min_ms: int, max_ms: int) -> float:
    """Return a random delay in seconds between min_ms and max_ms milliseconds."""
    return random.randint(min_ms, max_ms) / 1000.0


async def human_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
    """Sleep for a random human-like duration."""
    await asyncio.sleep(random_delay(min_ms, max_ms))
```

- [ ] **Step 4: Create scraper/rotation.py**

```python
# scraper/rotation.py
import json
import os


def read_counter(path: str) -> int:
    """Read the rotation counter from disk. Returns 0 if file missing."""
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        data = json.load(f)
    return data.get("counter", 0)


def write_counter(path: str, value: int) -> None:
    """Write the rotation counter to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"counter": value}, f)


def next_account(accounts: list[dict], counter_path: str) -> dict:
    """Pick the next account via round-robin. Persists counter to disk."""
    counter = read_counter(counter_path)
    account = accounts[counter % len(accounts)]
    write_counter(counter_path, counter + 1)
    return account
```

- [ ] **Step 5: Run tests — expect pass**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_delays.py tests/test_rotation.py -v
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add utils/delays.py scraper/rotation.py tests/test_delays.py tests/test_rotation.py
git commit -m "feat: human-like delay helpers and account rotation with disk persistence"
```

---

### Task 4: OpenRouter Client

**Files:**
- Create: `utils/openrouter.py`
- Create: `tests/test_openrouter.py`

- [ ] **Step 1: Write tests (using respx to mock httpx)**

```python
# tests/test_openrouter.py
import json
import pytest
import httpx
import respx
from utils.openrouter import evaluate_candidate, EvalResult, EVAL_PROMPT


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_match():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "match": True,
                                    "confidence": 0.9,
                                    "reasoning": "Strong fit",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Senior Bürofachkraft in Halle, 5 years experience",
        job_title="Bürofachkraft",
        location="Halle",
        requirements="Erfahrung im Büro",
    )
    assert result.match is True
    assert result.confidence == 0.9
    assert result.reasoning == "Strong fit"


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_no_match():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "match": False,
                                    "confidence": 0.3,
                                    "reasoning": "Wrong field",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Software Engineer in Munich",
        job_title="Bürofachkraft",
        location="Halle",
        requirements="",
    )
    assert result.match is False


@respx.mock
@pytest.mark.asyncio
async def test_evaluate_candidate_malformed_response():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
        )
    )

    result = await evaluate_candidate(
        api_key="sk-test",
        candidate_text="Test",
        job_title="Test",
        location="Test",
        requirements="",
    )
    assert result.match is False
    assert result.confidence == 0.0
    assert "parse" in result.reasoning.lower() or "error" in result.reasoning.lower()
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_openrouter.py -v
```

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Create utils/openrouter.py**

```python
# utils/openrouter.py
import json
import httpx
from pydantic import BaseModel

MODEL = "anthropic/claude-haiku-4-5"
MAX_TOKENS = 300
TIMEOUT_SECONDS = 30

EVAL_PROMPT = """You are a German recruitment specialist evaluating candidates for a specific job.

JOB TITLE: {job_title}
JOB LOCATION: {location}
JOB REQUIREMENTS: {requirements}

CANDIDATE PREVIEW:
{candidate_text}

EVALUATION RULES:
1. Match the candidate's current or recent job title against the target job title
2. Consider location proximity (candidate should be within reasonable commuting distance)
3. CRITICAL: Do NOT penalize short tenures. If a candidate has been at their current job for only 1-2 months, this is POSITIVE — they are likely in probation period and open to new opportunities
4. Focus on role alignment, not exact keyword matching
5. When in doubt, lean toward MATCH (we want to cast a wide net; human recruiter reviews later)

Respond in JSON format only, no other text:
{{"match": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation in German"}}"""


class EvalResult(BaseModel):
    match: bool = False
    confidence: float = 0.0
    reasoning: str = ""


async def evaluate_candidate(
    api_key: str,
    candidate_text: str,
    job_title: str,
    location: str,
    requirements: str,
) -> EvalResult:
    """Evaluate a candidate using Claude Haiku 4.5 via OpenRouter."""
    prompt = EVAL_PROMPT.format(
        job_title=job_title,
        location=location,
        requirements=requirements,
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
            data = json.loads(content)
            return EvalResult(
                match=data.get("match", False),
                confidence=data.get("confidence", 0.0),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            return EvalResult(reasoning="Error: could not parse evaluation response")
        except httpx.HTTPStatusError as e:
            return EvalResult(reasoning=f"Error: OpenRouter returned {e.response.status_code}")
        except httpx.TimeoutException:
            return EvalResult(reasoning="Error: evaluation timed out")
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_openrouter.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add utils/openrouter.py tests/test_openrouter.py
git commit -m "feat: OpenRouter client for Claude Haiku 4.5 candidate evaluation"
```

---

### Task 5: Airtable Client

**Files:**
- Create: `utils/airtable.py`
- Create: `tests/test_airtable.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_airtable.py
import pytest
import httpx
import respx
from utils.airtable import is_duplicate


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_found():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"id": "rec123", "fields": {}}]},
        )
    )

    result = await is_duplicate(
        pat="pat_test",
        base_id="appTEST",
        table_id="tblTEST",
        offer_id="2525450",
        profile_id="99999",
    )
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_not_found():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(200, json={"records": []}),
    )

    result = await is_duplicate(
        pat="pat_test",
        base_id="appTEST",
        table_id="tblTEST",
        offer_id="2525450",
        profile_id="99999",
    )
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_is_duplicate_api_error_returns_false():
    respx.get("https://api.airtable.com/v0/appTEST/tblTEST").mock(
        return_value=httpx.Response(500, json={"error": "Server Error"}),
    )

    result = await is_duplicate(
        pat="pat_test",
        base_id="appTEST",
        table_id="tblTEST",
        offer_id="2525450",
        profile_id="99999",
    )
    assert result is False
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_airtable.py -v
```

- [ ] **Step 3: Create utils/airtable.py**

```python
# utils/airtable.py
import asyncio
import httpx

AIRTABLE_API = "https://api.airtable.com/v0"
RATE_LIMIT_DELAY = 0.2  # 200ms between requests (5 req/sec limit)


async def is_duplicate(
    pat: str,
    base_id: str,
    table_id: str,
    offer_id: str,
    profile_id: str,
) -> bool:
    """Check if a candidate was already scraped for this offer."""
    formula = f"AND({{Offer ID}}=\"{offer_id}\",{{StepStone Profile ID}}=\"{profile_id}\")"
    url = f"{AIRTABLE_API}/{base_id}/{table_id}"

    async with httpx.AsyncClient() as client:
        try:
            await asyncio.sleep(RATE_LIMIT_DELAY)
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {pat}"},
                params={"filterByFormula": formula, "maxRecords": "1"},
                timeout=10.0,
            )
            response.raise_for_status()
            records = response.json().get("records", [])
            return len(records) > 0
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return False
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_airtable.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add utils/airtable.py tests/test_airtable.py
git commit -m "feat: Airtable client for candidate dedup"
```

---

### Task 6: Webhook Sender

**Files:**
- Create: `utils/webhook.py`
- Create: `tests/test_webhook.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_webhook.py
import pytest
import httpx
import respx
from utils.webhook import send_webhook
from models.candidate import ScrapeResult


@respx.mock
@pytest.mark.asyncio
async def test_send_webhook_success():
    route = respx.post("https://example.com/webhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = ScrapeResult(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
        account_used="Account 1",
    )
    success = await send_webhook("https://example.com/webhook", result)
    assert success is True
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_send_webhook_failure():
    respx.post("https://example.com/webhook").mock(
        return_value=httpx.Response(500)
    )

    result = ScrapeResult(
        offer_id="1",
        stage_id="2",
        job_title="Test",
        location="Berlin",
        account_used="Account 1",
    )
    success = await send_webhook("https://example.com/webhook", result)
    assert success is False
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_webhook.py -v
```

- [ ] **Step 3: Create utils/webhook.py**

```python
# utils/webhook.py
import httpx
from models.candidate import ScrapeResult


async def send_webhook(url: str, result: ScrapeResult) -> bool:
    """POST scrape results to the n8n webhook. Returns True on success."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                json=result.model_dump(),
                timeout=30.0,
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return False
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/test_webhook.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add utils/webhook.py tests/test_webhook.py
git commit -m "feat: n8n webhook sender"
```

---

### Task 7: Browser Launch Module

**Files:**
- Create: `scraper/browser.py`

No unit tests for this module — it requires a live browser + proxy. Verified via Step 0 integration test (Task 13).

- [ ] **Step 1: Create scraper/browser.py**

```python
# scraper/browser.py
import uuid
from patchright.async_api import async_playwright, Browser, BrowserContext, Page


async def create_browser(
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_pass: str,
    proxy_country: str = "DE",
) -> tuple[Browser, BrowserContext, Page]:
    """Launch a stealth Patchright browser with IPRoyal residential proxy."""
    session_id = uuid.uuid4().hex[:12]
    p = await async_playwright().start()

    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        proxy={
            "server": f"http://{proxy_host}:{proxy_port}",
            "username": f"{proxy_user}_country-{proxy_country}_session-{session_id}",
            "password": proxy_pass,
        },
    )

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
    )

    page = await context.new_page()
    page.set_default_navigation_timeout(120_000)

    return browser, context, page


async def close_browser(browser: Browser) -> None:
    """Safely close the browser."""
    try:
        await browser.close()
    except Exception:
        pass
```

- [ ] **Step 2: Verify import works**

```bash
cd D:/aramas-stepstone-scraper && python -c "from scraper.browser import create_browser; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scraper/browser.py
git commit -m "feat: Patchright browser launch with IPRoyal proxy and German locale"
```

---

### Task 8: Authentication Module

**Files:**
- Create: `scraper/auth.py`

No unit tests — requires live StepStone. Verified via Step 0.5 integration test (Task 14).

- [ ] **Step 1: Create scraper/auth.py**

```python
# scraper/auth.py
import json
import os
import re
from patchright.async_api import BrowserContext, Page
from utils.delays import human_delay

LOGIN_URL = "https://www.stepstone.de/5/index.cfm?event=login"
DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"


class AuthenticationError(Exception):
    pass


def _session_path(email: str) -> str:
    """Convert email to a safe filename for session storage."""
    safe = re.sub(r"[^a-zA-Z0-9]", "_", email)
    return os.path.join("sessions", f"{safe}.json")


def _load_session(path: str) -> list[dict] | None:
    """Load saved cookies from disk. Returns None if file missing or invalid."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _save_session(path: str, cookies: list[dict]) -> None:
    """Save cookies to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cookies, f)


def _is_login_page(url: str) -> bool:
    """Check if the current URL is a login page."""
    lower = url.lower()
    return any(k in lower for k in ["login", "anmelden", "signin"])


async def _dismiss_cookie_banner(page: Page) -> None:
    """Try to dismiss the cookie consent banner if present."""
    for selector in [
        "button[data-testid='cookie-accept']",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Accept')",
        "#onetrust-accept-btn-handler",
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await human_delay(500, 1000)
                return
        except Exception:
            continue


async def authenticate(
    context: BrowserContext,
    page: Page,
    email: str,
    password: str,
    captcha_solver=None,
) -> None:
    """Authenticate to StepStone DirectSearch.

    Tries saved session first, falls back to fresh login.
    Raises AuthenticationError if login fails.
    """
    session_file = _session_path(email)

    # 1. Try saved session
    saved_cookies = _load_session(session_file)
    if saved_cookies:
        await context.add_cookies(saved_cookies)
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
        await human_delay(1000, 2000)
        if not _is_login_page(page.url):
            return  # Session still valid

    # 2. Fresh login
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await human_delay(2000, 4000)
    await _dismiss_cookie_banner(page)

    # Find and fill email field
    email_field = None
    for selector in [
        "input[name='login']",
        "input[name='email']",
        "input[name='username']",
        "input[type='email']",
        "input[id='login']",
    ]:
        email_field = await page.query_selector(selector)
        if email_field:
            break

    if not email_field:
        await page.screenshot(path="screenshots/login_no_email_field.png")
        raise AuthenticationError("Could not find email input field on login page")

    await email_field.fill(email)
    await human_delay(500, 1500)

    # Find and fill password field
    password_field = await page.query_selector("input[type='password']")
    if not password_field:
        await page.screenshot(path="screenshots/login_no_password_field.png")
        raise AuthenticationError("Could not find password field on login page")

    await password_field.fill(password)
    await human_delay(500, 1500)

    # Submit
    submit_btn = None
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Anmelden')",
        "button:has-text('Login')",
        "button:has-text('Einloggen')",
    ]:
        submit_btn = await page.query_selector(selector)
        if submit_btn:
            break

    if submit_btn:
        await submit_btn.click()
    else:
        await password_field.press("Enter")

    # Wait for navigation
    await human_delay(3000, 5000)

    # 3. CAPTCHA handling (if solver provided)
    if captcha_solver:
        captcha_frame = await page.query_selector("iframe[src*='recaptcha']")
        if captcha_frame:
            sitekey = await captcha_frame.get_attribute("data-sitekey")
            if sitekey:
                try:
                    result = captcha_solver.recaptcha(sitekey=sitekey, url=page.url)
                    await page.evaluate(
                        f"document.getElementById('g-recaptcha-response').innerHTML = '{result['code']}'"
                    )
                    await human_delay(1000, 2000)
                except Exception:
                    pass  # CAPTCHA solving failed, continue anyway

    # 4. Verify login
    if _is_login_page(page.url):
        await page.screenshot(path="screenshots/login_failed.png")
        raise AuthenticationError(f"Login failed for {email} — still on login page")

    # 5. Save session
    cookies = await context.cookies()
    _save_session(session_file, cookies)
```

- [ ] **Step 2: Verify import works**

```bash
cd D:/aramas-stepstone-scraper && python -c "from scraper.auth import authenticate, AuthenticationError; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scraper/auth.py
git commit -m "feat: StepStone direct login with session persistence and CAPTCHA handling"
```

---

### Task 9: Search Module

**Files:**
- Create: `scraper/search.py`

No unit tests — requires live DirectSearch page. Verified in Step 1 integration test.

- [ ] **Step 1: Create scraper/search.py**

```python
# scraper/search.py
from patchright.async_api import Page
from utils.delays import human_delay

DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"
RADIUS_STEPS = [25, 50, 75, 100]


class SearchResult:
    def __init__(self, profile_id: str, preview_text: str):
        self.profile_id = profile_id
        self.preview_text = preview_text


async def _enter_keyword(page: Page, job_title: str) -> None:
    """Enter the job title into the keyword search field."""
    # Try known selectors for the keyword input
    for selector in [
        "input[name='searchtext']",
        "input[placeholder*='Jobtitel']",
        "input[placeholder*='jobtitel']",
        "input[placeholder*='Suchbegriff']",
        "#searchtext",
    ]:
        field = await page.query_selector(selector)
        if field:
            await field.fill(job_title)
            await human_delay(500, 1000)
            return
    raise RuntimeError("Could not find keyword search field")


async def _enter_location(page: Page, location: str) -> None:
    """Enter location using structured autocomplete to prevent mismatches."""
    for selector in [
        "input[name='location']",
        "input[placeholder*='Ort']",
        "input[placeholder*='Stadt']",
        "#location",
    ]:
        field = await page.query_selector(selector)
        if field:
            await field.fill("")
            await human_delay(200, 400)
            await field.type(location, delay=50)
            await human_delay(1000, 2000)

            # Wait for autocomplete dropdown and click first suggestion
            suggestion = await page.query_selector(
                ".autocomplete-suggestion, .location-suggestion, [role='option']"
            )
            if suggestion:
                await suggestion.click()
                await human_delay(300, 600)
            return
    raise RuntimeError("Could not find location search field")


async def _set_activity_filter(page: Page, days: int = 60) -> None:
    """Set the activity filter to show only candidates active in the last N days."""
    # This selector needs validation against actual StepStone HTML
    # The filter is typically a date field or dropdown
    try:
        filter_field = await page.query_selector(
            "input[name*='activity'], input[name*='seit'], select[name*='activity']"
        )
        if filter_field:
            tag = await filter_field.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await filter_field.select_option(label=f"{days} Tage")
            # For date inputs, handled differently per UI version
            await human_delay(300, 600)
    except Exception:
        pass  # Filter not found — proceed without it


async def _click_search(page: Page) -> None:
    """Click the search button."""
    for selector in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Suchen')",
        "button:has-text('Search')",
        ".search-button",
    ]:
        btn = await page.query_selector(selector)
        if btn:
            await btn.click()
            await human_delay(2000, 4000)
            return
    # Fallback: press Enter
    await page.keyboard.press("Enter")
    await human_delay(2000, 4000)


async def _get_result_count(page: Page) -> int:
    """Try to read the result count from the page."""
    for selector in [
        ".result-count",
        ".search-results-count",
        "[data-result-count]",
        ".resultcount",
    ]:
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            digits = "".join(c for c in text if c.isdigit())
            if digits:
                return int(digits)
    # Fallback: count visible candidate cards
    cards = await page.query_selector_all(
        ".candidate-card, .miniprofile, .search-result-item, tr.result"
    )
    return len(cards)


async def _scrape_preview_cards(page: Page) -> list[SearchResult]:
    """Extract candidate preview cards from the current results page."""
    results = []

    # Try multiple selector patterns for candidate cards
    cards = await page.query_selector_all(
        ".miniprofile, .candidate-card, .search-result-item, tr.result"
    )

    for card in cards:
        try:
            # Extract profile ID from link
            link = await card.query_selector("a[href*='profile'], a[href*='miniprofile']")
            profile_id = ""
            if link:
                href = await link.get_attribute("href") or ""
                # Extract numeric ID from href
                parts = href.split("/")
                for part in reversed(parts):
                    if part.isdigit():
                        profile_id = part
                        break

            # Extract preview text
            preview_text = await card.inner_text()

            if profile_id:
                results.append(SearchResult(profile_id=profile_id, preview_text=preview_text))
        except Exception:
            continue

    return results


async def search_candidates(
    page: Page,
    job_title: str,
    location: str,
) -> tuple[list[SearchResult], int | None]:
    """Search DirectSearch for candidates. Returns (results, radius_used).

    Tries increasing radius if no results found.
    Returns empty list with None radius if no candidates at any radius.
    """
    await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
    await human_delay(1000, 2000)

    for radius in RADIUS_STEPS:
        await _enter_keyword(page, job_title)
        await _enter_location(page, location)
        await _set_activity_filter(page)
        await _click_search(page)

        count = await _get_result_count(page)
        if count > 0:
            results = await _scrape_preview_cards(page)
            # TODO: handle pagination if count > 50
            return results, radius

        # Reset for next radius attempt
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
        await human_delay(1000, 2000)

    return [], None
```

- [ ] **Step 2: Verify import works**

```bash
cd D:/aramas-stepstone-scraper && python -c "from scraper.search import search_candidates, SearchResult; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scraper/search.py
git commit -m "feat: DirectSearch keyword+location search with radius fallback"
```

---

### Task 10: Profile Extraction Module

**Files:**
- Create: `scraper/profile.py`

- [ ] **Step 1: Create scraper/profile.py**

```python
# scraper/profile.py
import base64
import re
from patchright.async_api import Page
from models.candidate import CandidateResult
from utils.delays import human_delay


async def _click_candidate(page: Page, profile_id: str) -> bool:
    """Click a candidate card to open their profile dialog. Returns True if opened."""
    link = await page.query_selector(f"a[href*='{profile_id}']")
    if not link:
        return False
    await link.click()
    await human_delay(2000, 3000)
    # Wait for dialog to appear
    dialog = await page.query_selector("div.ngdialog, div[role='dialog'], .profile-dialog")
    return dialog is not None


async def _extract_name(dialog) -> str:
    """Extract candidate name from the profile dialog."""
    for selector in [".profile__name", ".candidate-name", "h2", "h3"]:
        el = await dialog.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    return ""


async def _extract_email(dialog) -> str:
    """Extract email from the profile dialog."""
    # Try data attribute first
    el = await dialog.query_selector("[data-profile-email]")
    if el:
        return (await el.get_attribute("data-profile-email")) or ""

    # Try mailto link
    mailto = await dialog.query_selector("a[href^='mailto:']")
    if mailto:
        href = await mailto.get_attribute("href") or ""
        return href.replace("mailto:", "").strip()

    # Regex fallback on text
    text = await dialog.inner_text()
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return match.group(0) if match else ""


async def _extract_phone(dialog) -> str:
    """Extract phone from the profile dialog."""
    tel = await dialog.query_selector("a[href^='tel:']")
    if tel:
        href = await tel.get_attribute("href") or ""
        return href.replace("tel:", "").strip()

    # Try onclick handler
    el = await dialog.query_selector("[onclick*='tel:']")
    if el:
        onclick = await el.get_attribute("onclick") or ""
        match = re.search(r"tel:([\d\s+\-()]+)", onclick)
        return match.group(1).strip() if match else ""

    return ""


async def _download_cv(page: Page, dialog) -> tuple[str | None, str]:
    """Download CV as base64. Returns (base64_string, filename) or (None, '')."""
    cv_link = await dialog.query_selector(
        "a[href*='downloadAttachment'], a.miniprofile__actionlink[href*='download']"
    )
    if not cv_link:
        return None, ""

    href = await cv_link.get_attribute("href") or ""
    if not href:
        return None, ""

    # Build absolute URL if needed
    if href.startswith("/"):
        href = f"https://www.stepstone.de{href}"

    try:
        response = await page.request.get(href)
        if response.ok:
            buffer = await response.body()
            b64 = base64.b64encode(buffer).decode("utf-8")
            # Try to get filename from response headers or link text
            link_text = await cv_link.inner_text()
            filename = link_text.strip() if link_text.strip() else "CV.pdf"
            return b64, filename
    except Exception:
        pass

    return None, ""


async def _close_dialog(page: Page) -> None:
    """Close the profile dialog."""
    for selector in [
        "button.ngdialog-close",
        "button[aria-label='Close']",
        "button[aria-label='Schließen']",
        ".dialog-close",
        "button:has-text('×')",
    ]:
        btn = await page.query_selector(selector)
        if btn:
            await btn.click()
            await human_delay(500, 1000)
            return
    # Fallback: press Escape
    await page.keyboard.press("Escape")
    await human_delay(500, 1000)


async def extract_profile(
    page: Page,
    profile_id: str,
    account_used: str,
) -> CandidateResult | None:
    """Click into a candidate profile, extract data, download CV, close dialog.

    Returns CandidateResult with unlocked=True if successful, or None if dialog didn't open.
    """
    if not await _click_candidate(page, profile_id):
        return None

    dialog = await page.query_selector(
        "div.ngdialog:last-of-type, div[role='dialog']:last-of-type, .profile-dialog"
    )
    if not dialog:
        return None

    try:
        name = await _extract_name(dialog)
        email = await _extract_email(dialog)
        phone = await _extract_phone(dialog)

        # Full profile text
        profile_text = ""
        try:
            profile_text = await dialog.inner_text()
        except Exception:
            pass

        # CV download
        cv_base64, cv_filename = await _download_cv(page, dialog)

        # Build clean filename if we have a name
        if cv_base64 and name:
            safe_name = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß]", "_", name)
            cv_filename = f"{safe_name}_CV.pdf"

        return CandidateResult(
            name=name,
            stepstone_profile_id=profile_id,
            email=email,
            phone=phone,
            profile_text=profile_text,
            unlocked=True,
            unlock_reason="success",
            cv_base64=cv_base64,
            cv_filename=cv_filename or "",
            account_used=account_used,
        )
    finally:
        await _close_dialog(page)
```

- [ ] **Step 2: Verify import works**

```bash
cd D:/aramas-stepstone-scraper && python -c "from scraper.profile import extract_profile; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add scraper/profile.py
git commit -m "feat: profile extraction from modal dialog with CV download"
```

---

### Task 11: Dedup Module

**Files:**
- Create: `scraper/dedup.py`

- [ ] **Step 1: Create scraper/dedup.py**

```python
# scraper/dedup.py
from utils.airtable import is_duplicate as _airtable_check


async def check_duplicate(
    pat: str,
    base_id: str,
    table_id: str,
    offer_id: str,
    profile_id: str,
) -> bool:
    """Check if this candidate was already processed for this offer.

    Returns True if duplicate (skip this candidate).
    Returns False if new (proceed with evaluation).
    On API error, returns False (process the candidate to be safe).
    """
    return await _airtable_check(
        pat=pat,
        base_id=base_id,
        table_id=table_id,
        offer_id=offer_id,
        profile_id=profile_id,
    )
```

- [ ] **Step 2: Commit**

```bash
git add scraper/dedup.py
git commit -m "feat: dedup module wrapping Airtable client"
```

---

### Task 12: FastAPI Application — The Orchestrator

**Files:**
- Create: `main.py`

- [ ] **Step 1: Create main.py**

```python
# main.py
import asyncio
import logging
import os
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException

from models.candidate import CandidateResult, ScrapeResult
from models.config import Settings
from models.job import JobInput
from scraper.auth import AuthenticationError, authenticate
from scraper.browser import close_browser, create_browser
from scraper.dedup import check_duplicate
from scraper.profile import extract_profile
from scraper.rotation import next_account
from scraper.search import search_candidates
from utils.delays import human_delay
from utils.openrouter import evaluate_candidate
from utils.webhook import send_webhook

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

settings = Settings()
scrape_lock = asyncio.Lock()
current_status: dict = {"state": "idle", "job": None, "error": None}

COUNTER_PATH = os.path.join("state", "account_counter.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("state", exist_ok=True)
    os.makedirs("screenshots", exist_ok=True)
    yield


app = FastAPI(title="StepStone Scraper", lifespan=lifespan)


async def run_scrape(job: JobInput) -> None:
    """Main scrape orchestrator. Runs as a background task."""
    global current_status
    current_status = {"state": "running", "job": job.model_dump(), "error": None}
    accounts = settings.get_accounts()
    account = next_account(accounts, COUNTER_PATH)
    account_label = f"Account {accounts.index(account) + 1}"

    result = ScrapeResult(
        offer_id=job.offer_id,
        stage_id=job.stage_id,
        job_title=job.job_title,
        location=job.location,
        requirements=job.requirements,
        account_used=account_label,
    )

    browser = None
    try:
        # 1. Launch browser
        logger.info(f"Launching browser for {job.job_title} in {job.location}")
        browser, context, page = await create_browser(
            proxy_host=settings.proxy_host,
            proxy_port=settings.proxy_port,
            proxy_user=settings.proxy_user,
            proxy_pass=settings.proxy_pass,
            proxy_country=settings.proxy_country,
        )

        # 2. Authenticate
        logger.info(f"Authenticating as {account['email']}")
        try:
            captcha_solver = None
            if settings.twocaptcha_api_key:
                from twocaptcha import TwoCaptcha
                captcha_solver = TwoCaptcha(settings.twocaptcha_api_key)

            await authenticate(context, page, account["email"], account["password"], captcha_solver)
            logger.info("Authentication successful")
        except AuthenticationError as e:
            logger.error(f"Auth failed for {account['email']}: {e}")
            # Try next account
            for alt in accounts:
                if alt["email"] != account["email"]:
                    try:
                        await authenticate(context, page, alt["email"], alt["password"], captcha_solver)
                        account_label = f"Account {accounts.index(alt) + 1}"
                        result.account_used = account_label
                        logger.info(f"Authenticated with fallback account {alt['email']}")
                        break
                    except AuthenticationError:
                        continue
            else:
                raise AuthenticationError("All accounts failed to authenticate")

        # 3. Search
        logger.info(f"Searching: {job.job_title} in {job.location}")
        candidates, radius = await search_candidates(page, job.job_title, job.location)
        logger.info(f"Found {len(candidates)} candidates (radius: {radius}km)")

        if not candidates:
            logger.info("No candidates found at any radius")
            await send_webhook(settings.n8n_webhook_url, result)
            current_status = {"state": "idle", "job": None, "error": None}
            return

        # 4. Process each candidate
        processed = 0
        for candidate in candidates:
            if processed >= job.max_candidates:
                break

            # 4a. Dedup check
            is_dup = await check_duplicate(
                pat=settings.airtable_pat,
                base_id=settings.airtable_base_id,
                table_id=settings.airtable_candidates_table,
                offer_id=job.offer_id,
                profile_id=candidate.profile_id,
            )
            if is_dup:
                logger.info(f"Skipping duplicate: {candidate.profile_id}")
                continue

            # 4b. Evaluate with Claude
            eval_result = await evaluate_candidate(
                api_key=settings.openrouter_api_key,
                candidate_text=candidate.preview_text,
                job_title=job.job_title,
                location=job.location,
                requirements=job.requirements,
            )
            await asyncio.sleep(1.0)  # Rate limit: 1 eval/sec

            if not eval_result.match:
                result.candidates.append(
                    CandidateResult(
                        name="",
                        stepstone_profile_id=candidate.profile_id,
                        matched=False,
                        match_confidence=eval_result.confidence,
                        match_reasoning=eval_result.reasoning,
                        account_used=account_label,
                    )
                )
                processed += 1
                continue

            # 4c. Unlock + extract profile
            logger.info(f"Match! Extracting profile {candidate.profile_id}")
            profile = await extract_profile(page, candidate.profile_id, account_label)
            if profile:
                profile.matched = True
                profile.match_confidence = eval_result.confidence
                profile.match_reasoning = eval_result.reasoning
                result.candidates.append(profile)
            else:
                result.candidates.append(
                    CandidateResult(
                        name="",
                        stepstone_profile_id=candidate.profile_id,
                        matched=True,
                        match_confidence=eval_result.confidence,
                        match_reasoning=eval_result.reasoning,
                        unlocked=False,
                        unlock_reason="profile_extraction_failed",
                        account_used=account_label,
                    )
                )

            processed += 1
            await human_delay(1000, 3000)

        # 5. Send results to n8n
        logger.info(f"Sending {len(result.candidates)} candidates to webhook")
        await send_webhook(settings.n8n_webhook_url, result)

    except Exception as e:
        logger.error(f"Scrape error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        result.partial = True
        current_status["error"] = str(e)
        try:
            await send_webhook(settings.n8n_webhook_url, result)
        except Exception:
            pass
    finally:
        if browser:
            await close_browser(browser)
        current_status = {
            "state": "idle",
            "job": None,
            "error": current_status.get("error"),
        }


@app.post("/scrape", status_code=202)
async def scrape(job: JobInput, background_tasks: BackgroundTasks):
    if scrape_lock.locked():
        raise HTTPException(409, detail="Scrape already in progress")

    async def locked_scrape():
        async with scrape_lock:
            await run_scrape(job)

    background_tasks.add_task(locked_scrape)
    return {"status": "accepted", "job_title": job.job_title, "location": job.location}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return current_status
```

- [ ] **Step 2: Verify import and startup**

```bash
cd D:/aramas-stepstone-scraper && python -c "from main import app; print('FastAPI app created:', app.title)"
```

Expected: `FastAPI app created: StepStone Scraper`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: FastAPI orchestrator with background scrape, account rotation, full pipeline"
```

---

### Task 13: Dockerfile + Docker Compose

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN patchright install chromium

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create docker-compose.yml**

```yaml
services:
  scraper:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
      - ./state:/app/state
      - ./screenshots:/app/screenshots
    restart: unless-stopped
```

- [ ] **Step 3: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "chore: Dockerfile and docker-compose for Hetzner deployment"
```

---

### Task 14: Run All Unit Tests

- [ ] **Step 1: Run full test suite**

```bash
cd D:/aramas-stepstone-scraper && python -m pytest tests/ -v
```

Expected: All tests pass (test_models: 5, test_delays: 2, test_rotation: 3, test_openrouter: 3, test_airtable: 3, test_webhook: 2 = **18 total**)

- [ ] **Step 2: Fix any failures, then commit**

```bash
git add -A
git commit -m "chore: all 18 unit tests passing"
```

---

### Task 15: Integration Test — Step 0 (Proxy Verification)

**Files:**
- Create: `step0_proxy_verify.py` (in project root, gitignored)

This test verifies: Patchright launches, IPRoyal proxy works, we get a German IP.

**Prerequisites:** `.env` file with real proxy credentials.

- [ ] **Step 1: Create step0_proxy_verify.py**

```python
"""Step 0: Verify Patchright + IPRoyal proxy gives us a German IP."""
import asyncio
import json
from patchright.async_api import async_playwright


async def main():
    print("=" * 60)
    print("STEP 0: Patchright + IPRoyal proxy verification")
    print("=" * 60)

    from dotenv import load_dotenv
    import os
    load_dotenv()

    proxy_host = os.getenv("PROXY_HOST")
    proxy_port = os.getenv("PROXY_PORT")
    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")
    proxy_country = os.getenv("PROXY_COUNTRY", "DE")

    async with async_playwright() as p:
        print("[+] Launching Patchright Chromium with proxy...")
        import uuid
        session_id = uuid.uuid4().hex[:12]

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            proxy={
                "server": f"http://{proxy_host}:{proxy_port}",
                "username": f"{proxy_user}_country-{proxy_country}_session-{session_id}",
                "password": proxy_pass,
            },
        )
        print(f"[+] Browser version: {browser.version}")

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        page = await context.new_page()

        print("[+] Navigating to ipinfo.io...")
        await page.goto("https://ipinfo.io/json", wait_until="domcontentloaded")
        body = await page.inner_text("body")
        data = json.loads(body)

        print(f"[+] IP:      {data.get('ip')}")
        print(f"[+] Country: {data.get('country')}")
        print(f"[+] City:    {data.get('city')}")
        print(f"[+] Org:     {data.get('org')}")

        is_german = data.get("country") == "DE"

        await page.screenshot(path="step0_proxy_result.png")
        print("[+] Screenshot: step0_proxy_result.png")

        print()
        if is_german:
            print("[SUCCESS] German IP confirmed via IPRoyal proxy")
        else:
            print(f"[FAIL] Expected country=DE, got country={data.get('country')}")

        await browser.close()
        print("[+] Browser closed")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run Step 0 (requires real .env)**

```bash
cd D:/aramas-stepstone-scraper && python step0_proxy_verify.py
```

Expected: `[SUCCESS] German IP confirmed via IPRoyal proxy`

- [ ] **Step 3: Report result to user**

If SUCCESS: proceed to Task 16. If FAIL: diagnose using the error and proxy config.

---

### Task 16: Integration Test — Step 0.5 (Login + DirectSearch)

**Files:**
- Create: `step05_login_verify.py` (in project root, gitignored)

This test verifies: Direct login to StepStone works through Patchright + proxy, DirectSearch is accessible.

- [ ] **Step 1: Create step05_login_verify.py**

```python
"""Step 0.5: Verify StepStone login + DirectSearch access."""
import asyncio
from dotenv import load_dotenv
from models.config import Settings
from scraper.browser import create_browser, close_browser
from scraper.auth import authenticate, AuthenticationError

load_dotenv()
settings = Settings()

DIRECTSEARCH_URL = "https://www.stepstone.de/5/index.cfm?event=directsearchgen4:searchprofiles"


async def main():
    print("=" * 60)
    print("STEP 0.5: StepStone login + DirectSearch verification")
    print("=" * 60)

    accounts = settings.get_accounts()
    account = accounts[0]  # Use first account
    print(f"[+] Using account: {account['email']}")

    browser, context, page = await create_browser(
        proxy_host=settings.proxy_host,
        proxy_port=settings.proxy_port,
        proxy_user=settings.proxy_user,
        proxy_pass=settings.proxy_pass,
        proxy_country=settings.proxy_country,
    )

    try:
        print("[+] Authenticating...")
        await authenticate(context, page, account["email"], account["password"])
        print("[+] Authentication successful!")

        print(f"[+] Navigating to DirectSearch...")
        await page.goto(DIRECTSEARCH_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        final_url = page.url
        title = await page.title()
        print(f"[+] URL:   {final_url}")
        print(f"[+] Title: {title}")

        has_directsearch = "directsearch" in final_url.lower()
        is_login = any(k in final_url.lower() for k in ["login", "anmelden"])

        search_bar = await page.query_selector(
            "input[name='searchtext'], input[placeholder*='Jobtitel']"
        )

        print()
        print("=" * 60)
        print("DIAGNOSTICS")
        print("=" * 60)
        print(f"Has directsearch in URL: {has_directsearch}")
        print(f"Is login page:           {is_login}")
        print(f"Search bar found:        {search_bar is not None}")
        print()

        if has_directsearch and not is_login and search_bar:
            print("[SUCCESS] Logged in and on DirectSearch with search bar visible")
        elif is_login:
            print("[FAIL] Redirected to login — authentication did not persist")
        else:
            print("[PARTIAL] On DirectSearch but search bar not found")

        await page.screenshot(path="step05_result.png", full_page=True)
        print("[+] Screenshot: step05_result.png")

        body = await page.inner_text("body")
        with open("step05_body_text.txt", "w") as f:
            f.write(body)
        print(f"[+] Body text: step05_body_text.txt ({len(body)} chars)")

    except AuthenticationError as e:
        print(f"[FAIL] {e}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
    finally:
        await close_browser(browser)
        print("[+] Browser closed")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run Step 0.5 (requires real .env + StepStone credentials)**

```bash
cd D:/aramas-stepstone-scraper && python step05_login_verify.py
```

Expected: `[SUCCESS] Logged in and on DirectSearch with search bar visible`

- [ ] **Step 3: Report result to user**

If SUCCESS: scraper infrastructure is verified end-to-end. Ready for production use.
If FAIL: inspect screenshot + body text, adjust selectors in auth.py, re-run.

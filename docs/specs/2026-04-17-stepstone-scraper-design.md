# StepStone DirectSearch Scraper — Revised Design Spec

**Date:** 2026-04-17
**Author:** Utkarsh (APEX Consulting)
**Client:** Aramaz Digital
**Status:** Architecture finalized, ready for implementation planning

---

## 1. Purpose

Automated candidate sourcing pipeline for Aramaz Digital. A Python FastAPI service on a Hetzner VPS that:

1. Receives job parameters from n8n Workflow A
2. Launches a stealth browser with residential proxy
3. Logs into StepStone DirectSearch (direct login, production-grade)
4. Searches for candidates matching the job
5. Evaluates each candidate with Claude Haiku 4.5 via OpenRouter
6. Unlocks matching candidates, scrapes full profiles, downloads CVs
7. POSTs results to n8n Workflow B webhook

## 2. Architecture Decisions

### Why self-hosted (not managed browser service)

- **Bright Data:** AUP prohibits login-gated data collection. Additionally, their Scraping Browser locks the entire CDP cookie store (no create/update/delete) and enforces robots.txt — both confirmed blockers from Step 0 testing (2026-04-09).
- **Browserless.io:** $200/month minimum, vendor dependency.
- **Self-hosted:** Full control, no AUP restrictions, cheapest option (~€20-55/month total).

### Why Patchright (not playwright-stealth)

StepStone uses Akamai Bot Manager (`_abck` cookies confirmed in cookie exports). Akamai's primary detection vector in 2026 is TLS fingerprinting (JA3/JA4), not browser JS leaks. `playwright-stealth` only patches JS-level tells (navigator.webdriver, plugins, WebGL) — insufficient for Akamai.

**Patchright** (`patchright` on PyPI, 1,299 GitHub stars, actively maintained as of 2026-04-10) is a drop-in Playwright replacement that patches CDP communication at the binary level. It prevents detection of: `navigator.webdriver` leaks, `Runtime.enable` domain leaks, and other CDP fingerprints. Combined with IPRoyal residential proxy for IP reputation, this is the minimum viable stealth stack for Akamai.

### Why Hetzner (not Railway)

Railway retired fixed pricing in 2024. Usage-based billing with a Playwright container (~700MB RAM) costs $12-18/month always-on, with 15-30s cold starts on scale-to-zero. Hetzner CX22 provides 2 vCPU, 4GB RAM, 40GB disk for €4.51/month fixed — no cold starts, no request timeouts, no usage surprises.

## 3. Component Stack

| Component | Technology | Purpose |
|---|---|---|
| Language | Python 3.11+ | Primary scraper code |
| Browser automation | Patchright (async) | Stealth browser control (drop-in Playwright replacement) |
| Proxy | IPRoyal Residential (Germany) | IP masking, geo-correct, sticky sessions |
| CAPTCHA solver | 2captcha | Fallback for login CAPTCHA challenges |
| AI evaluation | Claude Haiku 4.5 via OpenRouter | Candidate matching |
| Web framework | FastAPI + uvicorn | HTTP endpoint for n8n triggers |
| Hosting | Hetzner CX22 VPS | Docker container, always-on |
| Orchestrator | n8n (existing) | Job dispatch + candidate ingestion |
| Data layer | Airtable (existing) | Dedup + credit ledger, visible to Umair |

## 4. Cost Breakdown

| Item | Monthly Cost | Notes |
|---|---|---|
| IPRoyal Residential (Germany) | $30-50 | ~200-300MB/day at $7/GB for 100-200 profiles |
| Hetzner CX22 | €4.51 (~$5) | 2 vCPU, 4GB RAM, 40GB disk |
| 2captcha | $0-3 | Rare for logged-in accounts |
| OpenRouter (Claude Haiku 4.5) | $2-5 | ~3000 evaluations/month |
| **Total** | **~$37-63/month** | Paid by APEX, not client |

## 5. Python Dependencies

```
# requirements.txt
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
patchright>=1.58.0
httpx>=0.28.0
python-dotenv>=1.0.1
pydantic>=2.10.0
python-multipart>=0.0.9
2captcha-python>=1.0.3
```

### Dependency notes

- **patchright**: Drop-in Playwright replacement. API-identical — `from patchright.async_api import async_playwright`. Ships its own patched Chromium via `patchright install chromium`.
- **2captcha-python**: Official 2captcha SDK (not `twocaptcha-python` which is an unofficial alias).
- **python-multipart**: Required by FastAPI for form handling — runtime crash without it.
- **playwright-stealth**: Intentionally excluded. Patchright handles stealth at binary/CDP level.

## 6. Environment Variables

```bash
# .env.example

# Proxy
PROXY_HOST=geo.iproyal.com
PROXY_PORT=12321
PROXY_USER=<iproyal_username>
PROXY_PASS=<iproyal_password>
PROXY_COUNTRY=DE

# StepStone Accounts (round-robin rotation)
STEPSTONE_EMAIL_1=ba@aramaz-digital.de
STEPSTONE_PASS_1=<password>
STEPSTONE_EMAIL_2=mj@aramaz-digital.de
STEPSTONE_PASS_2=<password>

# OpenRouter (Claude Haiku 4.5 for evaluation)
OPENROUTER_API_KEY=<key>

# Airtable
AIRTABLE_PAT=<pat>
AIRTABLE_BASE_ID=<base_id>
AIRTABLE_CANDIDATES_TABLE=<table_id>
AIRTABLE_CREDIT_TABLE=<table_id>

# n8n
N8N_WEBHOOK_URL=https://aramazdigital.app.n8n.cloud/webhook/stepstone-results

# Recruitee
RECRUITEE_API_KEY=<key>
RECRUITEE_COMPANY_ID=61932

# 2captcha (fallback)
TWOCAPTCHA_API_KEY=<key>

# App
SCRAPE_TIMEOUT_SECONDS=1200
MAX_CANDIDATES_PER_JOB=50
```

## 7. File Structure

```
aramas-stepstone-scraper/
├── main.py                    # FastAPI app (3 endpoints: /scrape, /health, /status)
├── scraper/
│   ├── __init__.py
│   ├── browser.py             # Patchright launch + IPRoyal proxy + German locale
│   ├── auth.py                # Direct login + session persistence + CAPTCHA handling
│   ├── search.py              # DirectSearch: keyword + location + radius fallback + pagination
│   ├── evaluate.py            # Claude Haiku 4.5 candidate evaluation via OpenRouter
│   ├── profile.py             # Modal dialog extraction + CV download as base64
│   ├── dedup.py               # Airtable duplicate check
│   └── rotation.py            # Account round-robin (disk-persisted counter)
├── models/
│   ├── __init__.py
│   ├── job.py                 # Pydantic input schema (from n8n)
│   ├── candidate.py           # Pydantic output schema (to n8n)
│   └── config.py              # Settings via pydantic-settings + .env
├── utils/
│   ├── __init__.py
│   ├── airtable.py            # Airtable REST client (dedup + credit ledger)
│   ├── openrouter.py          # OpenRouter API client (Claude evals)
│   ├── webhook.py             # n8n webhook POST sender
│   └── delays.py              # Human-like random delay helpers
├── sessions/                  # Saved login sessions per account (gitignored)
├── state/                     # Account counter, job status (gitignored)
├── screenshots/               # Debug screenshots on failure (gitignored)
├── docs/
│   └── specs/                 # This spec and future docs
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── .gitignore
```

## 8. Module Specs

### 8.1 browser.py — Browser Launch

Launch Patchright Chromium with IPRoyal proxy, German locale/timezone, realistic viewport.

```python
async def create_browser(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        proxy={
            "server": f"http://{PROXY_HOST}:{PROXY_PORT}",
            "username": f"{PROXY_USER}_country-{PROXY_COUNTRY}_session-{session_id}",
            "password": PROXY_PASS,
        },
    )
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
        user_agent="<match Patchright's Chromium version>",
    )
    page = await context.new_page()
    return browser, context, page
```

**Key details:**
- IPRoyal sticky session via `_session-{uuid}` suffix on username — same IP for entire scrape job
- `--no-sandbox` required for Docker (runs as root)
- Patchright applies stealth patches automatically at CDP level — no `stealth_async()` call needed
- User agent must match Patchright's bundled Chromium version

### 8.2 auth.py — Authentication

Direct login as production-grade auth method. Session persistence to avoid re-login on every run.

**Flow:**
1. Check for saved session file (`sessions/<account>.json`)
2. If exists: load cookies, navigate to DirectSearch, verify not redirected to login
3. If no session or session expired: execute login flow
4. Login: navigate to login URL, dismiss cookie banner, fill email + password with human-like delays, submit
5. If CAPTCHA detected: send to 2captcha solver, inject solution
6. Verify login succeeded (not still on login page)
7. Save session cookies to disk for reuse

**Session persistence:** Cookies saved after each successful login. Sessions last 10-24 hours based on PHRECRUITERAUTHCOOKIE JWT expiry. On next run, cookies are loaded and verified before attempting fresh login.

### 8.3 rotation.py — Account Rotation

Round-robin across configured accounts. Counter persisted to `state/account_counter.json`.

```python
ACCOUNTS = [
    {"email": env("STEPSTONE_EMAIL_1"), "password": env("STEPSTONE_PASS_1")},
    {"email": env("STEPSTONE_EMAIL_2"), "password": env("STEPSTONE_PASS_2")},
]

def next_account():
    counter = read_counter()  # from state/account_counter.json, defaults to 0
    account = ACCOUNTS[counter % len(ACCOUNTS)]
    write_counter(counter + 1)
    return account
```

Each `/scrape` request picks the next account. If an account fails login, skip to next. Webhook response includes `account_used` for tracking.

### 8.4 search.py — DirectSearch

**Flow:**
1. Navigate to DirectSearch URL
2. Enter job title in keyword field
3. Enter location using structured autocomplete (wait for dropdown, select matching city)
4. Set activity filter to 60 days
5. Sort by "letzte Aktivität" (last activity)
6. If 0 results: expand radius (25 → 50 → 75 → 100km)
7. Paginate if >50 results
8. Return list of candidate preview cards with StepStone profile IDs

**Location handling:** Use StepStone's structured location autocomplete — type city name, wait for dropdown suggestions, click the correct match. This prevents the "Rostock for Dortmund" bug from free-text entry.

### 8.5 evaluate.py — Claude Candidate Evaluation

Input: candidate preview text (visible without unlocking).
Output: `{"match": bool, "confidence": 0.0-1.0, "reasoning": "..."}`.

```python
MODEL = "anthropic/claude-haiku-4-5"
MAX_TOKENS = 300
RATE_LIMIT_SECONDS = 1.0  # between evals
TIMEOUT_SECONDS = 30
```

**Evaluation rules (in prompt):**
- Match candidate's current/recent job title against target
- Consider location proximity
- Do NOT penalize short tenures (probation = open to new opportunities)
- Focus on role alignment, not exact keywords
- When in doubt, lean toward MATCH (human recruiter reviews later)

Rate limit: 1 eval/second. Timeout: 30s per eval, skip on timeout.

### 8.6 profile.py — Profile Extraction + CV Download

Profile opens in a modal dialog (`div.ngdialog`), not a new page.

**Extract:**
- Full name (`.profile__name`)
- Email (from `data-profile-email` or `mailto:` link)
- Phone (from `tel:` link or onclick handler)
- Address/city (from Wohnadresse list item)
- Full profile text (body innerText within dialog)
- CV as base64 (via `page.request.get()` on attachment URL)
- StepStone profile ID (from miniprofile link href)

Close dialog before moving to next candidate. Handle missing CVs gracefully.

### 8.7 dedup.py — Duplicate Detection

Before evaluating each candidate, check Airtable:

```
GET /v0/{BASE_ID}/{TABLE}?filterByFormula=AND({Offer ID}="X",{StepStone Profile ID}="Y")&maxRecords=1
```

If match found: skip candidate entirely. Built-in 200ms delay between checks (Airtable rate limit: 5 req/sec). Retry with backoff on 429.

### 8.8 main.py — FastAPI Application

**Endpoints:**
- `POST /scrape` — Accept job params, return 202 immediately, run scrape in background
- `GET /health` — Health check for monitoring
- `GET /status` — Current scrape state (idle/running/error + current job info)

**Concurrency:** `asyncio.Lock` — one scrape at a time. Returns 409 if busy.

## 9. Error Handling

| Scenario | Behavior |
|---|---|
| Login fails for selected account | Log error, try next account in rotation. If ALL fail, abort + POST error to webhook |
| Session expires mid-scrape | Detect login redirect, re-authenticate same account, resume |
| CAPTCHA during login | Send to 2captcha solver, inject solution, continue |
| 0 search results at 100km | POST empty result set to n8n (`candidates_scraped: 0`) |
| Airtable rate limit (429) | Retry with exponential backoff, max 3 retries |
| Claude eval timeout/error | Skip candidate (`eval_failed`), continue to next |
| Browser crash mid-scrape | Catch exception, POST partial results with `partial: true` |
| Proxy connection failure | Retry browser launch 3x with 10s delay, then abort |

**Timeouts:**
- Browser launch: 60s
- Page navigation: 120s
- Claude eval per candidate: 30s
- Total job: 1200s (configurable via `SCRAPE_TIMEOUT_SECONDS`)

## 10. Webhook Payload Schema (to n8n Workflow B)

Same schema as current Axiom webhook — Workflow B doesn't need changes.

```json
{
  "offer_id": "2525450",
  "stage_id": "12951264",
  "job_title": "Bürofachkraft",
  "location": "Halle",
  "requirements": "...",
  "account_used": "Account 1",
  "candidates_scraped": 10,
  "candidates_matched": 8,
  "candidates_unlocked": 5,
  "partial": false,
  "candidates": [
    {
      "name": "Jasmin Ciesla",
      "email": "ciesla@web.de",
      "phone": "+49-015755888840",
      "profile_text": "full scraped text...",
      "match_confidence": 0.85,
      "match_reasoning": "Strong candidate match...",
      "matched": true,
      "unlocked": true,
      "unlock_reason": "success",
      "cv_base64": "<base64_encoded_pdf>",
      "cv_filename": "Jasmin_Ciesla_CV.pdf",
      "stepstone_profile_id": "12345678",
      "account_used": "Account 1"
    }
  ]
}
```

## 11. Deployment

### Dockerfile

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

### docker-compose.yml

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

### Deploy to Hetzner

SSH in, clone repo, `docker compose up -d --build`. Sessions and state persist via volume mounts.

## 12. n8n Changes Required

| Workflow | Node | Change |
|---|---|---|
| A — Dispatch First Job | Trigger node | POST to `https://<hetzner-ip>:8000/scrape` instead of Axiom |
| B — Dispatch Next Job | Trigger node | Same endpoint change |
| B — Attach CV | Upload node | Multipart upload using `cv_base64` instead of Google Drive URL |
| B — Google Drive nodes | Delete | Remove CV upload to Drive (CVs now inline as base64) |

## 13. Testing Plan

| Step | What | Pass criteria |
|---|---|---|
| 0 | Patchright + IPRoyal proxy | Navigate to ipinfo.io, verify German IP |
| 0.5 | StepStone login + DirectSearch | Direct login succeeds, search bar visible |
| 1 | Single candidate scrape | Search + eval 3 candidates + extract 1 profile + CV |
| 2 | Full job scrape | Complete job with dedup + eval + unlock + webhook POST |
| 3 | Multi-job chain | 4 pilot jobs, account rotation, Airtable records |

## 14. Risk Register

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Akamai detects Patchright | 15% | High | Residential proxy + human-like delays + session persistence |
| IPRoyal IP blocked by StepStone | 10% | Medium | Switch IP (sticky session rotation), large German pool |
| Login flow changes (StepStone redesign) | 15% | Medium | Selector-based — update selectors when detected |
| CAPTCHA frequency increases | 10% | Medium | 2captcha fallback built in |
| Account locked/suspended | 5% | High | Account rotation spreads risk, alert on consecutive failures |
| Hetzner outage | 5% | Medium | Stateless — redeploy to any VPS in minutes |
| OpenRouter rate limit | 10% | Low | 1 eval/sec rate limit + retry with backoff |
| Cookie/session expiry mid-scrape | 5% | Low | Auto-detect login redirect, re-authenticate, resume |

## 15. Audit Trail

### What changed from the original plan (and why)

| # | Original | Revised | Reason |
|---|---|---|---|
| 1 | playwright-stealth 1.0.6 | Patchright >=1.58.0 | playwright-stealth is JS-only; Akamai uses TLS fingerprinting |
| 2 | playwright 1.45.0 | patchright 1.58.0 | 1.45.0 is 2 years old; old Chromium = flaky on modern sites |
| 3 | Railway $5/mo | Hetzner CX22 €4.51/mo | Railway is usage-based ($12-18/mo real cost), cold starts |
| 4 | claude-3-haiku | claude-haiku-4-5 | Old model deprecated |
| 5 | Cookie injection (Mode A primary) | Direct login only | Cookie injection impossible on Bright Data; self-hosted works but direct login is simpler ops |
| 6 | twocaptcha-python | 2captcha-python | Correct official PyPI package name |
| 7 | No python-multipart | Added | Required by FastAPI, runtime crash without it |
| 8 | $5-15/mo proxy budget | $30-50/mo | Realistic for 100-200 profiles/day with CVs |
| 9 | No account rotation | Round-robin rotation | Reduces per-account load, detection risk |
| 10 | Docker v1.45.0-jammy | v1.58.0-jammy | Must match pip patchright version |

### Known unknowns (to resolve during implementation)

1. **StepStone login selectors** — pseudocode guesses `input[name='login']`, `button[type='submit']`. Must validate against actual HTML in Step 0.5.
2. **DirectSearch search field selectors** — need to be identified from actual UI.
3. **Profile dialog selectors** — `div.ngdialog`, `.profile__name`, CV download link pattern all need validation.
4. **Patchright + IPRoyal compatibility** — untested combination. Step 0 will verify.
5. **Account 2 password** — pending from Umair.
6. **Airtable credit ledger table ID** — placeholder in env vars, needs actual ID.

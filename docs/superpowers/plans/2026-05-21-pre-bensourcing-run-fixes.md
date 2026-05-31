# Pre-#BenSourcing Run Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two remaining feature gaps (job-specific keywords, route all post-unlock rejects to the dedicated rejected pipeline) and add two hard credit-safety controls before the 2026-06-02 #BenSourcing run (10 jobs, ≤100 unlocks).

**Architecture:** Keywords arrive via the same path as the radius tag — Umair tags the Recruitee offer → n8n forwards it in the `/scrape` JobInput payload → the scraper adds each keyword as a structured STICHWORT criterion in the StepStone autosuggest. Rejected-after-unlock candidates are routed to Recruitee offer `2592624` ("Rejected Candidates - StepStone DirectSearch"), which is already the configured talent-pool offer. Credit safety is enforced by (a) wiring the dead `settings.max_candidates_per_job` as a per-job ceiling and (b) a new persistent daily global unlock counter backed by a JSON state file.

**Tech Stack:** Python 3.11, FastAPI, Patchright (Playwright fork), pydantic-settings, pytest + respx.

---

## Context the implementer needs

- **Credits are spent at exactly one place:** the `link.click()` inside `scraper/profile.py::_click_candidate`, called only from `extract_profile()` in `main.py`. Everything before it (search, scrape, CV/distance/LLM/Airtable gates) is free. Search submissions cost **zero** credits.
- **The unlock happens at `main.py` line ~363** (`profile = await extract_profile(...)`), BEFORE the post-unlock gates. A non-None return means the unlock succeeded = one credit spent.
- **`processed += 1`** at the end of the `for candidate in candidates:` loop (line ~616) bounds the loop via `if processed >= job.max_candidates: break` (line ~267). Airtable dups `continue` WITHOUT incrementing `processed`.
- **Rejected pipeline** = offer `2592624`, stage `13166770` ("Gesourct"). Already in `.env.example` as `RECRUITEE_TALENT_POOL_OFFER_ID` / `RECRUITEE_TALENT_POOL_STAGE_ID`.
- **Post-unlock rejection branches** in `main.py` (all set `profile.unlocked=True, profile.matched=False`):
  - `already_in_recruitee` (~439): already in Recruitee → MUST NOT push (would duplicate).
  - `location_unknown` truly-unknown (~502): already pushes to pool (reason "Standort Unklar").
  - `location_unknown` foreign-address (~489): currently NO push — CHANGE to push.
  - `too_far_for_relocation_post_unlock` (~557): currently NO push — CHANGE to push.
  - `too_far_post_unlock` (~565): currently NO push — CHANGE to push.
- **`_push_to_recruitee` refuses when `cv_base64` is missing** (the CV gate). All push-to-reject calls MUST happen BEFORE the branch sets `profile.cv_base64 = None`.
- **Run tests with:** `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/ -v`
- **conftest.py** sets env-var stubs so `main.py` imports in tests.

---

## File Structure

- `models/job.py` — add `keywords` field to `JobInput` (Task 3)
- `models/config.py` — add `max_unlocks_per_day` setting (Task 2)
- `scraper/search.py` — add `_add_keyword_criterion`, wire keywords into `search_candidates` (Task 3)
- `utils/unlock_budget.py` — NEW: persistent daily unlock counter (Task 2)
- `main.py` — wire per-job ceiling (Task 1), unlock-budget check+increment (Task 2), keyword pass-through (Task 3), reject routing (Task 4)
- `tests/test_unlock_budget.py` — NEW (Task 2)
- `tests/test_models.py` — keyword field tests (Task 3)
- `tests/test_main_push.py` — reject-routing tests (Task 4)
- `.env.example` — document new vars (Tasks 2, 4)

---

## Task 1: Wire the dead `max_candidates_per_job` ceiling

**Problem:** `settings.max_candidates_per_job` (default 50) exists but the loop only reads `job.max_candidates`. Setting the Railway env does nothing. We need the env to act as a hard ceiling so "max 10/job" is enforceable centrally.

**Files:**
- Modify: `main.py` (the `for candidate` loop guard, ~line 267)

- [ ] **Step 1: Find the loop guard**

Current (`main.py` ~263-267):
```python
        # 4. Process each candidate
        processed = 0
        for candidate in candidates:
            if processed >= job.max_candidates:
                break
```

- [ ] **Step 2: Replace with min() ceiling**

```python
        # 4. Process each candidate.
        # Effective per-job cap = min(what n8n requested, the server ceiling).
        # settings.max_candidates_per_job is the central kill-switch for credit
        # control — set MAX_CANDIDATES_PER_JOB=10 on Railway for the BenSourcing run.
        effective_max_candidates = min(job.max_candidates, settings.max_candidates_per_job)
        logger.info(
            f"Per-job candidate ceiling: {effective_max_candidates} "
            f"(job requested {job.max_candidates}, server cap {settings.max_candidates_per_job})"
        )
        processed = 0
        for candidate in candidates:
            if processed >= effective_max_candidates:
                logger.info(f"Reached per-job cap ({effective_max_candidates}); stopping this job.")
                break
```

- [ ] **Step 3: Verify import compiles**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "fix: enforce max_candidates_per_job as central per-job ceiling"
```

---

## Task 2: Persistent daily global unlock cap

**Problem:** There is no cross-job unlock counter. "Max 100 total" relies on 10 jobs × 10/job and breaks if n8n sends more jobs or a higher per-job cap. We need a true hard stop that survives process restarts and resets daily.

**Files:**
- Create: `utils/unlock_budget.py`
- Create: `tests/test_unlock_budget.py`
- Modify: `models/config.py` (add `max_unlocks_per_day`)
- Modify: `main.py` (check before unlock, increment after)
- Modify: `.env.example`

- [ ] **Step 1: Write the failing test**

Create `tests/test_unlock_budget.py`:
```python
import json
from pathlib import Path
import utils.unlock_budget as ub


def _state_path(tmp_path) -> str:
    return str(tmp_path / "unlock_counter.json")


def test_starts_at_zero_when_no_file(tmp_path):
    p = _state_path(tmp_path)
    assert ub.unlocks_today(p, today="2026-06-02") == 0


def test_increment_persists(tmp_path):
    p = _state_path(tmp_path)
    ub.record_unlock(p, today="2026-06-02")
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 2
    # New process reads the same count from disk
    assert ub.unlocks_today(p, today="2026-06-02") == 2


def test_resets_on_new_day(tmp_path):
    p = _state_path(tmp_path)
    ub.record_unlock(p, today="2026-06-02")
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 2
    # Next day → counter resets
    assert ub.unlocks_today(p, today="2026-06-03") == 0
    ub.record_unlock(p, today="2026-06-03")
    assert ub.unlocks_today(p, today="2026-06-03") == 1


def test_budget_remaining(tmp_path):
    p = _state_path(tmp_path)
    for _ in range(3):
        ub.record_unlock(p, today="2026-06-02")
    assert ub.budget_remaining(p, cap=100, today="2026-06-02") == 97


def test_budget_remaining_never_negative(tmp_path):
    p = _state_path(tmp_path)
    for _ in range(5):
        ub.record_unlock(p, today="2026-06-02")
    assert ub.budget_remaining(p, cap=3, today="2026-06-02") == 0


def test_corrupt_file_treated_as_zero(tmp_path):
    p = _state_path(tmp_path)
    Path(p).write_text("not json{")
    assert ub.unlocks_today(p, today="2026-06-02") == 0
    # And a subsequent record overwrites cleanly
    ub.record_unlock(p, today="2026-06-02")
    assert ub.unlocks_today(p, today="2026-06-02") == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/test_unlock_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.unlock_budget'`

- [ ] **Step 3: Implement `utils/unlock_budget.py`**

```python
"""Persistent daily unlock budget.

Each successful StepStone profile unlock costs one credit. To enforce a hard
ceiling across all jobs in a day (n8n sends each job as a separate /scrape
request, so an in-memory counter would not survive), we persist a small JSON
counter keyed by date.

State file shape: {"date": "YYYY-MM-DD", "unlocks": <int>}
On a new date the counter resets to 0. A corrupt/missing file reads as 0.

`today` is injected (not read from the clock) so callers can pass a stable
date string and tests are deterministic. main.py passes
datetime.now(timezone.utc).strftime("%Y-%m-%d").
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "date" in data and "unlocks" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return {"date": None, "unlocks": 0}


def _write(path: str, date: str, unlocks: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"date": date, "unlocks": unlocks}, f)
    os.replace(tmp, path)  # atomic


def unlocks_today(path: str, today: str) -> int:
    """Return how many unlocks have been recorded for `today` (0 on new day)."""
    data = _read(path)
    if data.get("date") != today:
        return 0
    return int(data.get("unlocks", 0))


def record_unlock(path: str, today: str) -> int:
    """Increment and persist the unlock counter for `today`. Returns new count."""
    current = unlocks_today(path, today)
    new_count = current + 1
    _write(path, today, new_count)
    return new_count


def budget_remaining(path: str, cap: int, today: str) -> int:
    """Return max(0, cap - unlocks_today)."""
    return max(0, cap - unlocks_today(path, today))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/test_unlock_budget.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Add the setting**

Modify `models/config.py` — after the `relocation_max_distance_km` block, before `model_config`:
```python
    # Hard daily ceiling on StepStone profile unlocks across ALL jobs in a day.
    # n8n sends each job as a separate /scrape request, so this is enforced via
    # a persistent JSON counter (state/unlock_counter.json) that resets daily.
    # This is the true backstop behind the per-job cap — even if n8n sends more
    # jobs than expected, the scraper stops unlocking once the daily cap is hit.
    # Set to 0 to disable the cap (NOT recommended in production).
    max_unlocks_per_day: int = 100
```

- [ ] **Step 6: Wire the counter into main.py — add import + path**

In `main.py`, add to the imports near `from utils.webhook import send_webhook`:
```python
from utils import unlock_budget
```

After `COUNTER_PATH = os.path.join("state", "account_counter.json")` add:
```python
UNLOCK_COUNTER_PATH = os.path.join("state", "unlock_counter.json")
```

- [ ] **Step 7: Add the budget check before unlock + increment after**

Find the unlock call in `main.py` (~line 362):
```python
            # 4c. Unlock + extract profile
            logger.info(f"Match! Extracting profile {candidate.profile_id}")
            profile = await extract_profile(
                page,
                candidate.profile_id,
                account_label,
                preview_cv_url=getattr(candidate, "cv_url", ""),
            )
```

Replace with:
```python
            # 4c. DAILY UNLOCK BUDGET CHECK (hard credit ceiling across all jobs)
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if settings.max_unlocks_per_day > 0:
                remaining = unlock_budget.budget_remaining(
                    UNLOCK_COUNTER_PATH, settings.max_unlocks_per_day, today
                )
                if remaining <= 0:
                    logger.warning(
                        f"DAILY UNLOCK CAP REACHED ({settings.max_unlocks_per_day}). "
                        f"Skipping unlock for {candidate.profile_id} and stopping this job."
                    )
                    result.partial = True
                    break

            # 4d. Unlock + extract profile (THIS SPENDS ONE CREDIT)
            logger.info(f"Match! Extracting profile {candidate.profile_id}")
            profile = await extract_profile(
                page,
                candidate.profile_id,
                account_label,
                preview_cv_url=getattr(candidate, "cv_url", ""),
            )
            if profile:
                # Unlock succeeded — record the credit spend immediately, before
                # any post-unlock gate can `continue` past the increment.
                new_count = unlock_budget.record_unlock(UNLOCK_COUNTER_PATH, today)
                logger.info(
                    f"Unlock recorded: {new_count}/{settings.max_unlocks_per_day} today "
                    f"({candidate.profile_id})"
                )
```

NOTE: there is already an `if profile:` block immediately after the original `extract_profile` call. The new `if profile:` above ADDS the counter line; keep the EXISTING `if profile:` block that follows (the post-unlock gates). Do not duplicate or remove it — the new block only records the unlock, then control falls through to the existing `if profile:` / `else:` extraction-success handling. To avoid two adjacent `if profile:` blocks, MERGE: place `new_count = unlock_budget.record_unlock(...)` and its log as the FIRST statements inside the EXISTING `if profile:` block instead of adding a second one.

- [ ] **Step 8: Verify the merge — read the region**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('ok')"`
Expected: `ok`. Then visually confirm there is exactly ONE `if profile:` block after `extract_profile`, and `record_unlock` is its first statement.

- [ ] **Step 9: Add env documentation**

In `.env.example`, after the `RELOCATION_MAX_DISTANCE_KM` block:
```bash
# Hard daily ceiling on StepStone unlocks across ALL jobs (credit safety).
# Persisted in state/unlock_counter.json, resets daily. The true backstop
# behind per-job caps. For the BenSourcing run keep at 100. Set 0 to disable.
MAX_UNLOCKS_PER_DAY=100
```

- [ ] **Step 10: Full test run + commit**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/ -v`
Expected: all pass.
```bash
git add utils/unlock_budget.py tests/test_unlock_budget.py models/config.py main.py .env.example
git commit -m "feat: persistent daily unlock cap as hard credit backstop"
```

---

## Task 3: Job-specific keywords (STICHWORT criteria)

**Problem:** Umair tags offers in Recruitee with a keyword (e.g. "Armatur") alongside the radius tag. n8n forwards it. The scraper must add it as a StepStone keyword criterion so results must contain that term. Currently unsupported.

**Files:**
- Modify: `models/job.py` (add `keywords` field + normalizer)
- Modify: `scraper/search.py` (add `_add_keyword_criterion`, call in `search_candidates`)
- Modify: `main.py` (pass `job.keywords` to `search_candidates`)
- Modify: `tests/test_models.py`

- [ ] **Step 1: Write the failing model test**

Add to `tests/test_models.py`:
```python
from models.job import JobInput


def test_jobinput_keywords_defaults_empty():
    j = JobInput(offer_id="1", stage_id="2", job_title="Koch", location="Berlin")
    assert j.keywords == []


def test_jobinput_keywords_from_list():
    j = JobInput(offer_id="1", stage_id="2", job_title="X", location="Y",
                 keywords=["Armatur", "Industrie"])
    assert j.keywords == ["Armatur", "Industrie"]


def test_jobinput_keywords_from_comma_string():
    # n8n may send a single comma-separated string from the Recruitee tag
    j = JobInput(offer_id="1", stage_id="2", job_title="X", location="Y",
                 keywords="Armatur, Industrie")
    assert j.keywords == ["Armatur", "Industrie"]


def test_jobinput_keywords_from_single_string():
    j = JobInput(offer_id="1", stage_id="2", job_title="X", location="Y",
                 keywords="Armatur")
    assert j.keywords == ["Armatur"]


def test_jobinput_keywords_strips_blanks():
    j = JobInput(offer_id="1", stage_id="2", job_title="X", location="Y",
                 keywords="Armatur, , ,Industrie,")
    assert j.keywords == ["Armatur", "Industrie"]
```

- [ ] **Step 2: Run to verify failure**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/test_models.py -k keywords -v`
Expected: FAIL — `keywords` not a field / validation error.

- [ ] **Step 3: Add the field + normalizer to `models/job.py`**

```python
from pydantic import BaseModel, ConfigDict, Field, AliasChoices, field_validator


class JobInput(BaseModel):
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
    account: str | None = None

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
```

- [ ] **Step 4: Run model tests to verify pass**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/test_models.py -k keywords -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Add `_add_keyword_criterion` to `scraper/search.py`**

Add after `_add_criterion_via_autosuggest`:
```python
async def _add_keyword_criterion(page: Page, keyword: str) -> bool:
    """Add a keyword as a STICHWORT criterion (must appear in candidate profile).

    Unlike job_title/location, we explicitly target the keyword section of the
    autosuggest dropdown so the term is ANDed as a free-text keyword rather than
    being interpreted as a job-title or location criterion. Returns True if a
    keyword chip was added.
    """
    field = await page.query_selector("#searchfield__textfield")
    if not field:
        return False
    await field.click(force=True)
    await field.fill("")
    await human_delay(300, 600)
    for ch in keyword:
        await field.type(ch, delay=80)
    await human_delay(2000, 3000)  # autosuggest debounce + render

    # Click the verbatim item under the STICHWORT (keyword) section.
    clicked = await page.evaluate(
        """(kw) => {
            const sections = Array.from(document.querySelectorAll('[class*=autosuggest__section-keyword]'));
            for (const sec of sections) {
                const items = Array.from(sec.querySelectorAll('.autosuggest__criteria'));
                for (const it of items) {
                    if ((it.innerText || '').trim().toLowerCase() === kw.toLowerCase()) {
                        it.click();
                        return true;
                    }
                }
                // fall back to first item in the keyword section
                if (items.length) { items[0].click(); return true; }
            }
            return false;
        }""",
        keyword,
    )
    if clicked:
        await human_delay(2000, 3500)  # criterion commit + auto re-search
        logger.info(f"Added keyword criterion: {keyword!r}")
        return True

    # Fallback: ArrowDown + Enter (may select a non-keyword section; log it)
    logger.warning(
        f"Keyword section not found for {keyword!r}; falling back to ArrowDown+Enter"
    )
    await field.press("ArrowDown")
    await human_delay(300, 600)
    await field.press("Enter")
    await human_delay(2000, 3500)
    return False
```

- [ ] **Step 6: Wire keywords into `search_candidates`**

Change the signature (currently `def search_candidates(page, job_title, location, max_distance_km=DEFAULT_RADIUS_KM)`):
```python
async def search_candidates(
    page: Page,
    job_title: str,
    location: str,
    max_distance_km: int = DEFAULT_RADIUS_KM,
    keywords: list[str] | None = None,
) -> tuple[list[SearchResult], int | None]:
```

After the location criterion is added (after the `_add_criterion_via_autosuggest(page, location)` call and its `_country_chip_present` check, BEFORE `_set_page_size`), insert:
```python
    # Add job-specific keyword criteria (ANDed; narrows results + saves credits)
    for kw in (keywords or []):
        logger.info(f"Adding keyword criterion: {kw!r}")
        await _add_keyword_criterion(page, kw)
```

- [ ] **Step 7: Pass keywords from main.py**

In `main.py`, find the `search_candidates` call (~line 254):
```python
        candidates, radius = await search_candidates(
            page, job.job_title, job.location, max_distance_km=job.max_distance_km
        )
```
Replace with:
```python
        candidates, radius = await search_candidates(
            page, job.job_title, job.location,
            max_distance_km=job.max_distance_km,
            keywords=job.keywords,
        )
        if job.keywords:
            logger.info(f"Applied job-specific keywords: {job.keywords}")
```

- [ ] **Step 8: Compile check + full test run**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['main.py','scraper/search.py','models/job.py']]; print('ok')"`
Expected: `ok`
Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 9: LIVE keyword probe (FREE — no unlock)**

Recreate `.env` with real creds (see Task 5 verification), then run a probe that searches "Service Techniker" + "Leuna" + keyword "Armatur" and confirms the cards mention Armatur / the keyword chip rendered. NO `extract_profile` call. Wipe `.env` after.

- [ ] **Step 10: Commit**

```bash
git add models/job.py scraper/search.py main.py tests/test_models.py
git commit -m "feat: job-specific keyword criteria via Recruitee tag -> n8n -> STICHWORT"
```

---

## Task 4: Route ALL post-unlock rejects to the rejected pipeline

**Problem:** Umair wants every candidate rejected AFTER unlock pushed to offer `2592624` ("Rejected Candidates - StepStone DirectSearch"). Currently only the `location_unknown` (truly-unknown) branch does. The foreign-address, `too_far_post_unlock`, and `too_far_for_relocation_post_unlock` branches drop the candidate with no push.

**Decision flagged for Umair:** foreign-address candidates were previously NOT pushed anywhere (old rule: "no foreign candidates in Recruitee"). The new requirement says ALL post-unlock rejects go to the dedicated rejected pipeline. This plan routes them there too, clearly labeled "Ausland", since the rejected pipeline is internal and exists for exactly this review purpose. If Umair wants foreign candidates dropped entirely, skip the foreign-address change in Step 4.

**Files:**
- Modify: `main.py` (rename + generalize `_maybe_push_to_talent_pool`; call from 3 more branches)
- Modify: `tests/test_main_push.py`
- Modify: `.env.example` (clarify the offer is the rejected pipeline)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_main_push.py`:
```python
import main as main_mod
from models.candidate import CandidateResult


def _rej_profile():
    return CandidateResult(
        name="Reject Test", stepstone_profile_id="999",
        email="r@example.com", phone="+49 171 0000000",
        cv_base64="ZmFrZQ==", cv_filename="cv.pdf",
        unlocked=True, unlock_reason="too_far_post_unlock",
        account_used="Account 1",
    )


@pytest.mark.asyncio
@respx.mock
async def test_reject_routing_pushes_to_rejected_pipeline(monkeypatch):
    """A post-unlock reject with talent-pool config set is pushed to offer 2592624."""
    monkeypatch.setattr(main_mod.settings, "recruitee_api_token", "tok", raising=False)
    monkeypatch.setattr(main_mod.settings, "recruitee_talent_pool_offer_id", 2592624, raising=False)
    monkeypatch.setattr(main_mod.settings, "recruitee_talent_pool_stage_id", 13166770, raising=False)

    create_route = respx.post(f"{BASE}/candidates").mock(
        return_value=httpx.Response(201, json={"candidate": {"id": 9, "placements": [{"id": 8}]}})
    )
    respx.patch(f"{BASE}/candidates/9/update_cv").mock(return_value=httpx.Response(200, json={}))
    respx.patch(f"{BASE}/placements/8/change_stage").mock(return_value=httpx.Response(200, json={}))

    profile = _rej_profile()
    await main_mod._maybe_push_to_rejected_pipeline(
        profile=profile, original_offer_id=2517044, reason="Aus Radius",
    )
    assert create_route.call_count == 1
    import json as _json
    body = _json.loads(create_route.calls[0].request.read())
    assert body["offer_ids"] == [2592624]
    assert any("Aus Radius" in s for s in body["candidate"]["sources"])


@pytest.mark.asyncio
async def test_reject_routing_noop_without_config(monkeypatch):
    """No talent-pool config → no-op, no raise."""
    monkeypatch.setattr(main_mod.settings, "recruitee_talent_pool_offer_id", None, raising=False)
    monkeypatch.setattr(main_mod.settings, "recruitee_talent_pool_stage_id", None, raising=False)
    profile = _rej_profile()
    # Should simply return without error
    await main_mod._maybe_push_to_rejected_pipeline(
        profile=profile, original_offer_id=2517044, reason="Aus Radius",
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/test_main_push.py -k reject_routing -v`
Expected: FAIL — `_maybe_push_to_rejected_pipeline` does not exist.

- [ ] **Step 3: Rename + regeneralize the function in main.py**

Rename `_maybe_push_to_talent_pool` → `_maybe_push_to_rejected_pipeline`. Update its docstring and the `sources` label from `"Talent Pool: ..."` to `"Abgelehnt: ..."`:
```python
async def _maybe_push_to_rejected_pipeline(
    profile: "CandidateResult",
    original_offer_id: int,
    reason: str,
) -> None:
    """Push a post-unlock-REJECTED candidate to the dedicated Recruitee
    'Rejected Candidates - StepStone DirectSearch' pipeline (offer 2592624,
    configured via RECRUITEE_TALENT_POOL_OFFER_ID/STAGE_ID).

    No-op if the offer/stage or API token are unset. `reason` is a short German
    label (e.g. 'Aus Radius', 'Standort Unklar', 'Ausland') shown in the
    candidate's `sources` so the recruiter sees WHY it was rejected and which
    original offer triggered it. CV is uploaded (push happens before cv_base64
    is stripped); if CV is missing, _push_to_recruitee refuses (cv_missing).
    """
    if not settings.recruitee_api_token:
        return
    if not (settings.recruitee_talent_pool_offer_id and settings.recruitee_talent_pool_stage_id):
        return

    sources = [
        "StepStone Automation",
        f"Abgelehnt: {reason} (Offer {original_offer_id})",
    ]
    logger.info(
        f"  REJECTED PIPELINE PUSH {profile.stepstone_profile_id}: reason={reason!r}, "
        f"original_offer={original_offer_id}"
    )
    await _push_to_recruitee(
        profile=profile,
        offer_id=settings.recruitee_talent_pool_offer_id,
        stage_id=settings.recruitee_talent_pool_stage_id,
        token=settings.recruitee_api_token,
        company_id=settings.recruitee_company_id,
        sources=sources,
    )
```

- [ ] **Step 4: Update the existing call site + add 3 new ones**

(a) The existing truly-unknown branch (~line 516) — rename the call and push BEFORE cv strip (already does):
```python
            await _maybe_push_to_rejected_pipeline(
                profile=profile,
                original_offer_id=int(job.offer_id),
                reason="Standort Unklar",
            )
```

(b) The foreign-address branch (~line 489-501) — after building `reason_text`, BEFORE `profile.cv_base64 = None` (which is at ~526, shared). Add the push right after the `logger.warning(... LOCATION UNGEOCODABLE ...)` block, replacing the `# No talent-pool push for foreign candidates.` comment:
```python
            # Route foreign rejects to the dedicated rejected pipeline too,
            # labeled "Ausland", so the recruiter has full visibility. (If Umair
            # wants foreign candidates dropped entirely, remove this call.)
            await _maybe_push_to_rejected_pipeline(
                profile=profile,
                original_offer_id=int(job.offer_id),
                reason="Ausland",
            )
```

(c) The `too_far` post-unlock branch (~line 540-581): BEFORE `profile.cv_base64 = None` (line ~577), add:
```python
                            await _maybe_push_to_rejected_pipeline(
                                profile=profile,
                                original_offer_id=int(job.offer_id),
                                reason="Aus Radius",
                            )
```
This single call covers BOTH `too_far_post_unlock` and `too_far_for_relocation_post_unlock` because it sits in the shared `if not accepted_post:` block before the cv strip.

- [ ] **Step 5: Compile check**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('ok')"`
Expected: `ok`. Grep to confirm no stale name remains:
Run: `grep -n "_maybe_push_to_talent_pool" main.py` → Expected: no output.

- [ ] **Step 6: Run tests**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 7: Clarify .env.example**

Update the talent-pool comment block to note the offer is now the rejected pipeline:
```bash
# Recruitee "Rejected Candidates - StepStone DirectSearch" pipeline (offer 2592624).
# ALL post-unlock-rejected candidates (too far / location unknown / foreign) are
# pushed here for manual recruiter review, labeled with the rejection reason in
# `sources`. Both vars must be set; either unset disables routing (rejects are
# dropped). Stage 13166770 = "Gesourct" (entry stage).
RECRUITEE_TALENT_POOL_OFFER_ID=2592624
RECRUITEE_TALENT_POOL_STAGE_ID=13166770
```

- [ ] **Step 8: Commit**

```bash
git add main.py tests/test_main_push.py .env.example
git commit -m "feat: route all post-unlock rejects to dedicated rejected pipeline"
```

---

## Task 5: Final verification + deploy

- [ ] **Step 1: Full suite green**

Run: `/Users/utkarsh/Projects/stepstone-scraper/.venv/bin/python -m pytest tests/ -v`
Expected: all pass (88 existing + new).

- [ ] **Step 2: Live free probe (search + keyword only, NO unlock)**

Recreate `.env` with real creds. Run a probe that, for one #BenSourcing-style job with a keyword, confirms: structured location chip present, keyword chip present, cards returned. Confirms zero credits via no `extract_profile`. Wipe `.env` after.

- [ ] **Step 3: Push to main (Railway auto-deploy)**

```bash
git push origin claude/gracious-elbakyan-7b16b8:main
```

- [ ] **Step 4: Set Railway env vars** (manual, by Utkarsh)

- `MAX_CANDIDATES_PER_JOB=10`
- `MAX_UNLOCKS_PER_DAY=100`
- confirm `RECRUITEE_TALENT_POOL_OFFER_ID=2592624`, `RECRUITEE_TALENT_POOL_STAGE_ID=13166770`
- confirm `RELOCATION_MAX_DISTANCE_KM` (200 default, or tighter)

---

## Pre-run checklist (NON-CODE — Utkarsh/Umair must confirm)

- [ ] n8n sends `keywords` in the job payload from the Recruitee tag (field name agreed with Umair)
- [ ] n8n sends `max_candidates` (or rely on Railway `MAX_CANDIDATES_PER_JOB=10` ceiling)
- [ ] Exactly the 10 `#BenSourcing` offers are wired into the run
- [ ] **Airtable NOT wiped** (wiping re-spends credits on existing-in-Recruitee candidates)
- [ ] Umair confirms: foreign-address rejects → rejected pipeline (Task 4) or drop?
- [ ] Recruitee shows 8 unlocks remaining now; new monthly allowance available before the run

---

## Self-Review Notes

- **Spec coverage:** keywords (Task 3), reject routing (Task 4), per-job cap (Task 1), 100 hard cap (Task 2), no-CV/interns/dups already shipped. All Umair items covered.
- **Type consistency:** `_maybe_push_to_rejected_pipeline` used consistently in Task 4 (renamed from `_maybe_push_to_talent_pool`); `unlock_budget.record_unlock/unlocks_today/budget_remaining` consistent across Task 2.
- **Credit-safety layering:** per-job ceiling (Task 1) + daily global cap (Task 2) are independent; both active.

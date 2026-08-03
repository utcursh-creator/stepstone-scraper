# StepStone Sourcing Scraper

A FastAPI service that sources candidates from **StepStone DirectSearch** for a
given job, evaluates them with an LLM, unlocks the ones that match, and pushes
them into **Recruitee**. It is driven by n8n, which dispatches one job at a time
and receives the results on a webhook.

> **Every profile unlock spends one paid StepStone credit.** Read
> [Credit safety](#credit-safety) before changing anything on the unlock path.

Proprietary software — see [LICENSE](LICENSE).

---

## How a job flows

```
n8n  ──POST /scrape──▶  scraper                        (returns 202 immediately)
                          │
                          ├─ geocode the job location   (abort if unresolvable)
                          ├─ launch browser via DE residential proxy
                          ├─ authenticate to StepStone  (reuse session, else fresh login)
                          ├─ DirectSearch: job title + location + radius + keywords
                          │
                          └─ for each result card:
                               1. Airtable dedup        ─ skip if already processed
                               2. CV gate               ─ skip if no CV attached
                               3. distance gate         ─ skip if Wohnort too far
                               4. LLM evaluation        ─ on the PREVIEW text
                               ▼  ── everything above is FREE ──
                               5. UNLOCK (spends 1 credit)
                               6. Recruitee dedup       ─ skip if already in the ATS
                               7. post-unlock distance gate
                               8. push to Recruitee (or to the review pool)
                          │
                          └──POST results──▶ n8n webhook
```

Gates 1–4 run **before** the unlock by design: everything that can reject a
candidate for free must do so before any credit is spent.

## Endpoints

| Method | Path      | Purpose |
|--------|-----------|---------|
| `POST` | `/scrape` | Start a job. Returns **202** immediately; the scrape runs as a background task and reports via the webhook. Returns **409** if a scrape is already running. |
| `GET`  | `/health` | Liveness only. Returns `ok` even when every credential is wrong. |
| `GET`  | `/status` | `{"state": "idle"\|"running", "job": …, "error": …}`. **This is the one that answers "is a scrape running?"** |

`/scrape` body (as n8n sends it):

```json
{
  "title": "Physiotherapeut (m/w/d)",
  "location": "Warendorf",
  "offer_id": "2468458",
  "stage_id": "12807955",
  "requirements": "",
  "account": "Account 1",
  "credits_remaining": 200,
  "max_distance_km": 25,
  "keywords": ""
}
```

> `/scrape` is **unauthenticated**. The hostname is the only access control —
> anyone who learns it can spend real StepStone credits. Treat the deployed URL
> as a secret.

## Configuration

All configuration comes from environment variables — see
[`.env.example`](.env.example) for the full list with notes.
`models/config.py` is the authoritative definition.

Settings are validated at import, so a missing required variable is a
**crash-loop with a clear `ValidationError`**, not a silent misconfiguration.

Three that catch people out:

- **`LLM_BASE_URL` / `LLM_MODEL` default to OpenRouter + a Claude model slug.**
  Setting only `LLM_API_KEY` to an OpenAI key sends every request to OpenRouter,
  which 401s. Set all three explicitly.
- **`OPENROUTER_API_KEY` is a legacy alias for `LLM_API_KEY`.** On a new
  deployment, set `LLM_API_KEY` and do **not** set the alias — otherwise the
  instance boots happily against whoever owns that key.
- **`Settings` ignores unknown variables.** A misspelled name (`RECRUITEE_API_KEY`
  instead of `RECRUITEE_API_TOKEN`, `OPENAI_API_KEY` instead of `LLM_API_KEY`)
  is silently discarded and the feature it was meant to enable just… doesn't.

## Deployment (Railway)

The repo ships a Dockerfile; Railway detects it automatically. Allow 5–10
minutes for the first build — the `patchright install chromium` step on top of
an already-large Playwright base image is the usual first-deploy failure, and
usually succeeds on a retry.

Required settings:

| Setting | Value | Why |
|---|---|---|
| Target port | **8000** | The app **ignores** Railway's injected `PORT`; the Dockerfile hardcodes `--port 8000` in exec form. Setting a `PORT` variable does nothing. |
| Healthcheck path | `/health` | Surfaces a boot failure as a failed deploy instead of a silently 502-ing service. |
| Volume mount | **`/app/state`** | The daily unlock counter lives here. See below. |
| Replicas | **1** | The concurrency guard is a per-process lock and each replica gets its own filesystem — two replicas mean two simultaneous logins on the same recruiter account and double the daily cap. |
| App sleeping | **off** | The only regular traffic is a schedule; a sleeping instance cold-starts a multi-GB image on the dispatch request. |

**The volume is not optional.** `state/unlock_counter.json` holds the daily
unlock count, and a missing file reads back as *zero unlocks used*. On an
ephemeral filesystem every redeploy re-grants a full daily credit budget.
`/app/sessions` and `/app/screenshots` stay ephemeral; that is acceptable.

## Credit safety

Layers, outermost first:

1. **`MAX_CANDIDATES_PER_JOB`** — the effective per-job cap. n8n never sends a
   per-job limit, so this is the only lever that binds. **Setting it to `0` is
   the true zero-credit kill switch.**
2. **`MAX_UNLOCKS_PER_DAY`** — cross-job daily backstop, persisted in the volume.
   **`0` means *unlimited*, not zero.** Never use it to "safe" an instance.
3. **Pre-unlock gates** — dedup, CV, distance and the LLM evaluation all run
   before any credit is spent.
4. **Accounting follows the charge, not the outcome.** `extract_profile` returns
   `(result, credit_spent)`; the counter is incremented whenever the unlock
   click landed, even if extraction then failed. Conflating those two let the
   counter drift below real spend, so the cap authorised extra unlocks on top of
   credits already burned.

An unrecoverable cost remains by design: StepStone hides a candidate's identity
until you unlock them, so the Recruitee duplicate check can only run *after* the
credit is spent.

## Operating notes

- **Never redeploy during a run.** A restart SIGKILLs the in-flight background
  task: the webhook never fires, n8n's chain-dispatch stops, and the job's row
  is stranded on `Dispatched` while its credits are already gone. Check
  `GET /status` first, and after an unplanned restart reset the affected row
  from `Dispatched` back to `Queued` by hand.
- **A wedged run** (`/status` stuck on `running` well beyond the usual 15–25
  minutes) has no automatic timeout; restarting the service is the only recovery.
- **Watch memory across a multi-job day.** The Playwright driver process is not
  stopped between jobs, so usage climbs; restart between batches if needed.
- **Sessions are ephemeral**, so the first run after any deploy performs a full
  fresh login. If `TWOCAPTCHA_API_KEY` is empty (its default), a CAPTCHA-gated
  login fails outright rather than being solved.
- **Credential changes must be communicated.** A silently rotated StepStone
  password fails every job in the batch at login.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
patchright install chromium
pytest tests/
```

Dependencies are **pinned** — see the note at the top of `requirements.txt`
before changing them.

The test suite runs entirely offline against fake page/context objects; no
browser, no network, no credentials. The most load-bearing suites are:

| File | Guards |
|---|---|
| `tests/test_unlock_credit_safety.py` | a spent credit is always recorded; a half-rendered dialog is a failure, not a success |
| `tests/test_eval_error.py` | an errored evaluation is never emitted as a verdict |
| `tests/test_search_field_wait.py` | the DirectSearch field is awaited, never probed once |
| `tests/test_auth_session.py` | a restored session is proven, and the cookie jar is cleared before a fresh login |
| `tests/test_ortsteil.py` | German district addresses resolve to their municipality |

These encode production incidents. Each file's docstring says which one and
what it cost — read it before relaxing an assertion.

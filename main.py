import asyncio
import logging
import os
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException

from models.candidate import CandidateResult, ScrapeResult
from models.config import Settings
from models.job import JobInput
from scraper.auth import AuthenticationError, authenticate, save_session
from scraper.browser import close_browser, create_browser
from scraper.dedup import check_duplicate
from scraper.talent_unlock import UnlockError, unlock_talent
from scraper.rotation import select_account
from scraper.talent_search import SearchError, search_talents
from utils.delays import human_delay
from utils.datadir import SESSIONS_DIR, STATE_DIR
from utils.geocode import (
    clear_cache,
    extract_wohnadresse,
    extract_gewuenschte_arbeitsorte,
    calculate_distance_km,
    check_desired_location_match,
    geocode_location,
    geocoder_blocked,
    GeocoderUnavailable,
    strip_ortsteil,
    should_accept_far_candidate,
    DIST_TOO_FAR_FOR_RELOCATION,
)
from utils.openrouter import evaluate_candidate
from utils.recruitee import (
    create_candidate,
    upload_cv,
    set_stage,
    check_candidate_exists_in_recruitee,
    clear_candidates_cache,
    RecruiteeError,
)
from utils.webhook import send_webhook
from utils import unlock_budget

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

settings = Settings()
scrape_lock = asyncio.Lock()
current_status: dict = {"state": "idle", "job": None, "error": None}

COUNTER_PATH = os.path.join(STATE_DIR, "account_counter.json")

# After every account failed to log in, no further login is attempted for this
# long. n8n chains the next queued job straight after a failed one, so without
# this one bad login becomes one login per queued job, from rotating proxy IPs:
# exactly what trips StepStone's bot check and gets an account locked.
AUTH_COOLDOWN_S = 3 * 3600

# A job stops taking new candidates after this long and reports what it has;
# the hard limit below is only for a job that hangs outright.
JOB_SOFT_LIMIT_S = 45 * 60
JOB_HARD_LIMIT_S = 60 * 60
MAX_CONSECUTIVE_UNLOCK_FAILURES = 2

# The running job's result, so a job killed by the hard limit still reports
# what it already did. One job at a time (scrape_lock).
_current_result = None
_auth_blocked_until: float = 0.0
_auth_block_reason: str = ""


def _block_logins(reason: str) -> None:
    global _auth_blocked_until, _auth_block_reason
    _auth_blocked_until = time.time() + AUTH_COOLDOWN_S
    _auth_block_reason = reason[:300]
UNLOCK_COUNTER_PATH = os.path.join(STATE_DIR, "unlock_counter.json")

# Abort a job after this many CONSECUTIVE eval errors. One timeout is transient
# and just skips a candidate; a run of them means the evaluator is systemically
# down (e.g. OpenRouter 402 — account out of funds), so there is no point
# walking the rest of the cards. No candidate is ever burned either way —
# errored candidates are never emitted — this only stops wasting the browser
# session and puts the reason in the webhook `error` for Slack.
EVAL_ERROR_ABORT_THRESHOLD = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs("screenshots", exist_ok=True)
    yield


app = FastAPI(title="StepStone Scraper", lifespan=lifespan)


async def _push_to_recruitee(
    profile: "CandidateResult",
    offer_id: int,
    stage_id: int,
    token: str,
    company_id: str,
    sources: list[str] | None = None,
) -> None:
    """Create candidate in Recruitee, upload CV, set stage.

    REFUSES to push when profile.cv_base64 is missing — without a CV file the
    Recruitee record is useless to the recruiter (Umair's feedback after
    Martin Winkler / Thomas Neumann landed with 'Noch kein Lebenslauf'). A
    missing cv_base64 at this point indicates either a CV-download failure
    after unlock or a pre-unlock false positive; in both cases we'd rather
    drop the candidate than dirty Recruitee with an empty profile.

    `sources` defaults to ["StepStone Automation"]; talent-pool pushes
    override it with a richer label that names the rejection reason and
    the original offer ID so the recruiter has full audit context.
    """
    # Step 0: Require a CV. Abort BEFORE touching Recruitee — we don't want to
    # create then orphan a half-formed candidate record.
    if not profile.cv_base64:
        logger.warning(
            f"REFUSING Recruitee push for {profile.stepstone_profile_id}: "
            f"cv_base64 is missing (CV download failed or pre-unlock false positive). "
            f"Candidate will not be created in Recruitee."
        )
        profile.recruitee_status = "cv_missing"
        return

    # Step 1: Create candidate + link to offer
    try:
        candidate_id, placement_id = await create_candidate(
            token=token,
            company_id=company_id,
            name=profile.name,
            emails=[profile.email] if profile.email else [],
            phones=[profile.phone] if profile.phone else [],
            offer_id=offer_id,
            sources=sources,
        )
        profile.recruitee_candidate_id = candidate_id
        profile.recruitee_placement_id = placement_id
        profile.recruitee_status = "created"
    except RecruiteeError as e:
        logger.error(f"Recruitee create_candidate failed for {profile.stepstone_profile_id}: {e}")
        profile.recruitee_status = "failed"
        return  # Skip CV upload and stage set if creation failed

    # Step 2: Upload CV (guaranteed present at this point — Step 0 enforces it)
    import base64 as _base64
    cv_bytes = _base64.b64decode(profile.cv_base64)
    filename = profile.cv_filename or "CV.pdf"
    uploaded = await upload_cv(
        token=token,
        company_id=company_id,
        candidate_id=candidate_id,
        cv_bytes=cv_bytes,
        filename=filename,
    )
    profile.cv_uploaded = uploaded
    if uploaded:
        profile.recruitee_status = "cv_uploaded"
    else:
        logger.warning(
            f"CV upload to Recruitee failed for candidate {candidate_id}; continuing to stage set"
        )

    # Step 3: Set stage (non-fatal)
    stage_set = await set_stage(
        token=token,
        company_id=company_id,
        placement_id=placement_id,
        stage_id=stage_id,
    )
    if stage_set:
        profile.recruitee_status = "stage_set"
    else:
        logger.warning(
            f"Stage set failed for placement {placement_id}; "
            f"status stays {profile.recruitee_status!r}"
        )


async def _maybe_push_to_rejected_pipeline(
    profile: "CandidateResult",
    original_offer_id: int,
    reason: str,
) -> None:
    """Push a post-unlock-REJECTED candidate to the dedicated Recruitee
    'Rejected Candidates - StepStone DirectSearch' pipeline (offer 2592624,
    configured via RECRUITEE_TALENT_POOL_OFFER_ID / _STAGE_ID).

    No-op if the offer/stage or API token are unset — falls back to the
    previous "drop" behaviour without raising.

    `reason` is a short German label like 'Aus Radius', 'Standort Unklar' or
    'Ausland' shown in the candidate's `sources` field alongside the original
    offer ID, so the recruiter sees WHY the candidate was rejected and which
    sourcing job triggered it. The CV is uploaded too (callers push BEFORE
    stripping cv_base64); if the CV is missing, _push_to_recruitee refuses
    (recruitee_status='cv_missing'), consistent with the no-CV rule.
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


async def run_scrape(job: JobInput) -> ScrapeResult:
    """Main scrape orchestrator. Returns ScrapeResult without sending webhook.

    The caller is responsible for sending the webhook AFTER releasing the
    concurrency lock, so n8n's chain-dispatch doesn't hit a 409.
    """
    global current_status
    clear_cache()  # Reset geocoding cache for this job
    clear_candidates_cache()  # Reset Recruitee candidate cache for this job
    current_status = {"state": "running", "job": job.model_dump(), "error": None}
    accounts = settings.get_accounts()
    logger.info(
        f"Job received: account_requested={job.account!r}, "
        f"max_distance_km={job.max_distance_km}"
    )
    account = select_account(accounts, job.account, COUNTER_PATH)
    account_label = f"Account {accounts.index(account) + 1}"

    global _current_result
    result = ScrapeResult(
        offer_id=job.offer_id,
        stage_id=job.stage_id,
        job_title=job.job_title,
        location=job.location,
        requirements=job.requirements,
        account_used=account_label,
    )
    _current_result = result  # locked_scrape reports it if the job must be killed

    # ================================================================
    # PRE-FLIGHT: the job's own location must be geocodable.
    # Every distance is haversine(candidate, job) — so if the JOB's town
    # doesn't resolve, distance_km is None for every candidate no matter
    # where they live, and the fail-closed gate below rejects all of them.
    # The job cannot produce a single acceptable candidate; the only thing
    # it can do is spend credits. Prod 2026-07-15 (offer 2468824,
    # 'Wölfersheim OT Wohnbach'): 5 unlocked, 5 rejected, 5 credits gone,
    # and each one mislabelled 'Ausland' though four lived in Germany.
    # Bail before the browser, the proxy and the first credit.
    # ================================================================
    try:
        job_coords = geocode_location(job.location)
    except GeocoderUnavailable as e:
        # The geocoder is down or refusing us: that says nothing about the
        # location. Prod 2026-09-28: Nominatim answered 429 to every job and
        # each one was reported as a wrong location. Distinct code, and the
        # message never contains "could not be geocoded".
        msg = (
            f"GEOCODER_UNAVAILABLE: the map lookup service (OpenStreetMap Nominatim) is "
            f"not answering, so the distance to {job.location!r} cannot be checked. "
            f"This is temporary and not about the job's location; nothing was searched "
            f"and no credits were spent. The job runs again on the next cycle. ({e})"
        )
        logger.error(msg)
        result.partial = True
        result.error = msg
        current_status["error"] = msg
        return result
    if job_coords is None:
        msg = (
            f"Job location {job.location!r} could not be geocoded, so no candidate "
            f"could pass the distance gate — aborting before any unlock. The map "
            f"lookup service answered that no such place exists in Germany, so the "
            f"Location on the job is most likely wrong (a plain municipality "
            f"resolves; a typo or a foreign town does not)."
        )
        logger.error(msg)
        result.partial = True
        result.error = msg
        current_status["error"] = msg
        return result

    if geocoder_blocked():
        msg = (
            "GEOCODER_UNAVAILABLE: the map lookup service is rate-limiting this server right now, "
            "so candidate distances cannot be checked. Nothing was searched and no credits were "
            "spent; the job runs again on the next cycle."
        )
        logger.error(msg)
        result.partial = True
        result.error = msg
        current_status["error"] = msg
        return result

    if time.time() < _auth_blocked_until:
        msg = (
            f"AUTH_COOLDOWN: the last login to StepStone failed, so no login is attempted "
            f"for another {int((_auth_blocked_until - time.time()) / 60) + 1} min (repeated "
            f"logins get the account locked). Nothing was searched and no credits were spent. "
            f"Last failure: {_auth_block_reason}"
        )
        logger.error(msg)
        result.partial = True
        result.error = msg
        current_status["error"] = msg
        return result

    # The municipality behind a possible Ortsteil, for the relocation signal
    # below. check_desired_location_match asks whether the job's town appears
    # in the candidate's Gewünschte Arbeitsorte — and candidates write
    # "Wölfersheim", never "Wölfersheim OT Wohnbach", so the raw string never
    # matches. That branch was unreachable while Ortsteil jobs had no distance
    # at all; now that they do, matching on the raw string would silently
    # reject every relocation candidate on those jobs as too_far_no_relocation.
    job_location_base = strip_ortsteil(job.location)

    job_deadline = time.monotonic() + JOB_SOFT_LIMIT_S
    browser = None
    context = None
    authenticated_email = None
    try:
        # 1. Launch browser
        logger.info(f"Launching browser for {job.job_title} in {job.location}")
        browser, context, page = await create_browser(
            proxy_host=settings.proxy_host,
            proxy_port=settings.proxy_port,
            proxy_user=settings.proxy_user,
            proxy_pass=settings.proxy_pass,
            proxy_country=settings.proxy_country,
            sticky_key=account["email"],
        )

        # 2. Authenticate
        logger.info(f"Authenticating as Account {accounts.index(account) + 1}")
        try:
            captcha_solver = None
            if settings.twocaptcha_api_key:
                from twocaptcha import TwoCaptcha
                captcha_solver = TwoCaptcha(settings.twocaptcha_api_key)

            try:
                await authenticate(context, page, account["email"], account["password"], captcha_solver)
            except AuthenticationError:
                raise
            except Exception as raw:
                # A Playwright timeout or crash mid-login is still a login attempt:
                # it must reach the same handling (cooldown, no fallback) as any other.
                raise AuthenticationError(f"{type(raw).__name__}: {str(raw)[:200]}", code="LOGIN_ERROR") from raw
            authenticated_email = account["email"]
            logger.info("Authentication successful")
        except AuthenticationError as e:
            logger.error(f"Auth failed for Account {accounts.index(account) + 1}: {e}")
            failures = [f"{account['email']}: {e}"]
            # Only a rejection of THIS account's password justifies trying the
            # second account. Everything else (CAPTCHA, block, timeouts, a slow
            # proxy) is about this browser and IP: a second password login from
            # it seconds later is how accounts get locked. A saved session that
            # merely loaded slowly typed no password and blocks nothing.
            if e.code == "SESSION_REUSE_TIMEOUT":
                raise
            if e.code != "LOGIN_REJECTED":
                _block_logins(failures[0])
                raise AuthenticationError(
                    "All accounts failed to authenticate (the second account was NOT tried "
                    "after this, to avoid an account lock): " + failures[0],
                    code="AUTH_FAILED",
                ) from e
            authenticated = False
            for alt in accounts:
                if alt["email"] == account["email"]:
                    continue
                try:
                    await authenticate(context, page, alt["email"], alt["password"], captcha_solver)
                    account_label = f"Account {accounts.index(alt) + 1}"
                    result.account_used = account_label
                    authenticated_email = alt["email"]
                    logger.info(f"Authenticated with fallback {account_label}")
                    authenticated = True
                    break
                except Exception as alt_error:
                    failures.append(f"{alt['email']}: {alt_error}")
                    break
            if not authenticated:
                _block_logins(" | ".join(failures))
                raise AuthenticationError(
                    "All accounts failed to authenticate: " + " | ".join(failures),
                    code="AUTH_FAILED",
                )

        # 3. Search Talent Finder. StepStone only offers 40/60/80/100/150 km, so it
        #    gets the smallest radius that covers the job; the exact limit is the
        #    distance gate below, on each candidate's postcode.
        logger.info(f"Searching: {job.job_title} in {job_location_base} (max {job.max_distance_km}km)")
        try:
            search = await search_talents(
                page, job.job_title, job_location_base,
                max_distance_km=job.max_distance_km,
                keywords=job.keywords,
            )
        except SearchError as e:
            # A failed search is an abort, never "found nothing": it must reach
            # n8n's error lane, not the client's Slack as a green zero.
            logger.error(f"Search failed: {e}")
            result.partial = True
            result.error = str(e)
            return result
        if search.total == 0:
            logger.info("StepStone reports 0 matching candidates: a genuine empty result, not a failure.")
        logger.info(
            f"Found {search.total} candidates on StepStone "
            f"(radius sent: {search.radius_km or 'Optimiert'} km, local limit {job.max_distance_km} km"
            f"{', without keywords (fallback)' if search.keyword_fallback else ''})"
        )

        # 4. Process each candidate.
        # Effective per-job cap = min(what n8n requested, the server ceiling).
        # settings.max_candidates_per_job is the central credit kill-switch —
        # set MAX_CANDIDATES_PER_JOB=10 on Railway for the BenSourcing run.
        effective_max_candidates = min(job.max_candidates, settings.max_candidates_per_job)
        logger.info(
            f"Per-job candidate ceiling: {effective_max_candidates} "
            f"(job requested {job.max_candidates}, server cap {settings.max_candidates_per_job})"
        )
        processed = 0
        consecutive_eval_errors = 0
        consecutive_unlock_failures = 0
        seen_profiles: set[str] = set()
        async for candidate in search.iterate():
            if candidate.profile_id in seen_profiles:
                continue  # the same talent on two result pages: handle once
            seen_profiles.add(candidate.profile_id)
            if time.monotonic() > job_deadline:
                # A normal partial run (like the per-job cap), not an error: n8n
                # still posts the summary to Slack and chains the next job.
                logger.warning("Job time budget used up; stopping with the results so far.")
                result.partial = True
                break
            if processed >= effective_max_candidates:
                logger.info(
                    f"Reached per-job cap ({effective_max_candidates}); stopping this job."
                )
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
                result.candidates_skipped_pre_unlock += 1
                continue

            # 4b. PRE-UNLOCK GATE 1: CV attachment check
            # Umair rule: candidates without CVs = "unqualified", skip unlock
            if not candidate.has_cv_attachment:
                logger.info(
                    f"  SKIPPED {candidate.profile_id}: "
                    f"no CV attachment detected in Anhänge section"
                )
                result.candidates.append(
                    CandidateResult(
                        name="",
                        stepstone_profile_id=candidate.profile_id,
                        matched=False,
                        match_confidence=0.0,
                        match_reasoning="Übersprungen: Kein Lebenslauf in Anhänge vorhanden.",
                        unlocked=False,
                        unlock_reason="no_cv",
                        account_used=account_label,
                    )
                )
                processed += 1
                continue

            # 4c. PRE-UNLOCK GATE 2: Distance validation (card-level Wohnort)
            wohnort = candidate.wohnort
            gewuenschte_list = candidate.gewuenschte_arbeitsorte
            gewuenschte_str = " ".join(gewuenschte_list) if gewuenschte_list else None
            distance_km = None

            if wohnort:
                distance_km = calculate_distance_km(wohnort, job.location)
                logger.info(
                    f"  Card-level distance for {candidate.profile_id}: "
                    f"Wohnort={wohnort}, distance={distance_km}km"
                )

            if wohnort and distance_km is None:
                # The card names a home town, but it cannot be placed in Germany
                # (abroad, or unresolvable). After an unlock the profile shows the
                # same place and the fail-closed gate rejects it: a credit paid for
                # a certain rejection. Reject it now, for free.
                logger.info(f"  REJECTED {candidate.profile_id} before unlock: home town not locatable in Germany")
                result.candidates.append(
                    CandidateResult(
                        name="",
                        stepstone_profile_id=candidate.profile_id,
                        matched=False,
                        match_confidence=0.0,
                        match_reasoning=(
                            f"ABGELEHNT: Wohnort {wohnort} lässt sich nicht in Deutschland verorten; "
                            f"Entfernung zu {job.location} nicht prüfbar."
                        ),
                        unlocked=False,
                        unlock_reason="location_unverifiable",
                        account_used=account_label,
                    )
                )
                processed += 1
                continue

            if distance_km is not None and distance_km > job.max_distance_km:
                accepted, dist_reason = should_accept_far_candidate(
                    distance_km=distance_km,
                    relocation_max_km=settings.relocation_max_distance_km,
                    gewuenschte_arbeitsorte=gewuenschte_str,
                    job_location=job_location_base,
                )
                if not accepted:
                    too_far_for_relocate = (dist_reason == DIST_TOO_FAR_FOR_RELOCATION)
                    if too_far_for_relocate:
                        reasoning_de = (
                            f"ABGELEHNT: Wohnort {wohnort} liegt {distance_km:.0f}km "
                            f"von {job.location} entfernt — über dem Umzugslimit von "
                            f"{settings.relocation_max_distance_km}km. Auch mit Umzugswunsch "
                            f"ist die Entfernung nicht realistisch."
                        )
                        unlock_reason_code = "too_far_for_relocation"
                    else:
                        reasoning_de = (
                            f"ABGELEHNT: Wohnort {wohnort} liegt {distance_km:.0f}km "
                            f"von {job.location} entfernt (Maximum: {job.max_distance_km}km). "
                            f"Keine Umzugsbereitschaft erkennbar."
                        )
                        unlock_reason_code = "too_far"
                    logger.info(
                        f"  REJECTED {candidate.profile_id} ({dist_reason}): "
                        f"Wohnort {wohnort} is {distance_km:.0f}km from {job.location} "
                        f"(max {job.max_distance_km}km, relocation cap "
                        f"{settings.relocation_max_distance_km}km)"
                    )
                    result.candidates.append(
                        CandidateResult(
                            name="",
                            stepstone_profile_id=candidate.profile_id,
                            matched=False,
                            match_confidence=0.0,
                            match_reasoning=reasoning_de,
                            unlocked=False,
                            unlock_reason=unlock_reason_code,
                            account_used=account_label,
                        )
                    )
                    processed += 1
                    continue

            # 4d. Evaluate with Claude (with card-level distance data)
            logger.info(
                f"Evaluating {candidate.profile_id} "
                f"(preview_text {len(candidate.preview_text)} chars, "
                f"wohnort={wohnort or 'unknown'}, distance={distance_km}km)"
            )
            eval_result = await evaluate_candidate(
                api_key=settings.llm_api_key,
                candidate_text=candidate.preview_text,
                job_title=job.job_title,
                location=job.location,
                requirements=job.requirements,
                distance_km=distance_km,
                wohnadresse=wohnort,
                gewuenschte_arbeitsorte=gewuenschte_str,
                max_distance_km=job.max_distance_km,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
            )
            logger.info(f"  eval match={eval_result.match} conf={eval_result.confidence} reason={eval_result.reasoning[:150]}")
            await asyncio.sleep(1.0)  # Rate limit: 1 eval/sec

            # 4d-guard: an eval ERROR is not a verdict. If OpenRouter errored
            # (402/5xx/timeout/transport/unparseable) we never got the model's
            # judgment, so this candidate must NOT be emitted. Appending it as
            # match=False would send it to the webhook, n8n would log it to the
            # Airtable dedup table, and it would be skipped forever — never
            # getting a real evaluation. Leave it un-processed (don't append,
            # don't count against the cap) so the next run re-evaluates it.
            # Prod 2026-07-22: OpenRouter 402'd every call; this branch is what
            # keeps that outage from silently burning candidates.
            if eval_result.error:
                result.candidates_eval_failed += 1
                consecutive_eval_errors += 1
                logger.warning(
                    f"  EVAL ERROR for {candidate.profile_id}: {eval_result.reasoning} "
                    f"— not emitting; will be re-evaluated next run "
                    f"({consecutive_eval_errors} consecutive)."
                )
                if consecutive_eval_errors >= EVAL_ERROR_ABORT_THRESHOLD:
                    msg = (
                        f"AI evaluation unavailable: {consecutive_eval_errors} consecutive "
                        f"evaluator errors ({eval_result.reasoning}). Aborting the job so no "
                        f"further cards are wasted; the rest are left un-evaluated for the next "
                        f"run. Check the AI provider (LLM_BASE_URL) — likely out of funds, a bad "
                        f"key, or an unsupported model."
                    )
                    logger.error(msg)
                    result.error = msg
                    result.partial = True
                    break
                continue

            consecutive_eval_errors = 0

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

            # 4c. DAILY UNLOCK BUDGET CHECK — hard credit ceiling across ALL jobs.
            # n8n fires each job as its own /scrape request, so this persistent
            # counter is the only thing that can enforce a true cross-job cap.
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

            # 4d. Unlock (THIS SPENDS ONE CREDIT, unless StepStone reports the
            #     candidate as already unlocked: then it is free)
            logger.info(f"Match! Unlocking {candidate.profile_id}")
            try:
                profile, credit_spent = await unlock_talent(
                    page, candidate, account_label, search.criteria_id,
                )
            except UnlockError as e:
                # No credits left, the session is gone, or StepStone refuses the
                # unlock itself: every further unlock would fail the same way.
                # Stop, and say why. A credit it may have taken is still charged.
                if e.credit_spent:
                    unlock_budget.record_unlock(UNLOCK_COUNTER_PATH, today)
                    # n8n's Credit Ledger is built from unlocked candidates with
                    # credit_spent: report the charge, even without details.
                    result.candidates.append(CandidateResult(
                        name="", stepstone_profile_id=candidate.profile_id, matched=True,
                        match_confidence=eval_result.confidence,
                        match_reasoning=f"Freischaltung abgelehnt ({e.code}); Credit von StepStone womöglich belastet.",
                        unlocked=True, unlock_reason="unlock_rejected", credit_spent=True,
                        account_used=account_label,
                    ))
                logger.error(f"Stopping the job: {e}")
                result.partial = True
                result.error = str(e)
                break
            if profile:
                profile.credit_spent = credit_spent
                consecutive_unlock_failures = 0
            else:
                consecutive_unlock_failures += 1
            # Charge the budget for every credit StepStone actually took, NOT
            # only the ones we got data back from. These are different events:
            # the click spends the credit, and the dialog can still fail to
            # render or populate afterwards. Recording only on success let the
            # counter drift BELOW real spend, so the daily cap then authorised
            # extra unlocks on top of credits already burned.
            if credit_spent:
                _new_unlock_count = unlock_budget.record_unlock(UNLOCK_COUNTER_PATH, today)
                logger.info(
                    f"Unlock recorded: {_new_unlock_count}/{settings.max_unlocks_per_day} "
                    f"today ({candidate.profile_id}"
                    f"{'' if profile else ', EXTRACTION FAILED — credit spent for nothing'})"
                )
            if profile:
                # ============================================================
                # POST-UNLOCK GATE 0: Global Recruitee dedup (runs FIRST)
                # Skip if email exists ANYWHERE in Recruitee (any offer, any
                # status). Catches both manually-added candidates and people
                # the recruiter has already sourced for other jobs in the past.
                #
                # Runs before the distance gate so the talent-pool push below
                # never creates duplicates: if a candidate is already in
                # Recruitee (including the talent pool itself from a previous
                # cycle), they're skipped here and never reach the talent-pool
                # push branch.
                # ============================================================
                if (profile.email or profile.phone) and settings.recruitee_api_token:
                    already_exists, existing_candidate_id, existing_offer_ids = await check_candidate_exists_in_recruitee(
                        token=settings.recruitee_api_token,
                        company_id=settings.recruitee_company_id,
                        email=profile.email,
                        phone=profile.phone,
                        name=profile.name,
                    )
                    if already_exists:
                        logger.info(
                            f"  RECRUITEE DEDUP: {candidate.profile_id} already exists in Recruitee "
                            f"(candidate {existing_candidate_id}, placed on offers: {existing_offer_ids})"
                        )
                        profile.matched = True
                        profile.match_confidence = eval_result.confidence
                        profile.match_reasoning = (
                            f"Kandidat bereits in Recruitee vorhanden "
                            f"(ID: {existing_candidate_id}, frühere Stellen: {existing_offer_ids}). "
                            f"Übersprungen."
                        )
                        profile.unlocked = True
                        profile.unlock_reason = "already_in_recruitee"
                        profile.recruitee_status = "duplicate"
                        profile.cv_base64 = None
                        result.candidates.append(profile)
                        processed += 1
                        await human_delay(1000, 3000)
                        continue

                # ============================================================
                # POST-UNLOCK GATE 1: Distance safety net (FAIL-CLOSED)
                # If card-level Wohnort was missing or didn't geocode, try the
                # full profile text. If we STILL can't pin the candidate to a
                # German location with a valid distance, reject — but push to
                # the talent pool first (if configured) so the recruiter can
                # manually review borderline candidates.
                #
                # "Can't verify" includes BOTH cases:
                #   (a) extract_wohnadresse() returns nothing
                #   (b) extract_wohnadresse() returns an address that
                #       Nominatim cannot geocode within Germany — typically
                #       a foreign location like 'Sidi bennour' (Morocco).
                # Pre-2026-05-04 the code only handled (a) and silently let (b)
                # through, pushing foreign candidates to Recruitee.
                #
                # Reading distance_km == None as "the CANDIDATE is unlocatable"
                # is only sound because the pre-flight above proved the job's
                # own location geocodes. Without it the same None also meant
                # "the JOB's town is unresolvable", and this gate blamed the
                # candidate for it — filing Germans from Mannheim and Leuna
                # under 'Ausland' (prod 2026-07-15, offer 2468824). If you ever
                # remove the pre-flight, this inference breaks and the label
                # starts lying again.
                # ============================================================
                if distance_km is None:
                    post_unlock_addr = extract_wohnadresse(profile.profile_text) if profile.profile_text else None
                    if post_unlock_addr:
                        try:
                            distance_km = calculate_distance_km(post_unlock_addr, job.location)
                        except GeocoderUnavailable:
                            # The credit is spent: keep the profile and let the
                            # fail-closed gate below handle it, never lose it.
                            distance_km = None
                        logger.info(
                            f"  Post-unlock distance for {candidate.profile_id}: "
                            f"Wohnadresse={post_unlock_addr}, distance={distance_km}km"
                        )

                    if distance_km is None:
                        # Two sub-cases here, treated very differently:
                        #
                        #   (a) post_unlock_addr is None — we genuinely couldn't
                        #       extract any Wohnort. Umair's stated case: card
                        #       had no Wohnort, CV had no Wohnort, but the
                        #       workplace city in the CV may still match the
                        #       target. → talent pool for manual review.
                        #
                        #   (b) post_unlock_addr is set but didn't geocode in
                        #       Germany. The address was extracted but
                        #       Nominatim returned nothing for "<addr>,
                        #       Deutschland" — almost certainly a foreign
                        #       location (e.g. Moroccan postal code 24353
                        #       Sidi bennour). → hard reject, NO push: Umair
                        #       does not want foreign candidates in Recruitee
                        #       at all, including the talent pool.
                        if post_unlock_addr:
                            logger.warning(
                                f"  LOCATION UNGEOCODABLE {candidate.profile_id}: "
                                f"Wohnadresse={post_unlock_addr!r} did not geocode within "
                                f"Germany (likely foreign address). Job location "
                                f"{job.location!r} geocoded fine, so the candidate's "
                                f"address is what failed. Rejecting."
                            )
                            reason_text = (
                                f"ABGELEHNT: Wohnadresse {post_unlock_addr} konnte nicht "
                                f"in Deutschland verortet werden (vermutlich Ausland). "
                                f"Push in Rejected-Pipeline zur manuellen Sichtung."
                            )
                            # Route foreign rejects to the dedicated rejected
                            # pipeline too (labeled "Ausland"), BEFORE cv strip.
                            await _maybe_push_to_rejected_pipeline(
                                profile=profile,
                                original_offer_id=int(job.offer_id),
                                reason="Ausland",
                            )
                        else:
                            snippet = (profile.profile_text or "")[:1500].replace("\n", " | ")
                            logger.warning(
                                f"  LOCATION UNKNOWN {candidate.profile_id}: card had no Wohnort, "
                                f"and extract_wohnadresse() found nothing in profile_text. "
                                f"Snippet: {snippet}"
                            )
                            reason_text = (
                                "ABGELEHNT: Wohnort konnte weder aus dem Suchergebnis noch aus dem "
                                "vollen Profil ermittelt werden. Push in Talent Pool zur manuellen "
                                "Sichtung — Arbeitsort im Lebenslauf könnte zur Zielregion passen."
                            )
                            # Rejected-pipeline push BEFORE we strip cv_base64.
                            await _maybe_push_to_rejected_pipeline(
                                profile=profile,
                                original_offer_id=int(job.offer_id),
                                reason="Standort Unklar",
                            )
                        profile.matched = False
                        profile.match_confidence = eval_result.confidence
                        profile.match_reasoning = reason_text
                        profile.unlocked = True
                        profile.unlock_reason = "location_unknown"
                        profile.cv_base64 = None
                        result.candidates.append(profile)
                        processed += 1
                        await human_delay(1000, 3000)
                        continue

                    if distance_km > job.max_distance_km:
                        gewuenschte_post = extract_gewuenschte_arbeitsorte(profile.profile_text)
                        accepted_post, dist_reason_post = should_accept_far_candidate(
                            distance_km=distance_km,
                            relocation_max_km=settings.relocation_max_distance_km,
                            gewuenschte_arbeitsorte=gewuenschte_post,
                            job_location=job_location_base,
                        )
                        if not accepted_post:
                            # Too far. Either no relocation signal at all, OR
                            # the candidate is beyond the relocation feasibility
                            # cap (Suraj-style 120km Koch case — even with
                            # Apfeltrang listed as a desired location, the
                            # distance makes the relocation implausible).
                            # Either way: hard reject. Not a talent-pool case;
                            # the pool is for ambiguous-location candidates, not
                            # for ones we know live too far away.
                            too_far_for_relocate_post = (dist_reason_post == DIST_TOO_FAR_FOR_RELOCATION)
                            if too_far_for_relocate_post:
                                reasoning_post = (
                                    f"ABGELEHNT (nach Unlock): Wohnadresse {post_unlock_addr} liegt "
                                    f"{distance_km:.0f}km von {job.location} entfernt — über dem "
                                    f"Umzugslimit von {settings.relocation_max_distance_km}km. "
                                    f"Auch mit Umzugswunsch ist die Entfernung nicht realistisch."
                                )
                                unlock_reason_post = "too_far_for_relocation_post_unlock"
                            else:
                                reasoning_post = (
                                    f"ABGELEHNT (nach Unlock): Wohnadresse {post_unlock_addr} liegt "
                                    f"{distance_km:.0f}km von {job.location} entfernt "
                                    f"(Maximum: {job.max_distance_km}km). "
                                    f"Keine Umzugsbereitschaft erkennbar."
                                )
                                unlock_reason_post = "too_far_post_unlock"
                            logger.info(
                                f"  POST-UNLOCK REJECTED {candidate.profile_id} ({dist_reason_post}): "
                                f"{post_unlock_addr} is {distance_km:.0f}km from {job.location} "
                                f"(max {job.max_distance_km}km, relocation cap "
                                f"{settings.relocation_max_distance_km}km)"
                            )
                            profile.matched = False
                            profile.match_confidence = eval_result.confidence
                            profile.match_reasoning = reasoning_post
                            profile.unlocked = True
                            profile.unlock_reason = unlock_reason_post
                            # Route too-far rejects to the dedicated rejected
                            # pipeline (reason "Aus Radius") BEFORE cv strip.
                            # Covers both too_far_post_unlock and
                            # too_far_for_relocation_post_unlock.
                            await _maybe_push_to_rejected_pipeline(
                                profile=profile,
                                original_offer_id=int(job.offer_id),
                                reason="Aus Radius",
                            )
                            profile.cv_base64 = None
                            result.candidates.append(profile)
                            processed += 1
                            await human_delay(1000, 3000)
                            continue

                # ============================================================
                # All gates passed — push to Recruitee
                # ============================================================
                profile.matched = True
                profile.match_confidence = eval_result.confidence
                profile.match_reasoning = eval_result.reasoning

                if settings.recruitee_api_token:
                    await _push_to_recruitee(
                        profile=profile,
                        offer_id=int(job.offer_id),
                        stage_id=int(job.stage_id),
                        token=settings.recruitee_api_token,
                        company_id=settings.recruitee_company_id,
                    )

                # Strip cv_base64 — uploaded to Recruitee; don't include in webhook.
                profile.cv_base64 = None

                result.candidates.append(profile)
            else:
                result.candidates.append(
                    CandidateResult(
                        name="",
                        stepstone_profile_id=candidate.profile_id,
                        matched=True,
                        match_confidence=eval_result.confidence,
                        match_reasoning=eval_result.reasoning,
                        # Charged but no details: still "unlocked" for n8n, so
                        # the credit lands in the Credit Ledger and the counts.
                        unlocked=bool(credit_spent),
                        unlock_reason="profile_extraction_failed",
                        credit_spent=bool(credit_spent),
                        account_used=account_label,
                    )
                )
                if consecutive_unlock_failures >= MAX_CONSECUTIVE_UNLOCK_FAILURES:
                    # Something is systematically wrong with unlocking. Do not
                    # try it on every remaining match (each may cost a credit).
                    result.partial = True
                    result.error = (
                        f"UNLOCK_FAILING: {consecutive_unlock_failures} unlocks in a row returned no "
                        f"candidate details, so the job stopped before trying more. Credits StepStone "
                        f"may have taken are recorded. Send the Railway logs to Apex; do not re-run."
                    )
                    logger.error(result.error)
                    processed += 1
                    break

            processed += 1
            await human_delay(1000, 3000)

    except Exception as e:
        logger.error(f"Scrape error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        result.partial = True
        # A crash is an abort, not a "found nothing": it must reach the webhook
        # payload, where n8n's aborted branch can see it. current_status is only
        # visible on /status, which nothing polls. Keep an earlier, more precise
        # abort reason (e.g. the eval-error threshold) if one was already set.
        if not result.error and isinstance(e, AuthenticationError) and e.code == "SESSION_REUSE_TIMEOUT":
            result.error = str(e)  # "SESSION_REUSE_TIMEOUT: ..." (no password was typed)
        if not result.error and isinstance(e, GeocoderUnavailable):
            # Mid-run: a candidate's distance could not be checked. Stopped
            # rather than unlocking someone whose distance is unknown.
            result.error = (
                f"GEOCODER_UNAVAILABLE: the map lookup service stopped answering during "
                f"the run, so it was stopped before unlocking anyone whose distance could "
                f"not be checked. Temporary; credits already spent are recorded. ({e})"
            )
        if not result.error:
            result.error = f"Scrape crashed: {type(e).__name__}: {e}"
        current_status["error"] = str(e)
    finally:
        if context is not None and authenticated_email:
            # Cookies rotate during a job; the next job must start from the
            # newest ones, or it may find the session dead and type the password.
            try:
                save_session(authenticated_email, await asyncio.wait_for(context.cookies(), 20))
            except Exception as e:
                logger.warning(f"Could not save the session after the job: {type(e).__name__}")
        if browser:
            try:
                await asyncio.wait_for(close_browser(browser), 30)
            except Exception as e:
                logger.warning(f"Closing the browser did not finish: {type(e).__name__}")

    return result


@app.post("/scrape", status_code=202)
async def scrape(job: JobInput, background_tasks: BackgroundTasks):
    if scrape_lock.locked():
        raise HTTPException(409, detail="Scrape already in progress")

    async def locked_scrape():
        global _current_result
        async with scrape_lock:
            _current_result = None
            try:
                result = await asyncio.wait_for(run_scrape(job), timeout=JOB_HARD_LIMIT_S)
            except Exception as e:
                # A hang (timeout) or a crash outside run_scrape's own handler:
                # n8n must still hear about the job, or its chain waits forever.
                logger.error(f"Job ended without a result: {type(e).__name__}: {e}")
                reason = ("JOB_HARD_TIMEOUT: the job hung and was stopped after "
                          f"{JOB_HARD_LIMIT_S // 60} min" if isinstance(e, asyncio.TimeoutError)
                          else f"Scrape crashed: {type(e).__name__}: {e}")
                # Keep what the job already did (unlocks, Recruitee pushes): n8n
                # must log those credits and candidates even when the job is killed.
                result = _current_result or ScrapeResult(
                    offer_id=job.offer_id, stage_id=job.stage_id, job_title=job.job_title,
                    location=job.location, requirements=job.requirements, account_used="",
                )
                result.partial = True
                result.error = reason
        # Lock released here — n8n's chain-dispatch will now get 202, not 409.
        current_status["state"] = "idle"
        current_status["job"] = None
        logger.info(f"Sending {len(result.candidates)} candidates to webhook")
        await send_webhook(settings.n8n_webhook_url, result)
        # Update status with any webhook error (doesn't re-acquire lock)
        if current_status.get("error") is None:
            current_status["error"] = None

    background_tasks.add_task(locked_scrape)
    return {"status": "accepted", "job_title": job.job_title, "location": job.location}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return current_status

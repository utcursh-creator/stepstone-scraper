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
from utils.geocode import (
    clear_cache,
    extract_wohnadresse,
    extract_gewuenschte_arbeitsorte,
    calculate_distance_km,
    check_desired_location_match,
)
from utils.openrouter import evaluate_candidate
from utils.recruitee import create_candidate, upload_cv, set_stage, check_candidate_exists_on_offer, RecruiteeError
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


async def _push_to_recruitee(
    profile: "CandidateResult",
    offer_id: int,
    stage_id: int,
    token: str,
    company_id: str,
) -> None:
    """Create candidate in Recruitee, upload CV, set Gesourct stage.

    All three steps update profile in-place. Failures are non-fatal:
    we log errors and set recruitee_status='failed' so n8n can skip
    the Recruitee steps for this candidate.
    """
    # Step 1: Create candidate + link to offer
    try:
        candidate_id, placement_id = await create_candidate(
            token=token,
            company_id=company_id,
            name=profile.name,
            emails=[profile.email] if profile.email else [],
            phones=[profile.phone] if profile.phone else [],
            offer_id=offer_id,
        )
        profile.recruitee_candidate_id = candidate_id
        profile.recruitee_placement_id = placement_id
        profile.recruitee_status = "created"
    except RecruiteeError as e:
        logger.error(f"Recruitee create_candidate failed for {profile.stepstone_profile_id}: {e}")
        profile.recruitee_status = "failed"
        return  # Skip CV upload and stage set if creation failed

    # Step 2: Upload CV (non-fatal if cv_base64 missing or upload fails)
    if profile.cv_base64:
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
                f"CV upload failed for Recruitee candidate {candidate_id}; continuing to stage set"
            )
    else:
        logger.info(
            f"No cv_base64 for profile {profile.stepstone_profile_id}; skipping CV upload"
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


async def run_scrape(job: JobInput) -> ScrapeResult:
    """Main scrape orchestrator. Returns ScrapeResult without sending webhook.

    The caller is responsible for sending the webhook AFTER releasing the
    concurrency lock, so n8n's chain-dispatch doesn't hit a 409.
    """
    global current_status
    clear_cache()  # Reset geocoding cache for this job
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
        for c in candidates:
            logger.info(f"  card {c.profile_id}: preview_text={len(c.preview_text)} chars, cv_url={'yes' if c.cv_url else 'no'}")

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

            if distance_km is not None and distance_km > job.max_distance_km:
                desired_match = check_desired_location_match(gewuenschte_str, job.location)
                if not desired_match:
                    logger.info(
                        f"  REJECTED {candidate.profile_id}: "
                        f"Wohnort {wohnort} is {distance_km:.0f}km from {job.location} "
                        f"(max {job.max_distance_km}km, no desired location match)"
                    )
                    result.candidates.append(
                        CandidateResult(
                            name="",
                            stepstone_profile_id=candidate.profile_id,
                            matched=False,
                            match_confidence=0.0,
                            match_reasoning=(
                                f"ABGELEHNT: Wohnort {wohnort} liegt {distance_km:.0f}km "
                                f"von {job.location} entfernt (Maximum: {job.max_distance_km}km). "
                                f"Keine Umzugsbereitschaft erkennbar."
                            ),
                            unlocked=False,
                            unlock_reason="too_far",
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
                api_key=settings.openrouter_api_key,
                candidate_text=candidate.preview_text,
                job_title=job.job_title,
                location=job.location,
                requirements=job.requirements,
                distance_km=distance_km,
                wohnadresse=wohnort,
                gewuenschte_arbeitsorte=gewuenschte_str,
                max_distance_km=job.max_distance_km,
            )
            logger.info(f"  eval match={eval_result.match} conf={eval_result.confidence} reason={eval_result.reasoning[:150]}")
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
            profile = await extract_profile(
                page,
                candidate.profile_id,
                account_label,
                preview_cv_url=getattr(candidate, "cv_url", ""),
            )
            if profile:
                # ============================================================
                # POST-UNLOCK GATE 1: Distance safety net
                # For the ~10% of cards where Wohnort was not visible pre-unlock,
                # the full profile text contains Wohnadresse. Check it now.
                # Credits are already spent, but we prevent distant candidates
                # from reaching Recruitee.
                # ============================================================
                if distance_km is None and profile.profile_text:
                    post_unlock_addr = extract_wohnadresse(profile.profile_text)
                    if post_unlock_addr:
                        distance_km = calculate_distance_km(post_unlock_addr, job.location)
                        logger.info(
                            f"  Post-unlock distance for {candidate.profile_id}: "
                            f"Wohnadresse={post_unlock_addr}, distance={distance_km}km"
                        )
                        if distance_km is not None and distance_km > job.max_distance_km:
                            gewuenschte_post = extract_gewuenschte_arbeitsorte(profile.profile_text)
                            desired_match = check_desired_location_match(
                                gewuenschte_post, job.location
                            )
                            if not desired_match:
                                logger.info(
                                    f"  POST-UNLOCK REJECTED {candidate.profile_id}: "
                                    f"{post_unlock_addr} is {distance_km:.0f}km from {job.location} "
                                    f"(max {job.max_distance_km}km)"
                                )
                                profile.matched = False
                                profile.match_confidence = eval_result.confidence
                                profile.match_reasoning = (
                                    f"ABGELEHNT (nach Unlock): Wohnadresse {post_unlock_addr} liegt "
                                    f"{distance_km:.0f}km von {job.location} entfernt "
                                    f"(Maximum: {job.max_distance_km}km). "
                                    f"Keine Umzugsbereitschaft erkennbar."
                                )
                                profile.unlocked = True
                                profile.unlock_reason = "too_far_post_unlock"
                                profile.cv_base64 = None
                                result.candidates.append(profile)
                                processed += 1
                                await human_delay(1000, 3000)
                                continue

                # ============================================================
                # POST-UNLOCK GATE 2: Recruitee email dedup
                # Catches candidates who were manually added to Recruitee by
                # recruiters (not in our Airtable dedup table). Prevents
                # duplicate entries on the same offer.
                # ============================================================
                if profile.email and settings.recruitee_api_token:
                    already_exists, existing_candidate_id = await check_candidate_exists_on_offer(
                        token=settings.recruitee_api_token,
                        company_id=settings.recruitee_company_id,
                        email=profile.email,
                        offer_id=int(job.offer_id),
                    )
                    if already_exists:
                        logger.info(
                            f"  RECRUITEE DEDUP: {candidate.profile_id} ({profile.email}) "
                            f"already exists on offer {job.offer_id} as candidate {existing_candidate_id}"
                        )
                        profile.matched = True
                        profile.match_confidence = eval_result.confidence
                        profile.match_reasoning = (
                            f"Kandidat bereits in Recruitee vorhanden "
                            f"(ID: {existing_candidate_id}). Übersprungen."
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
                        unlocked=False,
                        unlock_reason="profile_extraction_failed",
                        account_used=account_label,
                    )
                )

            processed += 1
            await human_delay(1000, 3000)

    except Exception as e:
        logger.error(f"Scrape error: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        result.partial = True
        current_status["error"] = str(e)
    finally:
        if browser:
            await close_browser(browser)

    return result


@app.post("/scrape", status_code=202)
async def scrape(job: JobInput, background_tasks: BackgroundTasks):
    if scrape_lock.locked():
        raise HTTPException(409, detail="Scrape already in progress")

    async def locked_scrape():
        async with scrape_lock:
            result = await run_scrape(job)
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

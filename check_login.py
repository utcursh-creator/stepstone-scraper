"""One-off check: can the scraper log in to Stepstone Recruit from here?

Costs no StepStone credits. It logs in exactly the way a job does (the same
headless Chromium and the same proxy set-up as scraper/browser.py), prints the
result and the credit balance, and exits. It keeps its own saved session (in
~/.cache, never the scraper's sessions/ folder) and reuses it on the next run, so
repeated checks do not keep typing the password: fresh logins are what tripped
StepStone's bot check on 2026-09-25. --fresh forces a new login.

    railway run .venv/bin/python check_login.py       # with Railway's variables
    .venv/bin/python check_login.py                     # with a local .env

Options:  --inspect     after logging in, open Talent Finder and report (structure
                        only, no search, no unlock) how the app calls its API
                        and how the search form is built; saves a JSON file
          --search "Physiotherapeut" --location Hamburg [--distance 25]
                        after logging in, run ONE real Talent Finder search
                        (first page only, nothing unlocked) and print counts
          --fresh       forget the saved session and log in from scratch
          --account 2   check the second account
          --no-proxy    log in without the proxy (to tell a proxy problem apart)
          --headed      show the browser window

Exit code: 0 logged in, 1 login failed (the code says why), 2 misconfigured.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    """Read a variable the way the scraper's settings do: case-insensitively.
    Railway holds several of this service's variables in lowercase
    (stepstone_email_1, proxy_host, ...); pydantic-settings accepts either, so
    this check must too, or it reports 'not set' for a perfectly good config."""
    if name in os.environ:
        return os.environ[name].strip()
    wanted = name.lower()
    for key, value in os.environ.items():
        if key.lower() == wanted:
            return value.strip()
    return default


async def run_check(context, page, email: str, password: str) -> tuple[bool, str]:
    """Log in once and describe the outcome in one line. Never includes the password."""
    from scraper import auth

    try:
        credits = await auth.authenticate(context, page, email, password)
        return True, f"LOGGED IN as {email}: {auth._balance(credits)}"
    except auth.AuthenticationError as e:
        return False, f"FAILED [{e.code}] {e}"


_FORM_JS = """
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const chain = (e) => { const out = []; let x = e.parentElement;
    for (let i = 0; i < 8 && x; i++, x = x.parentElement) { const t = x.getAttribute('data-testid'); if (t) out.push(t); }
    return out; };
  const attrs = (e) => ({ tag: e.tagName.toLowerCase(), type: e.getAttribute('type'), name: e.getAttribute('name'),
    role: e.getAttribute('role'), placeholder: e.getAttribute('placeholder'), aria_label: e.getAttribute('aria-label'),
    testid: e.getAttribute('data-testid'), genesis: e.getAttribute('data-genesis-element'),
    autocomplete: e.getAttribute('aria-autocomplete'), text: (e.innerText || '').trim().slice(0, 40),
    ancestor_testids: chain(e) });
  return {
    path: location.pathname,
    inputs: [...document.querySelectorAll('input, textarea, [contenteditable="true"], [role="combobox"]')].filter(vis).map(attrs),
    buttons: [...document.querySelectorAll('button, [role="button"]')].filter(vis).map(attrs).slice(0, 40),
    forms: [...document.querySelectorAll('form')].map((f) => ({ testid: f.getAttribute('data-testid'),
      role: f.getAttribute('role'), inputs: f.querySelectorAll('input').length })),
    distance_texts: [...document.querySelectorAll('label, span, div, option, button')]
      .filter((e) => vis(e) && e.children.length === 0 && /umkreis|entfernung|radius|\\bkm\\b/i.test(e.innerText || ''))
      .map((e) => (e.innerText || '').trim().slice(0, 60)).slice(0, 20),
  };
}
"""


def _mask(text: str) -> str:
    return re.sub(r"[A-Za-z0-9_-]{24,}", "{id}", text)


async def inspect_after_login(page) -> dict:
    """What the search build needs to know, read on the REAL Talent Finder page
    after login: which headers the app's own API calls carry (names only, never
    values), whether a request WE make is accepted (from patchright's isolated
    world and from the page's own world), the session cookie names, and how the
    search form is built. No search is run and nothing is unlocked; the Talent
    Finder start page holds no candidate data."""
    from scraper import auth

    seen = []

    def on_request(request):
        p = urlparse(request.url)
        if (p.hostname or "") == auth.APP_HOST and p.path.startswith("/recruiter/talent-sourcing/api/"):
            seen.append(request)

    page.on("request", on_request)
    await page.goto(auth.TALENT_FINDER_URL, wait_until="domcontentloaded")
    await asyncio.sleep(8)  # let the app make its own start-up calls
    page.remove_listener("request", on_request)

    app_calls = []
    for request in seen:
        try:
            names = sorted(await request.all_headers())
            response = await request.response()
            app_calls.append({"method": request.method, "path": urlparse(request.url).path,
                              "via": request.resource_type, "status": response.status if response else None,
                              "header_names": names})
        except Exception as e:
            app_calls.append({"path": urlparse(request.url).path, "error": type(e).__name__})

    async def own(isolated: bool) -> str:
        try:
            res = await page.evaluate(auth._CREDITS_JS, auth.CREDITS_PATH, isolated_context=isolated)
            return str(res.get("status")) if isinstance(res, dict) else "no result"
        except Exception as e:
            return f"failed ({type(e).__name__})"

    report = {
        "app_api_calls": app_calls,
        "our_balance_request_from_isolated_world": await own(True),
        "our_balance_request_from_page_world": await own(False),
        "app_host_cookie_names": sorted(
            _mask(c["name"]) + (" [HttpOnly]" if c.get("httpOnly") else "")
            for c in await page.context.cookies(f"https://{auth.APP_HOST}")),
        "search_form": await page.evaluate(_FORM_JS),
    }
    try:  # the "Erweitert" panel: where a distance / radius setting would live
        advanced = page.get_by_role("button", name=re.compile(r"erweitert", re.IGNORECASE)).first
        if await advanced.count():
            await advanced.click(timeout=5000)
            await asyncio.sleep(2)
            report["search_form_with_erweitert_open"] = await page.evaluate(_FORM_JS)
    except Exception as e:
        report["erweitert"] = f"could not open ({type(e).__name__})"
    return report


ATTEMPT_LOG = os.path.join(os.path.expanduser("~"), ".cache", "stepstone-login-check.json")
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".cache", "stepstone-login-check-sessions")
MAX_ATTEMPTS_PER_HOUR = 2


def _recent_attempts(now: float) -> list[float]:
    try:
        with open(ATTEMPT_LOG) as f:
            return [t for t in json.load(f) if now - t < 3600]
    except (OSError, ValueError):
        return []


def _record_attempt(now: float) -> None:
    attempts = _recent_attempts(now) + [now]
    os.makedirs(os.path.dirname(ATTEMPT_LOG), exist_ok=True)
    with open(ATTEMPT_LOG, "w") as f:
        json.dump(attempts, f)


def search_summary(outcome) -> dict:
    """Counts only. No names, no ids, no candidate text leave this function."""
    rs = outcome.first_page
    n = len(rs) or 1
    return {
        "stepstone_total": outcome.total,
        "pages_available": outcome.total_pages,
        "keyword_sent": outcome.keyword,
        "location_sent": outcome.location_name,
        "radius_sent_km": outcome.radius_km or "Optimiert",
        "keyword_fallback_used": outcome.keyword_fallback,
        "first_page_results": len(rs),
        "with_postcode": sum(1 for r in rs if r.postal_code),
        "with_cv": sum(1 for r in rs if r.has_cv_attachment),
        "still_locked": sum(1 for r in rs if r.is_locked),
        "already_unlocked": sum(1 for r in rs if not r.is_locked),
        "with_languages": sum(1 for r in rs if r.languages),
        "with_desired_locations": sum(1 for r in rs if r.gewuenschte_arbeitsorte),
        "avg_llm_text_chars": round(sum(len(r.preview_text) for r in rs) / n),
        "records_without_id_skipped": outcome.skipped_records,
    }


def _parse(argv):
    p = argparse.ArgumentParser(description="Check the Stepstone Recruit login (no credits spent).")
    p.add_argument("--account", type=int, choices=(1, 2), default=1)
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--search", default="", help="job title for one live search (first page only)")
    p.add_argument("--location", default="", help="location for --search")
    p.add_argument("--distance", type=int, default=25, help="max distance in km for --search")
    p.add_argument("--fresh", action="store_true", help="forget the saved session and log in from scratch")
    p.add_argument("--force", action="store_true",
                   help="ignore the limit of 2 fresh logins per hour (risks an account lock)")
    p.add_argument("--no-proxy", action="store_true")
    p.add_argument("--headed", action="store_true")
    return p.parse_args(argv)


async def main(argv=None) -> int:
    load_dotenv()  # never overrides variables already set (e.g. by `railway run`)
    args = _parse(argv)
    n = args.account
    email = _env(f"STEPSTONE_EMAIL_{n}")
    password = _env(f"STEPSTONE_PASS_{n}")
    if not email or not password:
        print(f"STEPSTONE_EMAIL_{n} and STEPSTONE_PASS_{n} must both be set.", file=sys.stderr)
        return 2
    if args.search and not args.location:
        print("--search needs --location.", file=sys.stderr)
        return 2
    if not args.no_proxy:
        missing = [v for v in ("PROXY_HOST", "PROXY_PORT", "PROXY_USER", "PROXY_PASS") if not _env(v)]
        if missing:
            print(f"Missing proxy variables: {', '.join(missing)} (or pass --no-proxy).", file=sys.stderr)
            return 2

    # Every run is a FRESH login from a new proxy IP. Three of those in ~25 min
    # tripped StepStone's bot check on 2026-09-25, and repeated tries are how
    # an account gets locked. Checked before any browser opens.
    now = time.time()
    recent = _recent_attempts(now)
    will_type_password = args.fresh or not os.path.exists(os.path.join(SESSION_DIR, f"account-{n}.json"))
    if will_type_password and len(recent) >= MAX_ATTEMPTS_PER_HOUR and not args.force:
        wait_min = int((min(recent) + 3600 - now) / 60) + 1
        print(f"Refusing: {len(recent)} fresh logins already in the last hour. StepStone's bot "
              f"check trips on repeated logins from changing IPs. Try again in ~{wait_min} min "
              f"(or --force, at the risk of an account lock).", file=sys.stderr)
        return 2
    if will_type_password:
        _record_attempt(now)

    from scraper import auth
    from scraper.browser import create_browser

    # The check's own session store: kept between runs, away from sessions/.
    session_file = os.path.join(SESSION_DIR, f"account-{n}.json")
    if args.fresh and os.path.exists(session_file):
        os.remove(session_file)
    auth._session_path = lambda _email: session_file

    if args.no_proxy:
        from patchright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=not args.headed, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}, locale="de-DE", timezone_id="Europe/Berlin")
        page = await context.new_page()
        page.set_default_navigation_timeout(120_000)
    else:
        pw = None
        browser, context, page = await create_browser(
            proxy_host=_env("PROXY_HOST"),
            proxy_port=int(_env("PROXY_PORT")),
            proxy_user=_env("PROXY_USER"),
            proxy_pass=_env("PROXY_PASS"),
            proxy_country=_env("PROXY_COUNTRY", "DE"),
        )

    # Show every step on the REAL site: the login module's own log lines (cookie
    # banner, session, balance) and each page the browser lands on. URLs are cut
    # to host + path, because the login URL carries one-time state/PKCE values.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    page.on("framenavigated", lambda frame: frame == page.main_frame
            and print(f"  page -> {auth._safe_url(frame.url)}"))

    route = "without the proxy" if args.no_proxy else "through the proxy"
    print(f"Checking the Stepstone Recruit login for {email} {route} (up to ~2 minutes)...")
    started = time.monotonic()
    try:
        ok, line = await run_check(context, page, email, password)
        print(f"{line}  [{time.monotonic() - started:.0f}s]")
        if ok and args.search:
            from scraper.talent_search import SearchError, search_talents
            print(f"Searching Talent Finder: {args.search!r} in {args.location!r} "
                  f"(max {args.distance} km; first page only, nothing unlocked)...")
            try:
                outcome = await search_talents(page, args.search, args.location,
                                               max_distance_km=args.distance, max_pages=1)
                print(json.dumps(search_summary(outcome), indent=1, ensure_ascii=False))
            except SearchError as e:
                print(f"SEARCH FAILED [{e.code}] {e}")
                ok = False
        if ok and args.inspect:
            print("Inspecting Talent Finder (no search, no unlock)...")
            report = await inspect_after_login(page)
            out = os.path.abspath(f"login-inspect-{int(time.time())}.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1, ensure_ascii=False)
            print(json.dumps(report, indent=1, ensure_ascii=False))
            print(f"Saved: {out}")
        if not ok:
            shot = os.path.abspath(f"login-check-{int(time.time())}.png")
            try:
                await page.screenshot(path=shot, full_page=True)
                print(f"Screenshot of the page when it failed (local file, do not share if it shows the email): {shot}")
            except Exception:
                pass
        return 0 if ok else 1
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        if pw is not None:
            await pw.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

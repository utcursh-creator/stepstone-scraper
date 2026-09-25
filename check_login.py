"""One-off check: can the scraper log in to Stepstone Recruit from here?

Costs no StepStone credits. It logs in exactly the way a job does (the same
headless Chromium and the same proxy set-up as scraper/browser.py), prints the
result and the credit balance, and exits. It never reads or overwrites the
scraper's saved sessions: it always does a fresh login, which is the path that
matters.

    railway run .venv/bin/python check_login.py       # with Railway's variables
    .venv/bin/python check_login.py                     # with a local .env

Options:  --account 2   check the second account
          --no-proxy    log in without the proxy (to tell a proxy problem apart)
          --headed      show the browser window

Exit code: 0 logged in, 1 login failed (the code says why), 2 misconfigured.
"""
import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time

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


def _parse(argv):
    p = argparse.ArgumentParser(description="Check the Stepstone Recruit login (no credits spent).")
    p.add_argument("--account", type=int, choices=(1, 2), default=1)
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
    if not args.no_proxy:
        missing = [v for v in ("PROXY_HOST", "PROXY_PORT", "PROXY_USER", "PROXY_PASS") if not _env(v)]
        if missing:
            print(f"Missing proxy variables: {', '.join(missing)} (or pass --no-proxy).", file=sys.stderr)
            return 2

    from scraper import auth
    from scraper.browser import create_browser

    # Keep the check's session away from the scraper's own sessions/ folder.
    scratch = tempfile.mkdtemp(prefix="login-check-")
    auth._session_path = lambda _email: os.path.join(scratch, "session.json")

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

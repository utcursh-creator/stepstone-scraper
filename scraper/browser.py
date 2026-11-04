import asyncio
import base64
import hashlib
import uuid
from datetime import date

from patchright.async_api import async_playwright, Browser, BrowserContext, Page


def proxy_password(proxy_pass: str, proxy_country: str, sticky_key: str | None,
                   day: date | None = None) -> str:
    """IPRoyal modifiers go on the PASSWORD: pass_country-de_session-XXX_lifetime-24h.

    With a sticky_key (the account's email) the session id is fixed for that
    account for the day, so every job of the day leaves from the SAME
    residential IP. A random 10-minute session per job meant a new IP for every
    login, and fresh logins from ever-changing IPs are what StepStone's bot
    check reacts to (CAPTCHA, 2026-09-28). Without a key: a one-off session.
    """
    if sticky_key:
        day = day or date.today()
        session_id = hashlib.sha256(f"{sticky_key.lower()}|{day.isoformat()}".encode()).hexdigest()[:12]
        lifetime = "24h"
    else:
        session_id, lifetime = uuid.uuid4().hex[:12], "10m"
    return f"{proxy_pass}_country-{proxy_country.lower()}_session-{session_id}_lifetime-{lifetime}"


def _add_proxy_login(head: bytes, login: bytes) -> bytes:
    """Put the login on a request head. A plain http:// request also gets
    Connection: close, so the next one arrives on a new connection and gets the
    login too, instead of being passed along without it."""
    request_line, *headers = head.rstrip(b"\r\n").split(b"\r\n")
    added = [b"Proxy-Authorization: Basic " + login]
    if not request_line.startswith(b"CONNECT "):
        headers = [h for h in headers if not h.lower().startswith((b"connection:", b"proxy-connection:"))]
        added.append(b"Connection: close")
    return b"\r\n".join([request_line, *added, *headers]) + b"\r\n\r\n"


async def _pipe(reader, writer) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _start_proxy_forwarder(proxy_host: str, proxy_port: int, proxy_user: str, composed_pass: str):
    """Local forwarder that sends the proxy login with the FIRST request.

    Chromium only sends proxy credentials after a 407. From some source IPs
    IPRoyal answers a request without a login with 402 instead, and Chromium
    gives up with ERR_TUNNEL_CONNECTION_FAILED before any page loads (Railway,
    2026-11-04). The same login sent up front gets a 200 from the same place.
    """
    login = base64.b64encode(f"{proxy_user}:{composed_pass}".encode())

    async def handle(client_reader, client_writer):
        upstream_writer = None
        try:
            head = await client_reader.readuntil(b"\r\n\r\n")
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy_host, proxy_port), 30
            )
            upstream_writer.write(_add_proxy_login(head, login))
            await upstream_writer.drain()
            await asyncio.gather(
                _pipe(client_reader, upstream_writer),
                _pipe(upstream_reader, client_writer),
            )
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            pass
        finally:
            client_writer.close()
            if upstream_writer is not None:
                upstream_writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def create_browser(
    proxy_host: str,
    proxy_port: int,
    proxy_user: str,
    proxy_pass: str,
    proxy_country: str = "DE",
    sticky_key: str | None = None,
) -> tuple[Browser, BrowserContext, Page]:
    """Launch a stealth Patchright browser with IPRoyal residential proxy."""
    p = await async_playwright().start()

    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )

    composed_pass = proxy_password(proxy_pass, proxy_country, sticky_key)
    browser._stepstone_driver = p
    try:
        forwarder = await _start_proxy_forwarder(proxy_host, proxy_port, proxy_user, composed_pass)
        browser._stepstone_forwarder = forwarder
        context, page = await _open_context(browser, forwarder.sockets[0].getsockname()[1])
    except Exception:
        await close_browser(browser)
        raise
    return browser, context, page


async def _open_context(browser, forwarder_port):
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="de-DE",
        timezone_id="Europe/Berlin",
        proxy={"server": f"http://127.0.0.1:{forwarder_port}"},
    )

    page = await context.new_page()
    page.set_default_navigation_timeout(120_000)
    return context, page


async def close_browser(browser: Browser) -> None:
    """Safely close the browser, then stop its Playwright driver (left running,
    each job leaked one node process: 13 per full run)."""
    try:
        await browser.close()
    except Exception:
        pass
    forwarder = getattr(browser, "_stepstone_forwarder", None)
    if forwarder is not None:
        forwarder.close()
    driver = getattr(browser, "_stepstone_driver", None)
    if driver is not None:
        try:
            await driver.stop()
        except Exception:
            pass

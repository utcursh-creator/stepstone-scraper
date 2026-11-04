import asyncio
import base64
from datetime import date

from scraper.browser import _add_proxy_login, _start_proxy_forwarder, proxy_password


def test_every_job_of_the_day_uses_the_same_ip_for_an_account():
    a = proxy_password("pw", "DE", "Recruiter@Example.test", date(2026, 9, 30))
    b = proxy_password("pw", "DE", "recruiter@example.test", date(2026, 9, 30))
    assert a == b and a.startswith("pw_country-de_session-") and a.endswith("_lifetime-24h")


def test_accounts_and_days_get_their_own_ip():
    d = date(2026, 9, 30)
    assert proxy_password("pw", "DE", "a@example.test", d) != proxy_password("pw", "DE", "b@example.test", d)
    assert proxy_password("pw", "DE", "a@example.test", d) != proxy_password("pw", "DE", "a@example.test", date(2026, 10, 1))


def test_without_an_account_a_one_off_session_is_used():
    assert proxy_password("pw", "DE", None).endswith("_lifetime-10m")


async def _fake_proxy(login_needed: bytes):
    """Answers like IPRoyal does from Railway: 402 (not 407) without the login,
    200 with it, and then echoes whatever comes through the tunnel."""
    heads = []

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head)
        if b"Proxy-Authorization: Basic " + login_needed + b"\r\n" not in head:
            writer.write(b"HTTP/1.1 402 Payment Required\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], heads


CONNECT = b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n"


async def test_the_first_connect_already_carries_the_proxy_login():
    login = base64.b64encode(b"user:pw_country-de")
    server, port, heads = await _fake_proxy(login)
    forwarder = await _start_proxy_forwarder("127.0.0.1", port, "user", "pw_country-de")
    reader, writer = await asyncio.open_connection("127.0.0.1", forwarder.sockets[0].getsockname()[1])

    writer.write(CONNECT)
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    assert len(heads) == 1

    writer.write(b"through the tunnel")
    await writer.drain()
    assert await reader.readexactly(18) == b"through the tunnel"

    writer.close()
    forwarder.close()
    server.close()


async def test_a_refusal_from_the_proxy_reaches_the_browser():
    server, port, _ = await _fake_proxy(b"a-different-login")
    forwarder = await _start_proxy_forwarder("127.0.0.1", port, "user", "pw")
    reader, writer = await asyncio.open_connection("127.0.0.1", forwarder.sockets[0].getsockname()[1])

    writer.write(CONNECT)
    await writer.drain()
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 402")

    writer.close()
    forwarder.close()
    server.close()


def test_a_connect_keeps_its_headers_and_gains_the_login():
    head = _add_proxy_login(CONNECT, b"bG9naW4=")
    assert head == (b"CONNECT example.test:443 HTTP/1.1\r\n"
                    b"Proxy-Authorization: Basic bG9naW4=\r\n"
                    b"Host: example.test:443\r\n\r\n")


def test_a_plain_http_request_closes_so_the_next_one_also_gets_the_login():
    head = _add_proxy_login(
        b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n"
        b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n",
        b"bG9naW4=",
    )
    assert head == (b"GET http://example.test/ HTTP/1.1\r\n"
                    b"Proxy-Authorization: Basic bG9naW4=\r\n"
                    b"Connection: close\r\n"
                    b"Host: example.test\r\n\r\n")

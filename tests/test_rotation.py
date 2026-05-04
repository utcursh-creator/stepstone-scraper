import json
import os
import tempfile
from scraper.rotation import (
    next_account,
    read_counter,
    resolve_requested_account,
    select_account,
    write_counter,
)


ACCOUNTS = [
    {"email": "a@test.com", "password": "pw1"},
    {"email": "b@test.com", "password": "pw2"},
]


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
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        a1 = next_account(ACCOUNTS, path)
        a2 = next_account(ACCOUNTS, path)
        a3 = next_account(ACCOUNTS, path)
        assert a1["email"] == "a@test.com"
        assert a2["email"] == "b@test.com"
        assert a3["email"] == "a@test.com"


def test_resolve_by_email_case_insensitive():
    assert resolve_requested_account(ACCOUNTS, "A@test.com")["email"] == "a@test.com"
    assert resolve_requested_account(ACCOUNTS, "b@TEST.com")["email"] == "b@test.com"


def test_resolve_by_label():
    assert resolve_requested_account(ACCOUNTS, "Account 1")["email"] == "a@test.com"
    assert resolve_requested_account(ACCOUNTS, "account 2")["email"] == "b@test.com"
    assert resolve_requested_account(ACCOUNTS, "ACCOUNT2")["email"] == "b@test.com"


def test_resolve_by_numeric_index():
    assert resolve_requested_account(ACCOUNTS, "1")["email"] == "a@test.com"
    assert resolve_requested_account(ACCOUNTS, "2")["email"] == "b@test.com"


def test_resolve_returns_none_for_unknown():
    assert resolve_requested_account(ACCOUNTS, "") is None
    assert resolve_requested_account(ACCOUNTS, None) is None
    assert resolve_requested_account(ACCOUNTS, "unknown@x.com") is None
    assert resolve_requested_account(ACCOUNTS, "Account 99") is None
    assert resolve_requested_account(ACCOUNTS, "99") is None


def test_select_account_honours_request():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        # Round-robin counter should NOT advance when request is honoured
        chosen = select_account(ACCOUNTS, "Account 2", path)
        assert chosen["email"] == "b@test.com"
        assert read_counter(path) == 0


def test_select_account_falls_back_on_unknown_request():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        chosen = select_account(ACCOUNTS, "unknown@x.com", path)
        assert chosen["email"] == "a@test.com"  # round-robin slot 0
        assert read_counter(path) == 1  # counter advanced


def test_select_account_falls_back_when_no_request():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        chosen = select_account(ACCOUNTS, None, path)
        assert chosen["email"] == "a@test.com"
        assert read_counter(path) == 1

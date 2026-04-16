import json
import os
import tempfile
from scraper.rotation import next_account, read_counter, write_counter


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
    accounts = [
        {"email": "a@test.com", "password": "pw1"},
        {"email": "b@test.com", "password": "pw2"},
    ]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "counter.json")
        a1 = next_account(accounts, path)
        a2 = next_account(accounts, path)
        a3 = next_account(accounts, path)
        assert a1["email"] == "a@test.com"
        assert a2["email"] == "b@test.com"
        assert a3["email"] == "a@test.com"

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def read_counter(path: str) -> int:
    """Read the rotation counter from disk. Returns 0 if file missing."""
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        data = json.load(f)
    return data.get("counter", 0)


def write_counter(path: str, value: int) -> None:
    """Write the rotation counter to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"counter": value}, f)


def next_account(accounts: list[dict], counter_path: str) -> dict:
    """Pick the next account via round-robin. Persists counter to disk."""
    counter = read_counter(counter_path)
    account = accounts[counter % len(accounts)]
    write_counter(counter_path, counter + 1)
    return account


def resolve_requested_account(accounts: list[dict], requested: str | None) -> dict | None:
    """Resolve an n8n-supplied `account` value to one of our configured accounts.

    Accepts any of:
      - email           e.g. "jn@aramaz-digital.de"
      - "Account 1" / "Account 2" (case-insensitive, with or without space)
      - "1" / "2"       1-based index

    Returns the matching account dict, or None if no match. Caller falls back
    to round-robin on None.
    """
    if not requested:
        return None
    raw = requested.strip()
    if not raw:
        return None

    # Email match (case-insensitive)
    for acc in accounts:
        if acc["email"].lower() == raw.lower():
            return acc

    # "Account N" label
    m = re.fullmatch(r"\s*account\s*(\d+)\s*", raw, re.IGNORECASE)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(accounts):
            return accounts[idx]

    # Bare numeric index
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(accounts):
            return accounts[idx]

    return None


def select_account(
    accounts: list[dict],
    requested: str | None,
    counter_path: str,
) -> dict:
    """Pick an account, honouring an n8n request when possible.

    If `requested` matches one of our configured accounts, return it.
    Otherwise fall back to round-robin and log the miss so we can see what
    n8n is actually sending.
    """
    if requested:
        match = resolve_requested_account(accounts, requested)
        if match:
            logger.info(f"Using requested account: {requested!r} -> {match['email']}")
            return match
        logger.warning(
            f"Requested account {requested!r} did not match any configured "
            f"account; falling back to round-robin"
        )
    return next_account(accounts, counter_path)

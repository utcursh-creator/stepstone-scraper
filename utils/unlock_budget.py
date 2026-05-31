"""Persistent daily unlock budget.

Each successful StepStone profile unlock costs one credit. To enforce a hard
ceiling across all jobs in a day (n8n sends each job as a separate /scrape
request, so an in-memory counter would not survive between jobs), we persist a
small JSON counter keyed by date.

State file shape: {"date": "YYYY-MM-DD", "unlocks": <int>}
On a new date the counter resets to 0. A corrupt/missing file reads as 0.

`today` is injected (not read from the clock) so callers pass a stable date
string and tests stay deterministic. main.py passes
datetime.now(timezone.utc).strftime("%Y-%m-%d").
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "date" in data and "unlocks" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return {"date": None, "unlocks": 0}


def _write(path: str, date: str, unlocks: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"date": date, "unlocks": unlocks}, f)
    os.replace(tmp, path)  # atomic on POSIX


def unlocks_today(path: str, today: str) -> int:
    """Return how many unlocks have been recorded for `today` (0 on a new day)."""
    data = _read(path)
    if data.get("date") != today:
        return 0
    return int(data.get("unlocks", 0))


def record_unlock(path: str, today: str) -> int:
    """Increment and persist the unlock counter for `today`. Returns new count."""
    current = unlocks_today(path, today)
    new_count = current + 1
    _write(path, today, new_count)
    return new_count


def budget_remaining(path: str, cap: int, today: str) -> int:
    """Return max(0, cap - unlocks_today)."""
    return max(0, cap - unlocks_today(path, today))

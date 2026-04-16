import json
import os


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

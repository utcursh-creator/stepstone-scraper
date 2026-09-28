"""Where runtime state lives: saved StepStone sessions and the counters.

On Railway a persistent volume is mounted at /app/data. Without it, every
deploy wiped sessions/, so every deploy forced a fresh password login from a
new proxy IP, and on 2026-09-28 four of those in a few hours made StepStone
show a CAPTCHA. Locally (no /app/data) the working directory is used, as before.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
STATE_DIR = os.path.join(DATA_DIR, "state")

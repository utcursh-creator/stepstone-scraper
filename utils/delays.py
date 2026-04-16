import asyncio
import random


def random_delay(min_ms: int, max_ms: int) -> float:
    """Return a random delay in seconds between min_ms and max_ms milliseconds."""
    return random.randint(min_ms, max_ms) / 1000.0


async def human_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
    """Sleep for a random human-like duration."""
    await asyncio.sleep(random_delay(min_ms, max_ms))

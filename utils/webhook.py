import httpx
from models.candidate import ScrapeResult


async def send_webhook(url: str, result: ScrapeResult) -> bool:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=result.model_dump(), timeout=30.0)
            response.raise_for_status()
            return True
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return False

import asyncio
import httpx

AIRTABLE_API = "https://api.airtable.com/v0"
RATE_LIMIT_DELAY = 0.2


async def is_duplicate(pat: str, base_id: str, table_id: str, offer_id: str, profile_id: str) -> bool:
    formula = f'AND({{Offer ID}}="{offer_id}",{{StepStone Profile ID}}="{profile_id}")'
    url = f"{AIRTABLE_API}/{base_id}/{table_id}"
    async with httpx.AsyncClient() as client:
        try:
            await asyncio.sleep(RATE_LIMIT_DELAY)
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {pat}"},
                params={"filterByFormula": formula, "maxRecords": "1"},
                timeout=10.0,
            )
            response.raise_for_status()
            records = response.json().get("records", [])
            return len(records) > 0
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return False

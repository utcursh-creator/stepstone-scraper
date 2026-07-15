import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

AIRTABLE_API = "https://api.airtable.com/v0"
RATE_LIMIT_DELAY = 0.2


async def is_duplicate(pat: str, base_id: str, table_id: str, offer_id: str, profile_id: str) -> bool:
    """Check whether a (offer_id, profile_id) pair already exists in Airtable.

    The dedup formula coerces both fields with `&""` (e.g.
    ``{Offer ID}&""="2517044"``). On the current TEXT columns this is a no-op
    (``"2517044" & "" = "2517044"``, and ``&`` binds tighter than ``=``), so
    it cannot change today's behavior. It is defence against the base being
    re-imported from CSV, which silently re-types columns: comparing a NUMBER
    field to a quoted string is always false in Airtable, so a bare
    ``{Offer ID}="..."`` would stop matching and this dedup would fail open.

    Caveat, so nobody over-trusts this: `&""` is NOT a complete fix for a
    number column — Airtable can render a number per its configured precision
    (e.g. "2517044.00"), which still would not equal "2517044". If the column
    ever does become a number, fix the COLUMN TYPE; do not rely on this.

    Fails open (returns False) on HTTP errors or timeouts — a duplicate row
    is preferable to silently dropping a candidate — but logs a WARNING so the
    failure is visible. It was a silent fail-open here that let weeks of 401s
    (a PAT with no access to the migrated base) burn duplicate unlock credits
    unnoticed.
    """
    # Defensive: ids are numeric strings, but strip any '"' chars so they can
    # never break out of the quoted formula literal.
    offer_id = str(offer_id).replace('"', "")
    profile_id = str(profile_id).replace('"', "")
    formula = f'AND({{Offer ID}}&""="{offer_id}",{{StepStone Profile ID}}&""="{profile_id}")'
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
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Airtable dedup fail-open: HTTP %s from base=%s table=%s "
                "(offer_id=%s profile_id=%s) — treating as non-duplicate",
                exc.response.status_code,
                base_id,
                table_id,
                offer_id,
                profile_id,
            )
            return False
        except httpx.TimeoutException:
            logger.warning(
                "Airtable dedup fail-open: timeout for base=%s table=%s "
                "(offer_id=%s profile_id=%s) — treating as non-duplicate",
                base_id,
                table_id,
                offer_id,
                profile_id,
            )
            return False

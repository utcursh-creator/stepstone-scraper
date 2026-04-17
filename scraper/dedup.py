from utils.airtable import is_duplicate as _airtable_check


async def check_duplicate(
    pat: str,
    base_id: str,
    table_id: str,
    offer_id: str,
    profile_id: str,
) -> bool:
    """Check if this candidate was already processed for this offer.

    Returns True if duplicate (skip this candidate).
    Returns False if new (proceed with evaluation).
    On API error, returns False (process the candidate to be safe).
    """
    return await _airtable_check(
        pat=pat,
        base_id=base_id,
        table_id=table_id,
        offer_id=offer_id,
        profile_id=profile_id,
    )

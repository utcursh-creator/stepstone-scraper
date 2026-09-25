"""Shared fakes for tests that drive main.run_scrape without a browser."""
from scraper.talent_search import SearchOutcome


def fake_search_outcome(cards, total=None, radius_km=40):
    """A Talent Finder search whose single page holds `cards` (anything with the
    SearchResult attributes the gates read)."""
    async def no_more_pages(page_number):  # pragma: no cover - one page only
        return {"content": []}
    cards = list(cards)
    return SearchOutcome(keyword="test", location_name="Berlin", radius_km=radius_km,
                         total=len(cards) if total is None else total, total_pages=1,
                         criteria_id=1, first_page=cards, fetch_page=no_more_pages)

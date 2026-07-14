"""
SeatGeek API fetcher.

SeatGeek has a well-documented public API that provides:
- Event search with filters
- Ticket listings with all-in pricing (fees included in displayed price)
- Deal scores (their proprietary value metric)

API Docs: https://platform.seatgeek.com/
Rate Limit: ~1000 requests/day on free tier
"""

import logging
from datetime import datetime
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseFetcher, EventSearch, TicketListing

logger = logging.getLogger(__name__)


class SeatGeekFetcher(BaseFetcher):
    """Fetcher for SeatGeek ticket marketplace."""

    BASE_URL = "https://api.seatgeek.com/2"

    # SeatGeek displays all-in pricing, so fee multiplier is ~1.0
    # However, there can be small order processing fees (~$5-10 per order)
    ORDER_FEE_PER_TICKET = 0.0  # SeatGeek includes fees in listed price

    def __init__(self, client_id: str, client_secret: Optional[str] = None):
        """
        Initialize SeatGeek fetcher.

        Args:
            client_id: SeatGeek API client ID (required, free to obtain)
            client_secret: SeatGeek API client secret (optional, for higher rate limits)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        super().__init__(api_key=client_id)

    def _get_platform_name(self) -> str:
        return "SeatGeek"

    def _get_auth_params(self) -> dict:
        """Get authentication parameters for API requests."""
        params = {"client_id": self.client_id}
        if self.client_secret:
            params["client_secret"] = self.client_secret
        return params

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def _make_request(self, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated request to the SeatGeek API."""
        url = f"{self.BASE_URL}/{endpoint}"
        request_params = self._get_auth_params()
        if params:
            request_params.update(params)

        logger.debug(f"SeatGeek API request: {endpoint} with params: {params}")

        response = requests.get(url, params=request_params, timeout=30)
        response.raise_for_status()
        return response.json()

    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search SeatGeek for events matching criteria.

        SeatGeek event types:
        - sports: nba, nfl, mlb, nhl, mls, etc.
        - concert: concert
        - theater: theater, broadway
        """
        # SeatGeek's API works best with performer names rather than
        # "Team A vs Team B" format. Try the full query first, then
        # fall back to searching individual performer/team names.
        events = self._search_with_query(search, search.query)

        # If no results, try splitting "X vs Y" or "X Vs Y" queries
        if not events and self._is_versus_query(search.query):
            parts = self._split_versus_query(search.query)
            for part in parts:
                events = self._search_with_query(search, part)
                if events:
                    break

        # If still no results, try with just the first word(s) (team/artist name)
        if not events:
            # Try first meaningful part (skip common words)
            words = search.query.split()
            if len(words) > 1:
                # Try first team name (everything before vs/at/-)
                first_part = words[0] if len(words[0]) > 3 else " ".join(words[:2])
                events = self._search_with_query(search, first_part)

        logger.info(f"SeatGeek found {len(events)} events for '{search.query}'")
        return events

    @staticmethod
    def _is_versus_query(query: str) -> bool:
        """Check if query is a 'Team A vs Team B' format."""
        import re
        return bool(re.search(r'\b(?:vs\.?|versus|at)\b', query, re.IGNORECASE))

    @staticmethod
    def _split_versus_query(query: str) -> list[str]:
        """Split a 'Team A vs Team B' query into individual team names."""
        import re
        parts = re.split(r'\s+(?:vs\.?|versus|at)\s+', query, flags=re.IGNORECASE)
        return [p.strip() for p in parts if p.strip()]

    def _search_with_query(self, search: EventSearch, query: str) -> list[dict]:
        """Execute a search with a specific query string."""
        params = {"q": query, "per_page": 25}

        # Add location filter
        if search.city:
            # SeatGeek uses venue.city for filtering
            params["venue.city"] = search.city
        if search.state:
            params["venue.state"] = search.state

        # Add date filters
        if search.date_from:
            params["datetime_utc.gte"] = search.date_from.strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
        if search.date_to:
            params["datetime_utc.lte"] = search.date_to.strftime("%Y-%m-%dT%H:%M:%S")

        # Add event type filter
        if search.event_type:
            type_map = {
                "sports": None,  # SeatGeek uses sport-specific types
                "concert": "concert",
                "theater": "theater",
            }
            if search.event_type in type_map and type_map[search.event_type]:
                params["type"] = type_map[search.event_type]

        # Only show events with available tickets
        params["listing_count.gt"] = 0

        try:
            data = self._make_request("events", params)
        except requests.exceptions.HTTPError as e:
            logger.error(f"SeatGeek API error: {e}")
            return []

        events = []
        for event in data.get("events", []):
            event_info = {
                "event_id": str(event["id"]),
                "event_name": event["title"],
                "event_date": datetime.fromisoformat(
                    event["datetime_utc"].replace("Z", "+00:00")
                ),
                "venue": event["venue"]["name"],
                "city": event["venue"]["city"],
                "state": event["venue"]["state"],
                "url": event["url"],
                "lowest_price": event.get("stats", {}).get("lowest_price"),
                "average_price": event.get("stats", {}).get("average_price"),
                "highest_price": event.get("stats", {}).get("highest_price"),
                "listing_count": event.get("stats", {}).get("listing_count", 0),
                "score": event.get("score", 0),  # SeatGeek popularity score
                "event_type": event.get("type", "unknown"),
            }
            events.append(event_info)

        return events

    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get ticket listings for a specific SeatGeek event.

        Note: SeatGeek's public API provides event-level stats (lowest, average,
        highest prices) but detailed per-listing data requires their affiliate/
        partner API. We use the event stats to generate representative listings,
        and the URL sends users to SeatGeek where they see real-time listings.
        """
        try:
            data = self._make_request(f"events/{event_id}")
        except requests.exceptions.HTTPError as e:
            logger.error(f"SeatGeek API error fetching event {event_id}: {e}")
            return []

        event = data.get("event", data)
        if not event:
            return []

        stats = event.get("stats", {})
        lowest_price = stats.get("lowest_price")
        average_price = stats.get("average_price")
        highest_price = stats.get("highest_price")
        listing_count = stats.get("listing_count", 0)

        if not lowest_price:
            return []

        # Parse event details
        event_name = event["title"]
        event_date = datetime.fromisoformat(
            event["datetime_utc"].replace("Z", "+00:00")
        )
        venue = event["venue"]["name"]
        city = event["venue"]["city"]
        url = event["url"]

        # SeatGeek shows all-in prices (fees included)
        # Generate representative price tiers based on stats
        listings = []

        # Best deal - lowest available price
        listings.append(
            TicketListing(
                platform=self.platform_name,
                event_name=event_name,
                event_date=event_date,
                venue=venue,
                city=city,
                section="Best Available (Upper/Back)",
                row=None,
                quantity=quantity,
                price_per_ticket=lowest_price,
                estimated_fees=self.ORDER_FEE_PER_TICKET,
                total_price_per_ticket=lowest_price + self.ORDER_FEE_PER_TICKET,
                url=url,
                deal_score=self._calculate_deal_score(
                    lowest_price, lowest_price, average_price
                ),
                notes="SeatGeek all-in pricing (fees included)",
            )
        )

        # Mid-range option
        if average_price and average_price != lowest_price:
            listings.append(
                TicketListing(
                    platform=self.platform_name,
                    event_name=event_name,
                    event_date=event_date,
                    venue=venue,
                    city=city,
                    section="Mid-Level Seating",
                    row=None,
                    quantity=quantity,
                    price_per_ticket=average_price,
                    estimated_fees=self.ORDER_FEE_PER_TICKET,
                    total_price_per_ticket=average_price + self.ORDER_FEE_PER_TICKET,
                    url=url,
                    deal_score=self._calculate_deal_score(
                        average_price, lowest_price, average_price
                    ),
                    notes="SeatGeek all-in pricing (fees included)",
                )
            )

        # Premium option
        if highest_price and highest_price != average_price:
            # Use a price between average and highest for "good lower level"
            premium_price = average_price + (highest_price - average_price) * 0.3
            listings.append(
                TicketListing(
                    platform=self.platform_name,
                    event_name=event_name,
                    event_date=event_date,
                    venue=venue,
                    city=city,
                    section="Lower Level / Premium",
                    row=None,
                    quantity=quantity,
                    price_per_ticket=round(premium_price, 2),
                    estimated_fees=self.ORDER_FEE_PER_TICKET,
                    total_price_per_ticket=round(
                        premium_price + self.ORDER_FEE_PER_TICKET, 2
                    ),
                    url=url,
                    deal_score=self._calculate_deal_score(
                        premium_price, lowest_price, average_price
                    ),
                    notes="SeatGeek all-in pricing (fees included)",
                )
            )

        return listings

    def get_event_price_history(self, event_id: str) -> dict:
        """
        Get price trend data for an event.
        Useful for determining if prices are going up or down.
        """
        try:
            data = self._make_request(f"events/{event_id}")
        except requests.exceptions.HTTPError:
            return {}

        event = data.get("event", data)
        stats = event.get("stats", {})

        return {
            "event_id": event_id,
            "event_name": event.get("title", ""),
            "current_lowest": stats.get("lowest_price"),
            "current_average": stats.get("average_price"),
            "current_highest": stats.get("highest_price"),
            "listing_count": stats.get("listing_count", 0),
            "checked_at": datetime.now().isoformat(),
            # SeatGeek doesn't expose historical prices via API,
            # but our tracker will build its own history over time
        }

    @staticmethod
    def _calculate_deal_score(
        price: float, lowest: float, average: float
    ) -> float:
        """
        Calculate a deal score from 0-10.
        10 = incredible deal, 0 = terrible value.
        """
        if not average or average == 0:
            return 5.0

        # How far below average is this price?
        ratio = price / average

        if ratio <= 0.5:
            return 10.0  # 50%+ below average
        elif ratio <= 0.7:
            return 8.5  # 30-50% below average
        elif ratio <= 0.85:
            return 7.0  # 15-30% below average
        elif ratio <= 1.0:
            return 5.5  # At or slightly below average
        elif ratio <= 1.2:
            return 4.0  # Up to 20% above average
        elif ratio <= 1.5:
            return 2.5  # 20-50% above average
        else:
            return 1.0  # 50%+ above average

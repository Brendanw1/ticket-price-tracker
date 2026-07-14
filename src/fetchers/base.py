"""
Base class for all ticket price fetchers.
Defines the common interface and shared utilities.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class TicketListing:
    """Represents a single ticket listing from any platform."""
    platform: str
    event_name: str
    event_date: datetime
    venue: str
    city: str
    section: str
    row: Optional[str]
    quantity: int
    price_per_ticket: float  # Listed price before fees
    estimated_fees: float  # Estimated platform fees
    total_price_per_ticket: float  # All-in price per ticket
    url: str
    fetched_at: datetime = field(default_factory=datetime.now)
    deal_score: Optional[float] = None  # 0-10 score if available
    notes: Optional[str] = None

    @property
    def total_cost(self) -> float:
        """Total cost for all tickets in this listing."""
        return self.total_price_per_ticket * self.quantity

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "platform": self.platform,
            "event_name": self.event_name,
            "event_date": self.event_date.isoformat(),
            "venue": self.venue,
            "city": self.city,
            "section": self.section,
            "row": self.row,
            "quantity": self.quantity,
            "price_per_ticket": self.price_per_ticket,
            "estimated_fees": self.estimated_fees,
            "total_price_per_ticket": self.total_price_per_ticket,
            "total_cost": self.total_cost,
            "url": self.url,
            "fetched_at": self.fetched_at.isoformat(),
            "deal_score": self.deal_score,
            "notes": self.notes,
        }


@dataclass
class EventSearch:
    """Search parameters for finding events."""
    query: str  # e.g., "Lakers vs Celtics" or "Taylor Swift"
    city: Optional[str] = None
    state: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    quantity: int = 2  # Number of tickets needed
    max_price: Optional[float] = None  # Maximum all-in price per ticket
    event_type: Optional[str] = None  # "sports", "concert", "theater"


class BaseFetcher(ABC):
    """Abstract base class for ticket platform fetchers."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.platform_name = self._get_platform_name()

    @abstractmethod
    def _get_platform_name(self) -> str:
        """Return the name of the platform."""
        pass

    @abstractmethod
    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search for events matching the criteria.
        Returns a list of event dictionaries with at minimum:
        - event_id (platform-specific)
        - event_name
        - event_date
        - venue
        - city
        """
        pass

    @abstractmethod
    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get available ticket listings for a specific event.
        Returns list of TicketListing objects sorted by total_price_per_ticket.
        """
        pass

    def get_best_deals(
        self, search: EventSearch, max_results: int = 10
    ) -> list[TicketListing]:
        """
        Search for events and return the best deals across all matches.
        If no events found with original query and it's a "vs" query,
        tries individual team/performer names.
        """
        import re

        events = self.search_events(search)

        # If no events found and query has "vs"/"at", try individual names
        if not events and re.search(r'\b(?:vs\.?|versus|at)\b', search.query, re.IGNORECASE):
            parts = re.split(r'\s+(?:vs\.?|versus|at)\s+', search.query, flags=re.IGNORECASE)
            parts = [p.strip() for p in parts if p.strip()]
            for part in parts:
                alt_search = EventSearch(
                    query=part,
                    city=search.city,
                    state=search.state,
                    date_from=search.date_from,
                    date_to=search.date_to,
                    quantity=search.quantity,
                    max_price=search.max_price,
                    event_type=search.event_type,
                )
                events = self.search_events(alt_search)
                if events:
                    break

        all_listings = []

        for event in events[:5]:  # Limit to first 5 matching events
            event_id = event.get("event_id", "")
            # Skip events with empty/missing IDs
            if not event_id or str(event_id).strip() == "":
                continue
            try:
                listings = self.get_listings(
                    event_id=str(event_id), quantity=search.quantity
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(
                    f"Error fetching listings for event '{event.get('event_name', '')}': {e}"
                )
                continue
            if search.max_price:
                listings = [
                    l for l in listings if l.total_price_per_ticket <= search.max_price
                ]
            all_listings.extend(listings)

        # Sort by total price per ticket (lowest first)
        all_listings.sort(key=lambda x: x.total_price_per_ticket)
        return all_listings[:max_results]

"""
Price history tracking and trend analysis.

Stores price snapshots over time using TinyDB (lightweight JSON-based storage).
Provides trend analysis to help determine if prices are dropping (good time to
buy) or rising (wait or buy now).
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from tinydb import TinyDB, Query

from ..fetchers.base import TicketListing

logger = logging.getLogger(__name__)


class PriceHistory:
    """
    Tracks price history for events across platforms.
    Provides trend analysis and buy/wait recommendations.
    """

    def __init__(self, db_path: str = "data/price_history.json"):
        """Initialize with path to TinyDB database file."""
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = TinyDB(db_path)
        self.snapshots = self.db.table("snapshots")
        self.alerts = self.db.table("alerts")

    def record_snapshot(self, listing: TicketListing):
        """Record a price snapshot for historical tracking."""
        self.snapshots.insert(
            {
                "event_name": listing.event_name,
                "platform": listing.platform,
                "section": listing.section,
                "price_per_ticket": listing.price_per_ticket,
                "total_price_per_ticket": listing.total_price_per_ticket,
                "quantity": listing.quantity,
                "timestamp": datetime.now().isoformat(),
                "event_date": listing.event_date.isoformat(),
            }
        )

    def record_batch(self, listings: list[TicketListing]):
        """Record multiple snapshots at once."""
        for listing in listings:
            self.record_snapshot(listing)

    def get_price_trend(
        self,
        event_name: str,
        platform: Optional[str] = None,
        days: int = 7,
    ) -> dict:
        """
        Analyze price trend for an event over the specified period.

        Returns:
            dict with trend info:
            - direction: "dropping", "rising", "stable"
            - change_percent: percentage change over period
            - lowest_seen: lowest price recorded
            - highest_seen: highest price recorded
            - current_vs_average: how current price compares to average
            - recommendation: "buy_now", "wait", or "uncertain"
        """
        Event = Query()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        conditions = (
            (Event.event_name == event_name)
            & (Event.timestamp >= cutoff)
        )
        if platform:
            conditions = conditions & (Event.platform == platform)

        records = self.snapshots.search(conditions)

        if len(records) < 2:
            return {
                "direction": "insufficient_data",
                "change_percent": 0,
                "lowest_seen": records[0]["total_price_per_ticket"] if records else None,
                "highest_seen": records[0]["total_price_per_ticket"] if records else None,
                "current_vs_average": 0,
                "recommendation": "uncertain",
                "data_points": len(records),
            }

        # Sort by timestamp
        records.sort(key=lambda x: x["timestamp"])

        prices = [r["total_price_per_ticket"] for r in records]
        earliest_price = prices[0]
        latest_price = prices[-1]
        lowest = min(prices)
        highest = max(prices)
        average = sum(prices) / len(prices)

        # Calculate trend
        change_pct = ((latest_price - earliest_price) / earliest_price) * 100

        if change_pct <= -5:
            direction = "dropping"
        elif change_pct >= 5:
            direction = "rising"
        else:
            direction = "stable"

        # Current price vs historical average
        current_vs_avg = ((latest_price - average) / average) * 100

        # Generate recommendation
        recommendation = self._generate_recommendation(
            direction, change_pct, current_vs_avg, records
        )

        return {
            "direction": direction,
            "change_percent": round(change_pct, 1),
            "lowest_seen": lowest,
            "highest_seen": highest,
            "current_price": latest_price,
            "average_price": round(average, 2),
            "current_vs_average": round(current_vs_avg, 1),
            "recommendation": recommendation,
            "data_points": len(records),
        }


    def _generate_recommendation(
        self,
        direction: str,
        change_pct: float,
        current_vs_avg: float,
        records: list[dict],
    ) -> str:
        """
        Generate a buy/wait recommendation based on trend analysis.

        Logic:
        - If prices are dropping significantly: WAIT (they'll likely drop more)
        - If prices are at/near historical low: BUY NOW
        - If prices are rising: BUY NOW (before they go higher)
        - If stable and below average: BUY NOW
        - If stable and above average: WAIT
        """
        # Check if event is within 48 hours (last-minute panic selling window)
        if records:
            try:
                event_date = datetime.fromisoformat(records[-1].get("event_date", ""))
                hours_until_event = (event_date - datetime.now()).total_seconds() / 3600
                if 0 < hours_until_event <= 48:
                    return "buy_now"  # Last-minute window - prices will drop further then vanish
            except (ValueError, TypeError):
                pass

        if direction == "dropping" and change_pct < -10:
            return "wait"  # Significant downtrend, likely to continue
        elif direction == "dropping" and current_vs_avg < -10:
            return "buy_now"  # Already well below average, good deal
        elif direction == "rising" and change_pct > 5:
            return "buy_now"  # Prices going up, buy before more expensive
        elif direction == "stable" and current_vs_avg <= 0:
            return "buy_now"  # At or below average, decent value
        elif direction == "stable" and current_vs_avg > 10:
            return "wait"  # Above average, wait for a dip
        else:
            return "uncertain"

    def get_lowest_ever(
        self, event_name: str, platform: Optional[str] = None
    ) -> Optional[float]:
        """Get the lowest price ever recorded for an event."""
        Event = Query()
        conditions = Event.event_name == event_name
        if platform:
            conditions = conditions & (Event.platform == platform)

        records = self.snapshots.search(conditions)
        if not records:
            return None

        return min(r["total_price_per_ticket"] for r in records)

    def is_new_low(self, listing: TicketListing) -> bool:
        """Check if this listing is a new all-time low for the event."""
        lowest = self.get_lowest_ever(listing.event_name, listing.platform)
        if lowest is None:
            return True  # First time seeing this event
        return listing.total_price_per_ticket < lowest

    def record_alert_sent(
        self, event_name: str, platform: str, price: float
    ):
        """Record that an alert was sent to avoid duplicate alerts."""
        self.alerts.insert(
            {
                "event_name": event_name,
                "platform": platform,
                "price": price,
                "sent_at": datetime.now().isoformat(),
            }
        )

    def was_alert_sent_recently(
        self, event_name: str, hours: int = 4
    ) -> bool:
        """Check if an alert was already sent recently for this event."""
        Alert = Query()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        results = self.alerts.search(
            (Alert.event_name == event_name) & (Alert.sent_at >= cutoff)
        )
        return len(results) > 0

    def cleanup_old_data(self, days: int = 90):
        """Remove price history older than specified days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        Event = Query()
        removed = self.snapshots.remove(Event.timestamp < cutoff)
        logger.info(f"Cleaned up {len(removed)} old price records")

    def get_summary_stats(self, event_name: str) -> dict:
        """Get summary statistics for an event's price history."""
        Event = Query()
        records = self.snapshots.search(Event.event_name == event_name)

        if not records:
            return {"error": "No data found for this event"}

        prices = [r["total_price_per_ticket"] for r in records]
        platforms = set(r["platform"] for r in records)

        return {
            "event_name": event_name,
            "total_snapshots": len(records),
            "platforms_tracked": list(platforms),
            "lowest_price": min(prices),
            "highest_price": max(prices),
            "average_price": round(sum(prices) / len(prices), 2),
            "price_range": round(max(prices) - min(prices), 2),
            "first_tracked": min(r["timestamp"] for r in records),
            "last_tracked": max(r["timestamp"] for r in records),
        }

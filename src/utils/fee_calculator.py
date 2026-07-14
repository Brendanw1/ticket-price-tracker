"""
Multi-platform fee calculator and comparison engine.

This module normalizes prices across platforms to true "all-in" costs,
accounting for each platform's different fee structures. This is critical
because a $100 ticket on SeatGeek (all-in) is NOT the same as a $100
ticket on StubHub (which becomes ~$127 after fees).

Platform Fee Summary:
- SeatGeek: All-in pricing (fees included in listed price)
- StubHub: ~25% buyer fee + $7.95 order processing
- Vivid Seats: ~22% service fee
- Ticketmaster Resale: ~20-25% service fee + $5-15 order fee
- Gametime: All-in pricing (similar to SeatGeek)
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..fetchers.base import TicketListing

logger = logging.getLogger(__name__)


@dataclass
class PlatformFeeStructure:
    """Defines the fee structure for a ticket platform."""
    platform_name: str
    is_all_in_pricing: bool  # True if listed price includes all fees
    buyer_fee_percent: float  # Percentage fee (e.g., 0.25 for 25%)
    order_processing_fee: float  # Flat per-order fee
    delivery_fee: float  # Per-order delivery fee
    notes: str = ""

    def calculate_total_per_ticket(
        self, listed_price: float, quantity: int = 2
    ) -> float:
        """Calculate the true all-in price per ticket."""
        if self.is_all_in_pricing:
            return listed_price

        service_fee = listed_price * self.buyer_fee_percent
        per_ticket_order_fee = self.order_processing_fee / max(quantity, 1)
        per_ticket_delivery_fee = self.delivery_fee / max(quantity, 1)

        return round(
            listed_price + service_fee + per_ticket_order_fee + per_ticket_delivery_fee,
            2,
        )

    def calculate_fees_only(
        self, listed_price: float, quantity: int = 2
    ) -> float:
        """Calculate just the fees portion per ticket."""
        if self.is_all_in_pricing:
            return 0.0

        service_fee = listed_price * self.buyer_fee_percent
        per_ticket_order_fee = self.order_processing_fee / max(quantity, 1)
        per_ticket_delivery_fee = self.delivery_fee / max(quantity, 1)

        return round(service_fee + per_ticket_order_fee + per_ticket_delivery_fee, 2)



# Pre-configured fee structures for known platforms
PLATFORM_FEES = {
    "SeatGeek": PlatformFeeStructure(
        platform_name="SeatGeek",
        is_all_in_pricing=True,
        buyer_fee_percent=0.0,
        order_processing_fee=0.0,
        delivery_fee=0.0,
        notes="All-in pricing - what you see is what you pay",
    ),
    "StubHub": PlatformFeeStructure(
        platform_name="StubHub",
        is_all_in_pricing=False,
        buyer_fee_percent=0.25,
        order_processing_fee=7.95,
        delivery_fee=0.0,
        notes="~25% buyer service fee + $7.95 order processing",
    ),
    "VividSeats": PlatformFeeStructure(
        platform_name="VividSeats",
        is_all_in_pricing=False,
        buyer_fee_percent=0.22,
        order_processing_fee=0.0,
        delivery_fee=0.0,
        notes="~22% service fee, free mobile delivery",
    ),
    "Ticketmaster": PlatformFeeStructure(
        platform_name="Ticketmaster",
        is_all_in_pricing=False,
        buyer_fee_percent=0.22,
        order_processing_fee=8.00,
        delivery_fee=0.0,
        notes="~22% service fee + $8 order processing",
    ),
    "Gametime": PlatformFeeStructure(
        platform_name="Gametime",
        is_all_in_pricing=True,
        buyer_fee_percent=0.0,
        order_processing_fee=0.0,
        delivery_fee=0.0,
        notes="All-in pricing, last-minute deals",
    ),
}


@dataclass
class ComparisonResult:
    """Result of comparing a listing across platforms."""
    listing: TicketListing
    true_cost_per_ticket: float
    true_total_cost: float
    fee_amount_per_ticket: float
    fee_percentage: float  # Actual fee as % of listed price
    value_rank: int  # 1 = best value
    savings_vs_worst: float  # How much cheaper than the most expensive option
    platform_notes: str


class FeeCalculator:
    """
    Compares ticket prices across platforms with normalized fee calculations.
    Provides true apples-to-apples comparison.
    """

    def __init__(self):
        self.fee_structures = PLATFORM_FEES.copy()

    def add_platform(self, fee_structure: PlatformFeeStructure):
        """Add or update a platform's fee structure."""
        self.fee_structures[fee_structure.platform_name] = fee_structure

    def get_true_price(
        self, platform: str, listed_price: float, quantity: int = 2
    ) -> float:
        """Get the true all-in price per ticket for a platform."""
        fee_struct = self.fee_structures.get(platform)
        if not fee_struct:
            logger.warning(
                f"Unknown platform '{platform}', assuming 20% fees"
            )
            return round(listed_price * 1.20, 2)
        return fee_struct.calculate_total_per_ticket(listed_price, quantity)

    def get_fees(
        self, platform: str, listed_price: float, quantity: int = 2
    ) -> float:
        """Get just the fee amount per ticket for a platform."""
        fee_struct = self.fee_structures.get(platform)
        if not fee_struct:
            return round(listed_price * 0.20, 2)
        return fee_struct.calculate_fees_only(listed_price, quantity)


    def compare_listings(
        self, listings: list[TicketListing]
    ) -> list[ComparisonResult]:
        """
        Compare listings across platforms with normalized pricing.
        Returns results sorted by true cost (best deal first).
        """
        if not listings:
            return []

        results = []

        for listing in listings:
            platform = listing.platform
            fee_struct = self.fee_structures.get(platform)

            # Calculate true all-in cost
            true_cost = self.get_true_price(
                platform, listing.price_per_ticket, listing.quantity
            )
            fee_amount = self.get_fees(
                platform, listing.price_per_ticket, listing.quantity
            )
            fee_pct = (
                (fee_amount / listing.price_per_ticket * 100)
                if listing.price_per_ticket > 0
                else 0
            )

            results.append(
                ComparisonResult(
                    listing=listing,
                    true_cost_per_ticket=true_cost,
                    true_total_cost=round(true_cost * listing.quantity, 2),
                    fee_amount_per_ticket=fee_amount,
                    fee_percentage=round(fee_pct, 1),
                    value_rank=0,  # Set below
                    savings_vs_worst=0.0,  # Set below
                    platform_notes=fee_struct.notes if fee_struct else "Unknown fee structure",
                )
            )

        # Sort by true cost (lowest first)
        results.sort(key=lambda x: x.true_cost_per_ticket)

        # Assign rankings and calculate savings
        if results:
            worst_price = results[-1].true_cost_per_ticket
            for i, result in enumerate(results):
                result.value_rank = i + 1
                result.savings_vs_worst = round(
                    worst_price - result.true_cost_per_ticket, 2
                )

        return results

    def find_best_deal(
        self, listings: list[TicketListing]
    ) -> Optional[ComparisonResult]:
        """Find the single best deal across all listings."""
        results = self.compare_listings(listings)
        return results[0] if results else None

    def generate_comparison_report(
        self, listings: list[TicketListing], event_name: str = ""
    ) -> str:
        """
        Generate a human-readable comparison report.
        Perfect for SMS or console output.
        """
        results = self.compare_listings(listings)
        if not results:
            return "No listings to compare."

        lines = []
        if event_name:
            lines.append(f"=== {event_name} ===")
        lines.append(f"Compared {len(results)} listings across platforms:")
        lines.append("")

        for result in results[:10]:  # Top 10
            listing = result.listing
            rank_emoji = {1: "1.", 2: "2.", 3: "3."}.get(
                result.value_rank, f"{result.value_rank}."
            )

            lines.append(
                f"{rank_emoji} {listing.platform} - "
                f"${result.true_cost_per_ticket:.2f}/ticket (all-in)"
            )
            lines.append(
                f"   Section: {listing.section}"
                f"{f' Row: {listing.row}' if listing.row else ''}"
            )
            lines.append(
                f"   Listed: ${listing.price_per_ticket:.2f} + "
                f"${result.fee_amount_per_ticket:.2f} fees "
                f"({result.fee_percentage:.0f}%)"
            )
            if result.savings_vs_worst > 0:
                lines.append(
                    f"   Saves ${result.savings_vs_worst:.2f}/ticket vs worst option"
                )
            lines.append("")

        # Summary
        best = results[0]
        worst = results[-1]
        if best.true_cost_per_ticket < worst.true_cost_per_ticket:
            savings_total = (
                worst.true_cost_per_ticket - best.true_cost_per_ticket
            ) * best.listing.quantity
            lines.append(
                f"BEST DEAL: {best.listing.platform} saves you "
                f"${savings_total:.2f} total vs {worst.listing.platform}"
            )

        return "\n".join(lines)

    def generate_sms_summary(
        self, listings: list[TicketListing], max_chars: int = 1500
    ) -> str:
        """
        Generate a concise SMS-friendly comparison.
        Keeps it short and actionable.
        """
        results = self.compare_listings(listings)
        if not results:
            return "No deals found."

        best = results[0]
        listing = best.listing

        lines = [
            f"TICKET ALERT: {listing.event_name}",
            f"Best: {listing.platform} ${best.true_cost_per_ticket:.0f}/ea (all-in)",
            f"Sec: {listing.section}",
        ]

        # Add runner-up if different platform
        if len(results) > 1 and results[1].listing.platform != listing.platform:
            runner = results[1]
            lines.append(
                f"vs {runner.listing.platform} ${runner.true_cost_per_ticket:.0f}/ea"
            )

        # Add savings info
        if len(results) > 1:
            savings = results[-1].true_cost_per_ticket - best.true_cost_per_ticket
            if savings > 5:
                lines.append(f"Saves ${savings:.0f}/ticket!")

        lines.append(f"Link: {listing.url}")

        msg = "\n".join(lines)
        return msg[:max_chars]

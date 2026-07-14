"""
SMS Alert system using Twilio.

Sends price drop notifications and deal alerts via SMS.
Includes rate limiting to avoid spamming, message formatting
optimized for SMS character limits, and delivery tracking.

Twilio Setup:
1. Sign up at https://www.twilio.com (free trial gives you $15 credit)
2. Get your Account SID and Auth Token from the console
3. Get a Twilio phone number (or use the trial number)
4. Verify your personal phone number (required on free tier)

Pricing: ~$0.0079/SMS in the US (about 1 cent per message)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from ..fetchers.base import TicketListing
from ..utils.fee_calculator import FeeCalculator, ComparisonResult

logger = logging.getLogger(__name__)


class SMSAlert:
    """
    SMS notification system for ticket price alerts.
    Uses Twilio to send SMS messages when prices hit targets.
    """

    # SMS character limit (standard SMS is 160, but Twilio handles multipart)
    MAX_SMS_LENGTH = 1600  # Twilio supports up to 1600 chars (concatenated)
    RECOMMENDED_LENGTH = 480  # 3 standard SMS segments for readability

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        max_alerts_per_hour: int = 5,
        quiet_hours: tuple = (23, 7),  # Don't send between 11PM and 7AM
    ):
        """
        Initialize SMS alert system.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            from_number: Twilio phone number (e.g., '+15551234567')
            to_number: Your phone number to receive alerts (e.g., '+15559876543')
            max_alerts_per_hour: Rate limit for alerts
            quiet_hours: Tuple of (start_hour, end_hour) for quiet period
        """
        self.client = Client(account_sid, auth_token)
        self.from_number = from_number
        self.to_number = to_number
        self.max_alerts_per_hour = max_alerts_per_hour
        self.quiet_hours = quiet_hours

        # Track sent messages for rate limiting
        self._sent_timestamps: list[datetime] = []
        self._fee_calculator = FeeCalculator()

    def _is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        current_hour = datetime.now().hour
        start, end = self.quiet_hours

        if start > end:  # Wraps midnight (e.g., 23 to 7)
            return current_hour >= start or current_hour < end
        else:
            return start <= current_hour < end

    def _is_rate_limited(self) -> bool:
        """Check if we've exceeded the hourly alert limit."""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        # Clean old timestamps
        self._sent_timestamps = [
            ts for ts in self._sent_timestamps if ts > one_hour_ago
        ]
        return len(self._sent_timestamps) >= self.max_alerts_per_hour

    def _send_sms(self, body: str) -> Optional[str]:
        """
        Send an SMS message via Twilio.

        Returns:
            Message SID if successful, None if failed.
        """
        # Enforce length limit
        if len(body) > self.MAX_SMS_LENGTH:
            body = body[: self.MAX_SMS_LENGTH - 3] + "..."

        try:
            message = self.client.messages.create(
                body=body,
                from_=self.from_number,
                to=self.to_number,
            )
            self._sent_timestamps.append(datetime.now())
            logger.info(
                f"SMS sent successfully. SID: {message.sid}, "
                f"To: {self.to_number}"
            )
            return message.sid
        except TwilioRestException as e:
            logger.error(f"Twilio error sending SMS: {e.msg}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending SMS: {e}")
            return None


    def send_price_drop_alert(
        self,
        listing: TicketListing,
        target_price: float,
        trend_info: Optional[dict] = None,
        force: bool = False,
    ) -> Optional[str]:
        """
        Send an alert when a ticket price drops below the target.

        Args:
            listing: The ticket listing that triggered the alert
            target_price: The user's target price threshold
            trend_info: Optional price trend data for context
            force: If True, bypass quiet hours and rate limits

        Returns:
            Message SID if sent, None otherwise.
        """
        if not force:
            if self._is_quiet_hours():
                logger.info("Skipping alert - quiet hours active")
                return None
            if self._is_rate_limited():
                logger.info("Skipping alert - rate limit reached")
                return None

        # Format the message
        body = self._format_price_drop_message(listing, target_price, trend_info)
        return self._send_sms(body)

    def send_deal_comparison_alert(
        self,
        results: list[ComparisonResult],
        event_name: str,
        target_price: Optional[float] = None,
        force: bool = False,
    ) -> Optional[str]:
        """
        Send a multi-platform deal comparison alert.

        Args:
            results: Comparison results from FeeCalculator
            event_name: Name of the event
            target_price: Optional target price for context
            force: If True, bypass quiet hours and rate limits

        Returns:
            Message SID if sent, None otherwise.
        """
        if not force:
            if self._is_quiet_hours():
                logger.info("Skipping comparison alert - quiet hours")
                return None
            if self._is_rate_limited():
                logger.info("Skipping comparison alert - rate limited")
                return None

        body = self._format_comparison_message(results, event_name, target_price)
        return self._send_sms(body)

    def send_new_low_alert(
        self,
        listing: TicketListing,
        previous_low: float,
        force: bool = False,
    ) -> Optional[str]:
        """
        Send an alert when a new all-time low price is detected.

        Args:
            listing: The listing with the new low price
            previous_low: The previous lowest recorded price
            force: If True, bypass quiet hours and rate limits

        Returns:
            Message SID if sent, None otherwise.
        """
        if not force:
            if self._is_quiet_hours():
                return None
            if self._is_rate_limited():
                return None

        savings = previous_low - listing.total_price_per_ticket
        savings_pct = (savings / previous_low) * 100

        body = (
            f"NEW LOW PRICE!\n"
            f"{listing.event_name}\n"
            f"${listing.total_price_per_ticket:.0f}/ticket on {listing.platform}\n"
            f"Down ${savings:.0f} ({savings_pct:.0f}%) from previous low\n"
            f"Section: {listing.section}\n"
            f"{listing.url}"
        )
        return self._send_sms(body)

    def send_buy_recommendation(
        self,
        listing: TicketListing,
        trend_info: dict,
        force: bool = False,
    ) -> Optional[str]:
        """
        Send a 'BUY NOW' recommendation based on trend analysis.

        Args:
            listing: Best available listing
            trend_info: Trend data with recommendation
            force: If True, bypass quiet hours and rate limits

        Returns:
            Message SID if sent, None otherwise.
        """
        if not force:
            if self._is_quiet_hours():
                return None
            if self._is_rate_limited():
                return None

        direction = trend_info.get("direction", "unknown")
        change_pct = trend_info.get("change_percent", 0)
        avg_price = trend_info.get("average_price", 0)

        reason = ""
        if direction == "rising":
            reason = f"Prices UP {change_pct:.0f}% - buy before higher"
        elif "current_vs_average" in trend_info:
            below_avg = abs(trend_info["current_vs_average"])
            reason = f"Currently {below_avg:.0f}% below average"

        body = (
            f"BUY RECOMMENDATION\n"
            f"{listing.event_name}\n"
            f"${listing.total_price_per_ticket:.0f}/ticket ({listing.platform})\n"
            f"{reason}\n"
            f"Avg price: ${avg_price:.0f}\n"
            f"Section: {listing.section}\n"
            f"{listing.url}"
        )
        return self._send_sms(body)


    def _format_price_drop_message(
        self,
        listing: TicketListing,
        target_price: float,
        trend_info: Optional[dict] = None,
    ) -> str:
        """Format a price drop alert message for SMS."""
        below_target = target_price - listing.total_price_per_ticket
        below_pct = (below_target / target_price) * 100

        lines = [
            f"PRICE DROP ALERT!",
            f"{listing.event_name}",
            f"${listing.total_price_per_ticket:.0f}/ticket (all-in) on {listing.platform}",
            f"${below_target:.0f} BELOW your ${target_price:.0f} target ({below_pct:.0f}% under)",
            f"Section: {listing.section}",
        ]

        if listing.row:
            lines.append(f"Row: {listing.row}")

        lines.append(f"Qty: {listing.quantity}")

        # Add trend context if available
        if trend_info:
            direction = trend_info.get("direction", "")
            if direction == "dropping":
                lines.append(f"Trend: Prices dropping (may go lower)")
            elif direction == "rising":
                lines.append(f"Trend: Prices rising - ACT FAST")
            elif direction == "stable":
                lines.append(f"Trend: Prices stable")

        lines.append(f"{listing.url}")

        return "\n".join(lines)

    def _format_comparison_message(
        self,
        results: list[ComparisonResult],
        event_name: str,
        target_price: Optional[float] = None,
    ) -> str:
        """Format a multi-platform comparison for SMS."""
        if not results:
            return f"No deals found for {event_name}"

        lines = [f"DEAL COMPARISON: {event_name}", ""]

        # Show top 3 deals
        for i, result in enumerate(results[:3], 1):
            listing = result.listing
            lines.append(
                f"{i}. {listing.platform}: "
                f"${result.true_cost_per_ticket:.0f}/ea (all-in)"
            )
            lines.append(f"   {listing.section}")
            if result.fee_amount_per_ticket > 0:
                lines.append(
                    f"   (Listed ${listing.price_per_ticket:.0f} + "
                    f"${result.fee_amount_per_ticket:.0f} fees)"
                )

        # Best deal summary
        best = results[0]
        if len(results) > 1:
            worst = results[-1]
            savings = worst.true_cost_per_ticket - best.true_cost_per_ticket
            if savings > 0:
                lines.append("")
                lines.append(
                    f"BEST: {best.listing.platform} saves "
                    f"${savings * best.listing.quantity:.0f} total"
                )

        # Target price context
        if target_price and best.true_cost_per_ticket <= target_price:
            lines.append(f"BELOW your ${target_price:.0f} target!")

        lines.append(f"\n{best.listing.url}")

        return "\n".join(lines)

    def send_test_message(self) -> Optional[str]:
        """
        Send a test SMS to verify configuration.
        Bypasses all rate limits and quiet hours.

        Returns:
            Message SID if successful, None if failed.
        """
        body = (
            "Ticket Price Tracker - TEST MESSAGE\n"
            "Your SMS alerts are configured correctly!\n"
            f"Alerts will be sent to: {self.to_number}\n"
            f"Max alerts/hour: {self.max_alerts_per_hour}\n"
            f"Quiet hours: {self.quiet_hours[0]}:00 - {self.quiet_hours[1]}:00"
        )
        return self._send_sms(body)

    def get_status(self) -> dict:
        """Get current alert system status."""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_count = sum(
            1 for ts in self._sent_timestamps if ts > one_hour_ago
        )

        return {
            "to_number": self.to_number,
            "from_number": self.from_number,
            "alerts_sent_last_hour": recent_count,
            "max_alerts_per_hour": self.max_alerts_per_hour,
            "rate_limited": self._is_rate_limited(),
            "quiet_hours_active": self._is_quiet_hours(),
            "quiet_hours": f"{self.quiet_hours[0]}:00 - {self.quiet_hours[1]}:00",
        }

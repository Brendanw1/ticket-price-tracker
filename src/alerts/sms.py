"""
SMS Alert system using Email-to-SMS Gateway (FREE).

Sends price drop notifications and deal alerts via SMS by routing
emails through carrier SMS gateways. This is 100% free — no Twilio,
no paid services, just your existing email account.

How it works:
- Every US carrier has an email-to-SMS gateway (e.g., number@txt.att.net)
- You send an email TO that address, and it arrives as a text message
- Works with Gmail, Outlook, Yahoo, or any SMTP-capable email provider

Setup:
1. Know your phone number and carrier
2. Use a Gmail account (or any email with SMTP access)
3. For Gmail: enable "App Passwords" (requires 2FA enabled)
   - Go to: https://myaccount.google.com/apppasswords
   - Create an app password for "Mail"
4. That's it — completely free, unlimited messages

Supported Carriers (US):
- AT&T: number@txt.att.net
- T-Mobile: number@tmomail.net
- Verizon: number@vtext.com
- Sprint: number@messaging.sprintpcs.com
- US Cellular: number@email.uscc.net
- Metro PCS: number@mymetropcs.com
- Boost Mobile: number@sms.myboostmobile.com
- Cricket: number@sms.cricketwireless.net
- Mint Mobile: number@tmomail.net (uses T-Mobile network)
- Google Fi: number@msg.fi.google.com
- Visible: number@vtext.com (uses Verizon network)
- Xfinity Mobile: number@vtext.com (uses Verizon network)

Supported Carriers (Canada):
- Rogers: number@pcs.rogers.com
- Bell: number@txt.bell.ca
- Telus: number@msg.telus.com
- Fido: number@fido.ca
- Koodo: number@msg.telus.com

Limitations:
- SMS messages limited to 160 characters per segment
- Most carriers concatenate up to ~5 segments (800 chars)
- Some carriers may strip formatting or truncate
- Delivery is usually instant but can occasionally delay 1-2 min
- MMS (picture messages) use different gateway addresses
"""

import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from ..fetchers.base import TicketListing
from ..utils.fee_calculator import FeeCalculator, ComparisonResult

logger = logging.getLogger(__name__)


# Email-to-SMS gateway addresses by carrier
CARRIER_GATEWAYS = {
    # US Carriers
    "att": "{number}@txt.att.net",
    "tmobile": "{number}@tmomail.net",
    "verizon": "{number}@vtext.com",
    "sprint": "{number}@messaging.sprintpcs.com",
    "uscellular": "{number}@email.uscc.net",
    "metropcs": "{number}@mymetropcs.com",
    "boost": "{number}@sms.myboostmobile.com",
    "cricket": "{number}@sms.cricketwireless.net",
    "mint": "{number}@tmomail.net",  # T-Mobile MVNO
    "googlefi": "{number}@msg.fi.google.com",
    "visible": "{number}@vtext.com",  # Verizon MVNO
    "xfinity": "{number}@vtext.com",  # Verizon MVNO
    "straight_talk": "{number}@vtext.com",  # Usually Verizon
    "consumer_cellular": "{number}@mailmymobile.net",
    # Canada
    "rogers": "{number}@pcs.rogers.com",
    "bell": "{number}@txt.bell.ca",
    "telus": "{number}@msg.telus.com",
    "fido": "{number}@fido.ca",
    "koodo": "{number}@msg.telus.com",
}

# Common SMTP server configurations
SMTP_SERVERS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "tls": True},
    "outlook": {"host": "smtp.office365.com", "port": 587, "tls": True},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 587, "tls": True},
    "icloud": {"host": "smtp.mail.me.com", "port": 587, "tls": True},
    "aol": {"host": "smtp.aol.com", "port": 587, "tls": True},
    "zoho": {"host": "smtp.zoho.com", "port": 587, "tls": True},
}


def get_sms_gateway(phone_number: str, carrier: str) -> Optional[str]:
    """
    Get the SMS gateway email address for a phone number and carrier.

    Args:
        phone_number: 10-digit phone number (e.g., '5551234567')
        carrier: Carrier name (e.g., 'att', 'tmobile', 'verizon')

    Returns:
        Gateway email address or None if carrier not supported.
    """
    # Clean the phone number - remove everything except digits
    clean_number = "".join(c for c in phone_number if c.isdigit())

    # Remove country code if present (US = 1)
    if len(clean_number) == 11 and clean_number.startswith("1"):
        clean_number = clean_number[1:]

    carrier_lower = carrier.lower().replace(" ", "").replace("-", "_")

    gateway_template = CARRIER_GATEWAYS.get(carrier_lower)
    if not gateway_template:
        logger.error(
            f"Unsupported carrier: '{carrier}'. "
            f"Supported: {', '.join(sorted(CARRIER_GATEWAYS.keys()))}"
        )
        return None

    return gateway_template.format(number=clean_number)


class SMSAlert:
    """
    SMS notification system for ticket price alerts.
    Uses Email-to-SMS gateway (completely free) to send text messages.
    """

    # SMS character limits via email gateway
    MAX_SMS_LENGTH = 800  # ~5 SMS segments concatenated
    RECOMMENDED_LENGTH = 450  # 3 segments for best readability

    def __init__(
        self,
        smtp_email: str,
        smtp_password: str,
        phone_number: str,
        carrier: str,
        smtp_provider: str = "gmail",
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        max_alerts_per_hour: int = 5,
        quiet_hours: tuple = (23, 7),  # Don't send between 11PM and 7AM
    ):
        """
        Initialize SMS alert system via email-to-SMS gateway.

        Args:
            smtp_email: Your email address (e.g., 'you@gmail.com')
            smtp_password: Your email password or app password
                           For Gmail: use App Password (not your regular password)
                           https://myaccount.google.com/apppasswords
            phone_number: Your phone number (e.g., '5551234567' or '+15551234567')
            carrier: Your phone carrier (e.g., 'att', 'tmobile', 'verizon')
                     See CARRIER_GATEWAYS for full list
            smtp_provider: Email provider ('gmail', 'outlook', 'yahoo', etc.)
                           Or use smtp_host/smtp_port for custom SMTP
            smtp_host: Custom SMTP host (overrides smtp_provider)
            smtp_port: Custom SMTP port (overrides smtp_provider)
            max_alerts_per_hour: Rate limit for alerts
            quiet_hours: Tuple of (start_hour, end_hour) for quiet period
        """
        self.smtp_email = smtp_email
        self.smtp_password = smtp_password
        self.phone_number = phone_number
        self.carrier = carrier
        self.max_alerts_per_hour = max_alerts_per_hour
        self.quiet_hours = quiet_hours

        # Resolve SMTP server config
        if smtp_host and smtp_port:
            self.smtp_host = smtp_host
            self.smtp_port = smtp_port
            self.smtp_tls = True
        else:
            provider_config = SMTP_SERVERS.get(smtp_provider.lower())
            if not provider_config:
                raise ValueError(
                    f"Unknown SMTP provider: '{smtp_provider}'. "
                    f"Supported: {', '.join(SMTP_SERVERS.keys())}. "
                    f"Or provide smtp_host and smtp_port directly."
                )
            self.smtp_host = provider_config["host"]
            self.smtp_port = provider_config["port"]
            self.smtp_tls = provider_config["tls"]

        # Resolve SMS gateway address
        self.sms_gateway = get_sms_gateway(phone_number, carrier)
        if not self.sms_gateway:
            raise ValueError(
                f"Could not resolve SMS gateway for carrier '{carrier}'. "
                f"Supported carriers: {', '.join(sorted(CARRIER_GATEWAYS.keys()))}"
            )

        # Track sent messages for rate limiting
        self._sent_timestamps: list[datetime] = []
        self._fee_calculator = FeeCalculator()

        logger.info(
            f"SMS alerts initialized via email gateway. "
            f"Gateway: {self.sms_gateway}, Provider: {smtp_provider}"
        )

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
        Send an SMS message via email-to-SMS gateway.

        Returns:
            A message ID string if successful, None if failed.
        """
        # Enforce length limit
        if len(body) > self.MAX_SMS_LENGTH:
            body = body[: self.MAX_SMS_LENGTH - 3] + "..."

        try:
            # Create the email message
            msg = MIMEMultipart()
            msg["From"] = self.smtp_email
            msg["To"] = self.sms_gateway
            # Leave subject empty - it would add noise to SMS
            msg["Subject"] = ""

            # Attach the body as plain text
            msg.attach(MIMEText(body, "plain"))

            # Connect and send via SMTP
            context = ssl.create_default_context()

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.smtp_tls:
                    server.starttls(context=context)
                server.login(self.smtp_email, self.smtp_password)
                server.sendmail(
                    self.smtp_email, self.sms_gateway, msg.as_string()
                )

            # Generate a message ID for tracking
            msg_id = f"sms_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(msg)}"
            self._sent_timestamps.append(datetime.now())

            logger.info(
                f"SMS sent via email gateway. "
                f"To: {self.sms_gateway}, ID: {msg_id}"
            )
            return msg_id

        except smtplib.SMTPAuthenticationError as e:
            logger.error(
                f"SMTP authentication failed. "
                f"If using Gmail, make sure you're using an App Password "
                f"(not your regular password). Error: {e}"
            )
            return None
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error sending SMS: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending SMS via email: {e}")
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
            Message ID if sent, None otherwise.
        """
        if not force:
            if self._is_quiet_hours():
                logger.info("Skipping alert - quiet hours active")
                return None
            if self._is_rate_limited():
                logger.info("Skipping alert - rate limit reached")
                return None

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
            Message ID if sent, None otherwise.
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
            Message ID if sent, None otherwise.
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
            f"${listing.total_price_per_ticket:.0f}/ea on {listing.platform}\n"
            f"Down ${savings:.0f} ({savings_pct:.0f}%) from prev low\n"
            f"Sec: {listing.section}\n"
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
            Message ID if sent, None otherwise.
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
            reason = f"{below_avg:.0f}% below average"

        body = (
            f"BUY RECOMMENDATION\n"
            f"{listing.event_name}\n"
            f"${listing.total_price_per_ticket:.0f}/ea ({listing.platform})\n"
            f"{reason}\n"
            f"Avg: ${avg_price:.0f} | Sec: {listing.section}\n"
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
            f"PRICE DROP!",
            f"{listing.event_name}",
            f"${listing.total_price_per_ticket:.0f}/ea (all-in) on {listing.platform}",
            f"${below_target:.0f} BELOW ${target_price:.0f} target ({below_pct:.0f}% under)",
            f"Sec: {listing.section}",
        ]

        if listing.row:
            lines.append(f"Row: {listing.row}")

        # Add trend context (brief for SMS)
        if trend_info:
            direction = trend_info.get("direction", "")
            if direction == "dropping":
                lines.append("Trend: Dropping")
            elif direction == "rising":
                lines.append("Trend: Rising - ACT FAST")

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

        lines = [f"DEALS: {event_name}"]

        # Show top 3 deals (compact format for SMS)
        for i, result in enumerate(results[:3], 1):
            listing = result.listing
            fee_str = ""
            if result.fee_amount_per_ticket > 0:
                fee_str = f" (+${result.fee_amount_per_ticket:.0f} fee)"
            lines.append(
                f"{i}. {listing.platform}: ${result.true_cost_per_ticket:.0f}/ea{fee_str}"
            )

        # Best deal summary
        best = results[0]
        if len(results) > 1:
            worst = results[-1]
            savings = worst.true_cost_per_ticket - best.true_cost_per_ticket
            if savings > 0:
                lines.append(
                    f"Save ${savings:.0f}/ea with {best.listing.platform}"
                )

        if target_price and best.true_cost_per_ticket <= target_price:
            lines.append(f"BELOW ${target_price:.0f} target!")

        lines.append(f"{best.listing.url}")

        return "\n".join(lines)

    def send_test_message(self) -> Optional[str]:
        """
        Send a test SMS to verify configuration.
        Bypasses all rate limits and quiet hours.

        Returns:
            Message ID if successful, None if failed.
        """
        body = (
            "Ticket Price Tracker\n"
            "SMS alerts working!\n"
            f"Gateway: {self.sms_gateway}\n"
            f"Max/hr: {self.max_alerts_per_hour}\n"
            f"Quiet: {self.quiet_hours[0]}:00-{self.quiet_hours[1]}:00"
        )
        return self._send_sms(body)

    def get_status(self) -> dict:
        """Get current alert system status."""
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_count = sum(
            1 for ts in self._sent_timestamps if ts > one_hour_ago
        )

        return {
            "phone_number": self.phone_number,
            "carrier": self.carrier,
            "sms_gateway": self.sms_gateway,
            "smtp_email": self.smtp_email,
            "smtp_host": self.smtp_host,
            "alerts_sent_last_hour": recent_count,
            "max_alerts_per_hour": self.max_alerts_per_hour,
            "rate_limited": self._is_rate_limited(),
            "quiet_hours_active": self._is_quiet_hours(),
            "quiet_hours": f"{self.quiet_hours[0]}:00 - {self.quiet_hours[1]}:00",
        }

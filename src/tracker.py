"""
Main orchestrator for the Ticket Price Tracker.

This module ties together all components:
- Fetchers (SeatGeek, StubHub, Vivid Seats, TickPick, Gametime, Ticketmaster)
- Fee Calculator (normalizes prices across 6 platforms)
- Price History (tracks trends over time)
- SMS Alerts (sends notifications when targets are hit)

It runs on a configurable schedule, checking prices at regular intervals
and triggering alerts when conditions are met.
"""

import logging
import sys
import time
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
import schedule
from dotenv import load_dotenv

from .fetchers import (
    SeatGeekFetcher,
    StubHubFetcher,
    VividSeatsFetcher,
    TickPickFetcher,
    GametimeFetcher,
    TicketmasterFetcher,
)
from .fetchers.base import EventSearch, TicketListing
from .utils.fee_calculator import FeeCalculator
from .utils.price_history import PriceHistory
from .alerts.sms import SMSAlert

logger = logging.getLogger(__name__)


class TicketTracker:
    """
    Main orchestrator that coordinates price checking across platforms,
    tracks history, and sends alerts.
    """

    def __init__(self, config_path: str = "config/config.yaml"):
        """
        Initialize the tracker with configuration.

        Args:
            config_path: Path to YAML configuration file
        """
        # Load environment variables
        load_dotenv()

        # Load configuration
        self.config = self._load_config(config_path)
        self.running = False

        # Initialize components
        self._init_fetchers()
        self._init_alerts()
        self.fee_calculator = FeeCalculator()
        self.price_history = PriceHistory(
            db_path=self.config.get("data_path", "data/price_history.json")
        )

        # Track events we're monitoring
        self.watched_events = self.config.get("watched_events", [])

        logger.info(
            f"Ticket Tracker initialized. "
            f"Monitoring {len(self.watched_events)} events across "
            f"{len(self.active_fetchers)} platforms."
        )

    def _load_config(self, config_path: str) -> dict:
        """Load YAML configuration file."""
        path = Path(config_path)
        if not path.exists():
            logger.warning(
                f"Config file not found at {config_path}, using defaults"
            )
            return self._default_config()

        with open(path, "r") as f:
            config = yaml.safe_load(f)

        config = config or self._default_config()

        # Resolve ${ENV_VAR} references in config values
        self._resolve_env_vars(config)

        return config

    def _resolve_env_vars(self, obj):
        """
        Recursively resolve ${ENV_VAR} references in config values.
        Replaces strings like '${SEATGEEK_CLIENT_ID}' with the actual
        environment variable value, or empty string if not set.
        """
        import re
        import os

        env_pattern = re.compile(r"^\$\{(\w+)\}$")

        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str):
                    match = env_pattern.match(value)
                    if match:
                        env_name = match.group(1)
                        obj[key] = os.environ.get(env_name, "")
                elif isinstance(value, (dict, list)):
                    self._resolve_env_vars(value)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                if isinstance(item, str):
                    match = env_pattern.match(item)
                    if match:
                        env_name = match.group(1)
                        obj[i] = os.environ.get(env_name, "")
                elif isinstance(item, (dict, list)):
                    self._resolve_env_vars(item)

    @staticmethod
    def _default_config() -> dict:
        """Return default configuration."""
        return {
            "check_interval_minutes": 30,
            "platforms": {
                "seatgeek": {"enabled": True, "client_id": ""},
                "stubhub": {"enabled": True},
                "vividseats": {"enabled": True},
                "tickpick": {"enabled": True},
                "gametime": {"enabled": True},
                "ticketmaster": {"enabled": True, "api_key": ""},
            },
            "alerts": {
                "sms": {
                    "enabled": False,
                    "account_sid": "",
                    "auth_token": "",
                    "from_number": "",
                    "to_number": "",
                    "max_alerts_per_hour": 5,
                    "quiet_hours_start": 23,
                    "quiet_hours_end": 7,
                }
            },
            "watched_events": [],
            "data_path": "data/price_history.json",
        }


    def _init_fetchers(self):
        """Initialize platform fetchers based on configuration."""
        self.active_fetchers = []
        platforms = self.config.get("platforms", {})

        # SeatGeek (requires API key)
        sg_config = platforms.get("seatgeek", {})
        if sg_config.get("enabled", False) and sg_config.get("client_id"):
            try:
                fetcher = SeatGeekFetcher(
                    client_id=sg_config["client_id"],
                    client_secret=sg_config.get("client_secret"),
                )
                self.active_fetchers.append(fetcher)
                logger.info("SeatGeek fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init SeatGeek: {e}")

        # TickPick (no API key needed - zero fees platform)
        tp_config = platforms.get("tickpick", {})
        if tp_config.get("enabled", True):
            try:
                fetcher = TickPickFetcher()
                self.active_fetchers.append(fetcher)
                logger.info("TickPick fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init TickPick: {e}")

        # Gametime (no API key needed - last-minute deals)
        gt_config = platforms.get("gametime", {})
        if gt_config.get("enabled", True):
            try:
                fetcher = GametimeFetcher()
                self.active_fetchers.append(fetcher)
                logger.info("Gametime fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init Gametime: {e}")

        # StubHub (no API key needed - web scraping)
        sh_config = platforms.get("stubhub", {})
        if sh_config.get("enabled", True):
            try:
                fetcher = StubHubFetcher()
                self.active_fetchers.append(fetcher)
                logger.info("StubHub fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init StubHub: {e}")

        # Vivid Seats (no API key needed - web scraping)
        vs_config = platforms.get("vividseats", {})
        if vs_config.get("enabled", True):
            try:
                fetcher = VividSeatsFetcher()
                self.active_fetchers.append(fetcher)
                logger.info("Vivid Seats fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init Vivid Seats: {e}")

        # Ticketmaster Resale (optional API key for better results)
        tm_config = platforms.get("ticketmaster", {})
        if tm_config.get("enabled", True):
            try:
                fetcher = TicketmasterFetcher(
                    api_key=tm_config.get("api_key") or None,
                )
                self.active_fetchers.append(fetcher)
                logger.info("Ticketmaster Resale fetcher initialized")
            except Exception as e:
                logger.error(f"Failed to init Ticketmaster: {e}")

        if not self.active_fetchers:
            logger.warning("No platform fetchers initialized! Check config.")

    def _init_alerts(self):
        """Initialize alert systems based on configuration."""
        self.sms_alert: Optional[SMSAlert] = None
        alert_config = self.config.get("alerts", {}).get("sms", {})

        if alert_config.get("enabled", False):
            required_fields = ["smtp_email", "smtp_password", "phone_number", "carrier"]
            missing = [f for f in required_fields if not alert_config.get(f)]

            if missing:
                logger.warning(
                    f"SMS alerts enabled but missing config: {missing}"
                )
                return

            try:
                self.sms_alert = SMSAlert(
                    smtp_email=alert_config["smtp_email"],
                    smtp_password=alert_config["smtp_password"],
                    phone_number=alert_config["phone_number"],
                    carrier=alert_config["carrier"],
                    smtp_provider=alert_config.get("smtp_provider", "gmail"),
                    smtp_host=alert_config.get("smtp_host"),
                    smtp_port=alert_config.get("smtp_port"),
                    max_alerts_per_hour=alert_config.get("max_alerts_per_hour", 5),
                    quiet_hours=(
                        alert_config.get("quiet_hours_start", 23),
                        alert_config.get("quiet_hours_end", 7),
                    ),
                )
                logger.info("SMS alerts initialized via email-to-SMS gateway")
            except Exception as e:
                logger.error(f"Failed to init SMS alerts: {e}")


    def check_event(self, event_config: dict) -> list[TicketListing]:
        """
        Check prices for a single watched event across all platforms.

        Args:
            event_config: Dictionary with event search parameters:
                - query: Search string (e.g., "Lakers vs Celtics")
                - city: Optional city filter
                - state: Optional state filter
                - max_price: Target price threshold for alerts
                - quantity: Number of tickets needed
                - event_type: "sports", "concert", "theater"

        Returns:
            List of all listings found across platforms.
        """
        search = EventSearch(
            query=event_config["query"],
            city=event_config.get("city"),
            state=event_config.get("state"),
            max_price=event_config.get("max_price"),
            quantity=event_config.get("quantity", 2),
            event_type=event_config.get("event_type"),
        )

        all_listings = []

        for fetcher in self.active_fetchers:
            try:
                logger.info(
                    f"Checking {fetcher.platform_name} for '{search.query}'..."
                )
                listings = fetcher.get_best_deals(search, max_results=10)
                all_listings.extend(listings)
                logger.info(
                    f"  Found {len(listings)} listings on {fetcher.platform_name}"
                )
            except Exception as e:
                logger.error(
                    f"Error fetching from {fetcher.platform_name}: {e}"
                )

        return all_listings

    def check_all_events(self):
        """
        Check prices for all watched events.
        This is the main scheduled task.
        """
        logger.info(
            f"\n{'='*60}\n"
            f"PRICE CHECK - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'='*60}"
        )

        for event_config in self.watched_events:
            query = event_config.get("query", "Unknown")
            logger.info(f"\nChecking: {query}")

            try:
                listings = self.check_event(event_config)

                if not listings:
                    logger.info(f"  No listings found for '{query}'")
                    continue

                # Record price history
                self.price_history.record_batch(listings)

                # Compare across platforms
                results = self.fee_calculator.compare_listings(listings)

                # Log the comparison
                report = self.fee_calculator.generate_comparison_report(
                    listings, event_name=query
                )
                logger.info(f"\n{report}")

                # Check if alerts should fire
                self._evaluate_alerts(event_config, listings, results)

            except Exception as e:
                logger.error(f"Error checking event '{query}': {e}")

        logger.info(f"\n{'='*60}\nCheck complete.\n{'='*60}\n")


    def _evaluate_alerts(
        self,
        event_config: dict,
        listings: list[TicketListing],
        results: list,
    ):
        """
        Evaluate whether to send alerts based on current prices and trends.

        Alert triggers:
        1. Price drops below user's target price
        2. New all-time low detected
        3. Buy recommendation from trend analysis
        """
        if not self.sms_alert:
            return

        query = event_config.get("query", "")
        target_price = event_config.get("max_price")

        # Check if we already alerted recently for this event
        if self.price_history.was_alert_sent_recently(query, hours=4):
            logger.debug(f"Alert already sent recently for '{query}', skipping")
            return

        best_listing = listings[0] if listings else None
        if not best_listing:
            return

        alert_sent = False

        # Trigger 1: Price below target
        if target_price and best_listing.total_price_per_ticket <= target_price:
            trend = self.price_history.get_price_trend(query)
            sid = self.sms_alert.send_price_drop_alert(
                listing=best_listing,
                target_price=target_price,
                trend_info=trend,
            )
            if sid:
                alert_sent = True
                logger.info(f"Price drop alert sent for '{query}'")

        # Trigger 2: New all-time low
        if not alert_sent and self.price_history.is_new_low(best_listing):
            previous_low = self.price_history.get_lowest_ever(
                best_listing.event_name, best_listing.platform
            )
            if previous_low and previous_low > best_listing.total_price_per_ticket:
                sid = self.sms_alert.send_new_low_alert(
                    listing=best_listing,
                    previous_low=previous_low,
                )
                if sid:
                    alert_sent = True
                    logger.info(f"New low alert sent for '{query}'")

        # Trigger 3: Buy recommendation from trend analysis
        if not alert_sent:
            trend = self.price_history.get_price_trend(query)
            if trend.get("recommendation") == "buy_now" and trend.get("data_points", 0) >= 3:
                sid = self.sms_alert.send_buy_recommendation(
                    listing=best_listing,
                    trend_info=trend,
                )
                if sid:
                    alert_sent = True
                    logger.info(f"Buy recommendation sent for '{query}'")

        # Record that we sent an alert
        if alert_sent:
            self.price_history.record_alert_sent(
                event_name=query,
                platform=best_listing.platform,
                price=best_listing.total_price_per_ticket,
            )

    def run_once(self):
        """Run a single price check (useful for testing or cron jobs)."""
        self.check_all_events()

    def run_scheduled(self):
        """
        Run the tracker on a schedule.
        Checks prices at the configured interval until stopped.
        """
        interval = self.config.get("check_interval_minutes", 30)

        logger.info(
            f"Starting scheduled tracker. "
            f"Checking every {interval} minutes. "
            f"Press Ctrl+C to stop."
        )

        # Set up graceful shutdown
        self.running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Run immediately on start
        self.check_all_events()

        # Schedule recurring checks
        schedule.every(interval).minutes.do(self.check_all_events)

        while self.running:
            schedule.run_pending()
            time.sleep(10)  # Check schedule every 10 seconds

        logger.info("Tracker stopped.")

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown on SIGINT/SIGTERM."""
        logger.info("\nShutdown signal received. Finishing current check...")
        self.running = False


    def add_event(
        self,
        query: str,
        max_price: Optional[float] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        quantity: int = 2,
        event_type: Optional[str] = None,
    ):
        """
        Add an event to the watch list.

        Args:
            query: Search string (e.g., "Lakers vs Celtics" or "Bad Bunny")
            max_price: Target price threshold for alerts (all-in per ticket)
            city: City filter
            state: State filter
            quantity: Number of tickets needed
            event_type: "sports", "concert", or "theater"
        """
        event_config = {
            "query": query,
            "max_price": max_price,
            "city": city,
            "state": state,
            "quantity": quantity,
            "event_type": event_type,
        }
        # Remove None values
        event_config = {k: v for k, v in event_config.items() if v is not None}
        self.watched_events.append(event_config)
        logger.info(f"Added event to watchlist: '{query}' (target: ${max_price})")

    def remove_event(self, query: str):
        """Remove an event from the watch list by query string."""
        self.watched_events = [
            e for e in self.watched_events if e.get("query") != query
        ]
        logger.info(f"Removed '{query}' from watchlist")

    def list_watched_events(self) -> list[dict]:
        """Get the current watch list."""
        return self.watched_events

    def get_status(self) -> dict:
        """Get current tracker status."""
        status = {
            "active_platforms": [f.platform_name for f in self.active_fetchers],
            "watched_events": len(self.watched_events),
            "check_interval_minutes": self.config.get("check_interval_minutes", 30),
            "sms_alerts_enabled": self.sms_alert is not None,
        }
        if self.sms_alert:
            status["sms_status"] = self.sms_alert.get_status()
        return status

    def test_alerts(self) -> bool:
        """Send a test SMS alert to verify configuration."""
        if not self.sms_alert:
            logger.error("SMS alerts not configured")
            return False

        sid = self.sms_alert.send_test_message()
        if sid:
            logger.info(f"Test message sent! SID: {sid}")
            return True
        else:
            logger.error("Failed to send test message")
            return False

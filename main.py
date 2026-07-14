#!/usr/bin/env python3
"""
Ticket Price Tracker - Main Entry Point

Usage:
    python main.py                  # Run once (single price check)
    python main.py --schedule       # Run on schedule (continuous monitoring)
    python main.py --test-sms       # Send a test SMS to verify setup
    python main.py --status         # Show tracker status
    python main.py --add "query"    # Add an event to watch (interactive)

Examples:
    python main.py --schedule
    python main.py --add "Lakers vs Celtics" --max-price 150 --city "Los Angeles"
    python main.py --add "Taylor Swift Eras Tour" --max-price 200 --qty 2
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.tracker import TicketTracker


def setup_logging(verbose: bool = False):
    """Configure logging for the application."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/tracker.log", mode="a"),
        ],
    )
    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("cloudscraper").setLevel(logging.WARNING)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Ticket Price Tracker - Monitor resale prices across platforms",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to configuration file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run continuously on a schedule",
    )
    parser.add_argument(
        "--test-sms",
        action="store_true",
        help="Send a test SMS message",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show tracker status",
    )
    parser.add_argument(
        "--add",
        metavar="QUERY",
        help="Add an event to the watchlist",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        help="Target max price per ticket (used with --add)",
    )
    parser.add_argument(
        "--city",
        help="City filter (used with --add)",
    )
    parser.add_argument(
        "--state",
        help="State filter (used with --add)",
    )
    parser.add_argument(
        "--qty",
        type=int,
        default=2,
        help="Number of tickets needed (default: 2)",
    )
    parser.add_argument(
        "--event-type",
        choices=["sports", "concert", "theater"],
        help="Event type filter",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    setup_logging(verbose=args.verbose)
    logger = logging.getLogger(__name__)

    # Initialize tracker
    try:
        tracker = TicketTracker(config_path=args.config)
    except Exception as e:
        logger.error(f"Failed to initialize tracker: {e}")
        sys.exit(1)

    # Handle commands
    if args.test_sms:
        logger.info("Sending test SMS...")
        success = tracker.test_alerts()
        sys.exit(0 if success else 1)

    elif args.status:
        status = tracker.get_status()
        print("\n=== Ticket Tracker Status ===")
        print(f"Active platforms: {', '.join(status['active_platforms'])}")
        print(f"Watched events: {status['watched_events']}")
        print(f"Check interval: every {status['check_interval_minutes']} minutes")
        print(f"SMS alerts: {'Enabled' if status['sms_alerts_enabled'] else 'Disabled'}")
        if status.get("sms_status"):
            sms = status["sms_status"]
            print(f"  To: {sms['to_number']}")
            print(f"  Alerts last hour: {sms['alerts_sent_last_hour']}/{sms['max_alerts_per_hour']}")
            print(f"  Quiet hours: {sms['quiet_hours']}")
            print(f"  Currently quiet: {sms['quiet_hours_active']}")
        print("\nWatched Events:")
        for i, event in enumerate(tracker.list_watched_events(), 1):
            price_str = f" (target: ${event['max_price']})" if event.get("max_price") else ""
            print(f"  {i}. {event['query']}{price_str}")
        print()

    elif args.add:
        tracker.add_event(
            query=args.add,
            max_price=args.max_price,
            city=args.city,
            state=args.state,
            quantity=args.qty,
            event_type=args.event_type,
        )
        print(f"Added '{args.add}' to watchlist.")
        print("Note: To persist, add this event to config/config.yaml")

    elif args.schedule:
        logger.info("Starting scheduled monitoring...")
        tracker.run_scheduled()

    else:
        # Default: run a single check
        logger.info("Running single price check...")
        tracker.run_once()


if __name__ == "__main__":
    main()

"""
StubHub price fetcher using web scraping.

StubHub's public API was deprecated/restricted in 2023. This module uses
web scraping via cloudscraper to bypass Cloudflare protection and extract
ticket listing data from their public event pages.

IMPORTANT: StubHub displays prices BEFORE fees. Their fee structure is
approximately 24-28% on top of the listed price. This is factored into
the total_price_per_ticket calculation.

Fee Breakdown (approximate):
- Buyer service fee: ~20-25% of ticket price
- Order processing fee: ~$5-10 per order (split across tickets)
"""

import logging
import re
import json
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

import cloudscraper
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseFetcher, EventSearch, TicketListing

logger = logging.getLogger(__name__)


class StubHubFetcher(BaseFetcher):
    """Fetcher for StubHub ticket marketplace via web scraping."""

    BASE_URL = "https://www.stubhub.com"

    # StubHub fee structure (approximate percentages)
    BUYER_FEE_PERCENT = 0.25  # ~25% service fee
    ORDER_PROCESSING_FEE = 7.95  # Per order, flat

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize StubHub fetcher.
        No API key required for scraping, but rate limiting is important.
        """
        self.scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "desktop": True,
            }
        )
        self.scraper.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        )
        super().__init__(api_key=api_key)

    def _get_platform_name(self) -> str:
        return "StubHub"

    def _estimate_fees(self, listed_price: float, quantity: int) -> float:
        """
        Estimate StubHub fees per ticket.
        StubHub charges ~25% buyer fee + order processing fee.
        """
        service_fee = listed_price * self.BUYER_FEE_PERCENT
        processing_per_ticket = self.ORDER_PROCESSING_FEE / quantity
        return round(service_fee + processing_per_ticket, 2)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page with retry logic and anti-bot handling."""
        try:
            response = self.scraper.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"StubHub fetch failed for {url}: {e}")
            raise

    def _extract_json_ld(self, html: str) -> list[dict]:
        """Extract structured data from JSON-LD script tags."""
        soup = BeautifulSoup(html, "lxml")
        json_ld_scripts = soup.find_all("script", type="application/ld+json")

        results = []
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    results.extend(data)
                else:
                    results.append(data)
            except (json.JSONDecodeError, TypeError):
                continue
        return results

    def _extract_next_data(self, html: str) -> Optional[dict]:
        """Extract Next.js __NEXT_DATA__ which often contains listing data."""
        soup = BeautifulSoup(html, "lxml")
        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            try:
                return json.loads(next_script.string)
            except json.JSONDecodeError:
                pass
        return None

    def _parse_price_from_text(self, text: str) -> Optional[float]:
        """Extract a price value from text like '$45' or '$1,234'."""
        match = re.search(r"\$([0-9,]+(?:\.\d{2})?)", text)
        if match:
            return float(match.group(1).replace(",", ""))
        return None

    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search StubHub for events matching the query.
        Scrapes the search results page.
        """
        query_encoded = quote_plus(search.query)
        search_url = f"{self.BASE_URL}/find/s/?q={query_encoded}"

        # Add date parameters if specified
        if search.date_from:
            search_url += f"&dateFrom={search.date_from.strftime('%Y-%m-%d')}"
        if search.date_to:
            search_url += f"&dateTo={search.date_to.strftime('%Y-%m-%d')}"

        try:
            html = self._fetch_page(search_url)
        except Exception as e:
            logger.error(f"StubHub search failed: {e}")
            return []

        if not html:
            return []

        events = []

        # Try to extract from JSON-LD structured data first
        json_ld_data = self._extract_json_ld(html)
        for item in json_ld_data:
            if item.get("@type") == "Event":
                event_url = item.get("url", "")
                # Extract event ID from URL
                event_id = self._extract_event_id_from_url(event_url)

                event_info = {
                    "event_id": event_id or event_url,
                    "event_name": item.get("name", ""),
                    "event_date": self._parse_date(
                        item.get("startDate", "")
                    ),
                    "venue": item.get("location", {}).get("name", ""),
                    "city": item.get("location", {})
                    .get("address", {})
                    .get("addressLocality", ""),
                    "state": item.get("location", {})
                    .get("address", {})
                    .get("addressRegion", ""),
                    "url": event_url
                    if event_url.startswith("http")
                    else f"{self.BASE_URL}{event_url}",
                    "lowest_price": None,
                    "listing_count": None,
                }

                # Try to get price from offers
                offers = item.get("offers", {})
                if isinstance(offers, dict):
                    event_info["lowest_price"] = self._safe_float(
                        offers.get("lowPrice")
                    )
                elif isinstance(offers, list) and offers:
                    prices = [
                        self._safe_float(o.get("price"))
                        for o in offers
                        if self._safe_float(o.get("price"))
                    ]
                    if prices:
                        event_info["lowest_price"] = min(prices)

                events.append(event_info)

        # Fallback: parse HTML directly if no JSON-LD
        if not events:
            events = self._parse_search_html(html, search)

        # Filter by city if specified
        if search.city:
            city_lower = search.city.lower()
            events = [
                e
                for e in events
                if city_lower in e.get("city", "").lower()
                or city_lower in e.get("venue", "").lower()
            ]

        logger.info(f"StubHub found {len(events)} events for '{search.query}'")
        return events

    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get ticket listings for a specific StubHub event.
        Scrapes the event page for available tickets.
        """
        # event_id could be a URL or an ID
        if event_id.startswith("http"):
            event_url = event_id
        else:
            event_url = f"{self.BASE_URL}/event/{event_id}"

        # Add quantity filter to URL
        if "?" in event_url:
            event_url += f"&qty={quantity}"
        else:
            event_url += f"?qty={quantity}"

        try:
            html = self._fetch_page(event_url)
        except Exception as e:
            logger.error(f"StubHub listing fetch failed: {e}")
            return []

        if not html:
            return []

        listings = []

        # Try Next.js data first (most reliable)
        next_data = self._extract_next_data(html)
        if next_data:
            listings = self._parse_next_data_listings(next_data, quantity)

        # Fallback: parse structured data
        if not listings:
            json_ld_data = self._extract_json_ld(html)
            listings = self._parse_json_ld_listings(json_ld_data, quantity)

        # Fallback: parse HTML directly
        if not listings:
            listings = self._parse_listing_html(html, quantity)

        # Sort by total price
        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings

    def _parse_next_data_listings(
        self, next_data: dict, quantity: int
    ) -> list[TicketListing]:
        """Parse listings from Next.js page data."""
        listings = []

        try:
            # Navigate the Next.js data structure for event/listing info
            page_props = next_data.get("props", {}).get("pageProps", {})
            event_data = page_props.get("event", page_props.get("eventData", {}))
            listing_data = page_props.get("listings", page_props.get("listingData", []))

            if not event_data:
                return []

            event_name = event_data.get("name", event_data.get("title", "Unknown"))
            venue = event_data.get("venue", {}).get(
                "name", event_data.get("venueName", "")
            )
            city = event_data.get("venue", {}).get(
                "city", event_data.get("venueCity", "")
            )
            event_date_str = event_data.get(
                "eventDateLocal", event_data.get("dateLocal", "")
            )
            event_date = self._parse_date(event_date_str)
            event_url = f"{self.BASE_URL}/event/{event_data.get('id', '')}"

            # Parse individual listings
            if isinstance(listing_data, list):
                for item in listing_data[:20]:  # Limit to top 20
                    price = self._safe_float(
                        item.get("price", item.get("currentPrice", {}).get("amount"))
                    )
                    if not price:
                        continue

                    section = item.get("section", item.get("sectionName", "Unknown"))
                    row = item.get("row", item.get("rowName"))
                    qty = item.get("quantity", quantity)
                    fees = self._estimate_fees(price, qty)

                    listings.append(
                        TicketListing(
                            platform=self.platform_name,
                            event_name=event_name,
                            event_date=event_date,
                            venue=venue,
                            city=city,
                            section=str(section),
                            row=str(row) if row else None,
                            quantity=qty,
                            price_per_ticket=price,
                            estimated_fees=fees,
                            total_price_per_ticket=round(price + fees, 2),
                            url=event_url,
                            notes=f"StubHub listed price + ~{int(self.BUYER_FEE_PERCENT*100)}% fees",
                        )
                    )
        except (KeyError, TypeError, AttributeError) as e:
            logger.debug(f"Error parsing Next.js data: {e}")

        return listings

    def _parse_json_ld_listings(
        self, json_ld_data: list[dict], quantity: int
    ) -> list[TicketListing]:
        """Parse listings from JSON-LD structured data."""
        listings = []

        for item in json_ld_data:
            if item.get("@type") != "Event":
                continue

            event_name = item.get("name", "Unknown")
            venue = item.get("location", {}).get("name", "")
            city = (
                item.get("location", {}).get("address", {}).get("addressLocality", "")
            )
            event_date = self._parse_date(item.get("startDate", ""))
            event_url = item.get("url", "")

            offers = item.get("offers", {})
            if isinstance(offers, dict):
                low_price = self._safe_float(offers.get("lowPrice"))
                high_price = self._safe_float(offers.get("highPrice"))

                if low_price:
                    fees = self._estimate_fees(low_price, quantity)
                    listings.append(
                        TicketListing(
                            platform=self.platform_name,
                            event_name=event_name,
                            event_date=event_date,
                            venue=venue,
                            city=city,
                            section="Best Available",
                            row=None,
                            quantity=quantity,
                            price_per_ticket=low_price,
                            estimated_fees=fees,
                            total_price_per_ticket=round(low_price + fees, 2),
                            url=event_url,
                            notes=f"StubHub lowest listed + ~{int(self.BUYER_FEE_PERCENT*100)}% estimated fees",
                        )
                    )

                if high_price and high_price != low_price:
                    mid_price = (low_price + high_price) / 2
                    fees = self._estimate_fees(mid_price, quantity)
                    listings.append(
                        TicketListing(
                            platform=self.platform_name,
                            event_name=event_name,
                            event_date=event_date,
                            venue=venue,
                            city=city,
                            section="Mid-Range",
                            row=None,
                            quantity=quantity,
                            price_per_ticket=round(mid_price, 2),
                            estimated_fees=fees,
                            total_price_per_ticket=round(mid_price + fees, 2),
                            url=event_url,
                            notes=f"StubHub mid-range + ~{int(self.BUYER_FEE_PERCENT*100)}% estimated fees",
                        )
                    )

        return listings

    def _parse_listing_html(self, html: str, quantity: int) -> list[TicketListing]:
        """Last resort: parse listing info directly from HTML."""
        soup = BeautifulSoup(html, "lxml")
        listings = []

        # Look for common StubHub listing card patterns
        # These CSS classes change frequently, so we use multiple selectors
        listing_cards = soup.select(
            '[data-testid="listing-card"], .ListingCard, .ticket-card'
        )

        # Try to get event info from page title/header
        title_el = soup.find("h1") or soup.find("title")
        event_name = title_el.get_text(strip=True) if title_el else "Unknown Event"

        for card in listing_cards[:15]:
            # Try to extract price
            price_el = card.select_one(
                '[data-testid="listing-price"], .price, .Price'
            )
            if not price_el:
                continue

            price = self._parse_price_from_text(price_el.get_text())
            if not price:
                continue

            # Try to extract section/row
            section_el = card.select_one(
                '[data-testid="section"], .section, .Section'
            )
            row_el = card.select_one('[data-testid="row"], .row, .Row')

            section = section_el.get_text(strip=True) if section_el else "Unknown"
            row = row_el.get_text(strip=True) if row_el else None

            fees = self._estimate_fees(price, quantity)

            listings.append(
                TicketListing(
                    platform=self.platform_name,
                    event_name=event_name,
                    event_date=datetime.now(),  # Fallback
                    venue="",
                    city="",
                    section=section,
                    row=row,
                    quantity=quantity,
                    price_per_ticket=price,
                    estimated_fees=fees,
                    total_price_per_ticket=round(price + fees, 2),
                    url="",
                    notes="Parsed from HTML - verify on StubHub",
                )
            )

        return listings

    def _parse_search_html(self, html: str, search: EventSearch) -> list[dict]:
        """Parse search results from HTML when JSON-LD is unavailable."""
        soup = BeautifulSoup(html, "lxml")
        events = []

        # Look for event cards in search results
        event_cards = soup.select(
            '[data-testid="event-card"], .EventCard, .event-listing'
        )

        for card in event_cards[:20]:
            link = card.find("a", href=True)
            if not link:
                continue

            href = link["href"]
            event_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            event_id = self._extract_event_id_from_url(event_url)

            name_el = card.find("h3") or card.find("h2") or link
            event_name = name_el.get_text(strip=True) if name_el else ""

            # Try to find price
            price_el = card.select_one(".price, .Price, [data-testid*='price']")
            lowest_price = None
            if price_el:
                lowest_price = self._parse_price_from_text(price_el.get_text())

            events.append(
                {
                    "event_id": event_id or event_url,
                    "event_name": event_name,
                    "event_date": datetime.now(),  # Would need to parse from card
                    "venue": "",
                    "city": "",
                    "url": event_url,
                    "lowest_price": lowest_price,
                    "listing_count": None,
                }
            )

        return events

    @staticmethod
    def _extract_event_id_from_url(url: str) -> Optional[str]:
        """Extract StubHub event ID from URL."""
        # URLs like: /event/12345 or /event-name-tickets/12345
        match = re.search(r"/(\d{6,10})(?:\?|$|/)", url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """Parse various date formats into datetime."""
        if not date_str:
            return datetime.now()

        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y %I:%M %p",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        # Try ISO format as last resort
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.now()

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """Safely convert a value to float."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

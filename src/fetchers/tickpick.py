"""
TickPick price fetcher using web scraping.

TickPick is the only major ticket marketplace with ZERO buyer fees.
The listed price is the final price you pay - no service fees, no
order processing fees, no delivery fees. This makes them frequently
the cheapest option for any given event.

Key Differentiators:
- No buyer fees whatsoever (sellers pay the fees instead)
- BidIt feature allows buyers to name their price
- All-in pricing model (listed = final)
- Strong inventory for sports and concerts

Technical Approach:
- Web scraping via cloudscraper (Cloudflare protected)
- Extracts data from embedded JSON in page source
- Falls back to HTML parsing if needed
"""

import logging
import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

import cloudscraper
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseFetcher, EventSearch, TicketListing

logger = logging.getLogger(__name__)


class TickPickFetcher(BaseFetcher):
    """Fetcher for TickPick ticket marketplace via web scraping."""

    BASE_URL = "https://www.tickpick.com"

    # TickPick has ZERO buyer fees - what you see is what you pay
    BUYER_FEE_PERCENT = 0.0
    ORDER_PROCESSING_FEE = 0.0

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize TickPick fetcher.
        No API key required - uses web scraping.
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
        return "TickPick"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page with retry logic."""
        try:
            response = self.scraper.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"TickPick fetch failed for {url}: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        """Fetch JSON data from TickPick endpoints."""
        try:
            self.scraper.headers.update({"Accept": "application/json"})
            response = self.scraper.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"TickPick JSON fetch failed: {e}")
            raise
        finally:
            self.scraper.headers.update(
                {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
            )

    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search TickPick for events matching the query.
        Uses their search page and extracts event data.
        """
        query_encoded = quote_plus(search.query)
        search_url = f"{self.BASE_URL}/buy-tickets/{query_encoded}"

        try:
            html = self._fetch_page(search_url)
        except Exception as e:
            logger.error(f"TickPick search failed: {e}")
            return []

        if not html:
            return []

        events = []
        soup = BeautifulSoup(html, "lxml")

        # Try to extract from embedded JSON/Next.js data
        events = self._extract_events_from_scripts(soup)

        # Fallback: parse HTML event cards
        if not events:
            events = self._parse_search_html(soup)

        # Filter by location if specified
        if search.city:
            city_lower = search.city.lower()
            events = [
                e for e in events
                if city_lower in e.get("city", "").lower()
                or city_lower in e.get("venue", "").lower()
            ]

        # Filter by date if specified
        if search.date_from:
            events = [
                e for e in events
                if e.get("event_date", datetime.max) >= search.date_from
            ]
        if search.date_to:
            events = [
                e for e in events
                if e.get("event_date", datetime.min) <= search.date_to
            ]

        logger.info(f"TickPick found {len(events)} events for '{search.query}'")
        return events

    def _extract_events_from_scripts(self, soup: BeautifulSoup) -> list[dict]:
        """Extract event data from embedded script tags."""
        events = []

        # Look for __NEXT_DATA__ or similar embedded JSON
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                data = json.loads(next_data_script.string)
                page_props = data.get("props", {}).get("pageProps", {})

                # Try various data locations
                event_list = (
                    page_props.get("events", [])
                    or page_props.get("searchResults", [])
                    or page_props.get("productions", [])
                )

                for item in event_list:
                    event = self._parse_event_item(item)
                    if event:
                        events.append(event)
            except (json.JSONDecodeError, TypeError):
                pass

        # Try JSON-LD structured data
        if not events:
            json_ld_scripts = soup.find_all("script", type="application/ld+json")
            for script in json_ld_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, list):
                        for item in data:
                            if item.get("@type") == "Event":
                                events.append(self._parse_json_ld_event(item))
                    elif isinstance(data, dict) and data.get("@type") == "Event":
                        events.append(self._parse_json_ld_event(data))
                except (json.JSONDecodeError, TypeError):
                    continue

        return events

    def _parse_event_item(self, item: dict) -> Optional[dict]:
        """Parse a single event item from TickPick's data."""
        if not item:
            return None

        event_id = str(
            item.get("id", item.get("eventId", item.get("productionId", "")))
        )
        if not event_id:
            return None

        name = item.get("name", item.get("title", item.get("eventName", "")))
        venue_data = item.get("venue", {})
        venue_name = (
            venue_data.get("name", "") if isinstance(venue_data, dict)
            else item.get("venueName", "")
        )
        city = (
            venue_data.get("city", "") if isinstance(venue_data, dict)
            else item.get("city", "")
        )
        state = (
            venue_data.get("state", "") if isinstance(venue_data, dict)
            else item.get("state", "")
        )

        # Parse date
        date_str = item.get("date", item.get("eventDate", item.get("datetime", "")))
        event_date = self._parse_date(date_str)

        # Get price info
        lowest_price = self._safe_float(
            item.get("minPrice", item.get("lowestPrice", item.get("price")))
        )

        # Build URL
        slug = item.get("slug", item.get("url", ""))
        if slug and not slug.startswith("http"):
            event_url = f"{self.BASE_URL}/{slug}" if slug.startswith("/") else f"{self.BASE_URL}/{slug}"
        elif slug:
            event_url = slug
        else:
            event_url = f"{self.BASE_URL}/event/{event_id}"

        return {
            "event_id": event_id,
            "event_name": name,
            "event_date": event_date,
            "venue": venue_name,
            "city": city,
            "state": state,
            "url": event_url,
            "lowest_price": lowest_price,
            "listing_count": item.get("listingCount", item.get("totalListings")),
        }

    def _parse_json_ld_event(self, item: dict) -> dict:
        """Parse a JSON-LD Event object."""
        location = item.get("location", {})
        address = location.get("address", {})

        offers = item.get("offers", {})
        lowest_price = None
        if isinstance(offers, dict):
            lowest_price = self._safe_float(offers.get("lowPrice"))
        elif isinstance(offers, list) and offers:
            prices = [self._safe_float(o.get("price")) for o in offers]
            prices = [p for p in prices if p is not None]
            lowest_price = min(prices) if prices else None

        return {
            "event_id": item.get("url", ""),
            "event_name": item.get("name", ""),
            "event_date": self._parse_date(item.get("startDate", "")),
            "venue": location.get("name", ""),
            "city": address.get("addressLocality", ""),
            "state": address.get("addressRegion", ""),
            "url": item.get("url", ""),
            "lowest_price": lowest_price,
            "listing_count": None,
        }

    def _parse_search_html(self, soup: BeautifulSoup) -> list[dict]:
        """Parse search results from HTML when JSON is unavailable."""
        events = []

        # Look for event cards - TickPick uses various card formats
        event_cards = soup.select(
            '[data-testid="event-card"], .event-card, .event-listing, '
            '.EventCard, .search-result-card, a[href*="/event/"]'
        )

        for card in event_cards[:20]:
            link = card if card.name == "a" else card.find("a", href=True)
            if not link or not link.get("href"):
                continue

            href = link["href"]
            event_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"

            name_el = card.find("h3") or card.find("h2") or card.find(".event-name")
            event_name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)

            price_el = card.select_one(
                '.price, [data-testid*="price"], .event-price, .min-price'
            )
            lowest_price = None
            if price_el:
                lowest_price = self._parse_price_from_text(price_el.get_text())

            venue_el = card.select_one('.venue, .venue-name, [data-testid="venue"]')
            venue = venue_el.get_text(strip=True) if venue_el else ""

            date_el = card.select_one('.date, .event-date, [data-testid="date"]')
            event_date = datetime.now()
            if date_el:
                event_date = self._parse_date(date_el.get_text(strip=True))

            events.append({
                "event_id": event_url,
                "event_name": event_name,
                "event_date": event_date,
                "venue": venue,
                "city": "",
                "url": event_url,
                "lowest_price": lowest_price,
                "listing_count": None,
            })

        return events

    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get ticket listings for a specific TickPick event.
        TickPick prices are ALL-IN - no fees added.
        """
        # event_id could be a URL or an ID
        if event_id.startswith("http"):
            event_url = event_id
        else:
            event_url = f"{self.BASE_URL}/event/{event_id}"

        # Add quantity to URL
        if "?" in event_url:
            event_url += f"&qty={quantity}"
        else:
            event_url += f"?qty={quantity}"

        try:
            html = self._fetch_page(event_url)
        except Exception as e:
            logger.error(f"TickPick listing fetch failed: {e}")
            return []

        if not html:
            return []

        listings = []
        soup = BeautifulSoup(html, "lxml")

        # Try Next.js data first
        listings = self._parse_listings_from_scripts(soup, quantity)

        # Fallback: parse HTML
        if not listings:
            listings = self._parse_listing_html(soup, quantity)

        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings

    def _parse_listings_from_scripts(
        self, soup: BeautifulSoup, quantity: int
    ) -> list[TicketListing]:
        """Parse listing data from embedded scripts."""
        listings = []

        # Try __NEXT_DATA__
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                data = json.loads(next_data_script.string)
                page_props = data.get("props", {}).get("pageProps", {})

                event_data = page_props.get("event", page_props.get("production", {}))
                listing_data = page_props.get(
                    "listings", page_props.get("tickets", [])
                )

                event_name = event_data.get(
                    "name", event_data.get("title", "Unknown Event")
                )
                venue = event_data.get("venue", {}).get(
                    "name", event_data.get("venueName", "")
                )
                city = event_data.get("venue", {}).get(
                    "city", event_data.get("city", "")
                )
                event_date = self._parse_date(
                    event_data.get("date", event_data.get("eventDate", ""))
                )
                event_url = f"{self.BASE_URL}/event/{event_data.get('id', '')}"

                if isinstance(listing_data, list):
                    for item in listing_data[:20]:
                        price = self._safe_float(
                            item.get("price", item.get("pricePerTicket"))
                        )
                        if not price:
                            continue

                        section = item.get(
                            "section", item.get("sectionName", "General Admission")
                        )
                        row = item.get("row", item.get("rowName"))
                        qty = item.get("quantity", quantity)

                        # TickPick = NO FEES
                        listings.append(
                            TicketListing(
                                platform=self.platform_name,
                                event_name=event_name,
                                event_date=event_date,
                                venue=venue,
                                city=city,
                                section=str(section),
                                row=str(row) if row else None,
                                quantity=min(qty, quantity),
                                price_per_ticket=price,
                                estimated_fees=0.0,
                                total_price_per_ticket=price,  # NO FEES!
                                url=event_url,
                                notes="TickPick: NO buyer fees - price is all-in",
                            )
                        )
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.debug(f"Error parsing TickPick Next.js data: {e}")

        # Try JSON-LD
        if not listings:
            json_ld_scripts = soup.find_all("script", type="application/ld+json")
            for script in json_ld_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get("@type") == "Event":
                        event_name = data.get("name", "Unknown Event")
                        venue = data.get("location", {}).get("name", "")
                        city = (
                            data.get("location", {})
                            .get("address", {})
                            .get("addressLocality", "")
                        )
                        event_date = self._parse_date(data.get("startDate", ""))

                        offers = data.get("offers", {})
                        if isinstance(offers, dict):
                            low = self._safe_float(offers.get("lowPrice"))
                            if low:
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
                                        price_per_ticket=low,
                                        estimated_fees=0.0,
                                        total_price_per_ticket=low,
                                        url=data.get("url", ""),
                                        notes="TickPick: NO buyer fees - price is all-in",
                                    )
                                )
                except (json.JSONDecodeError, TypeError):
                    continue

        return listings

    def _parse_listing_html(
        self, soup: BeautifulSoup, quantity: int
    ) -> list[TicketListing]:
        """Parse listings directly from HTML."""
        listings = []

        title_el = soup.find("h1")
        event_name = title_el.get_text(strip=True) if title_el else "Unknown Event"

        # Look for listing cards/rows
        listing_cards = soup.select(
            '[data-testid="listing-card"], .listing-card, .ticket-row, '
            '.ListingCard, [data-testid="ticket-listing"]'
        )

        for card in listing_cards[:15]:
            price_el = card.select_one(
                '.price, [data-testid*="price"], .ticket-price'
            )
            if not price_el:
                continue

            price = self._parse_price_from_text(price_el.get_text())
            if not price:
                continue

            section_el = card.select_one(
                '.section, [data-testid*="section"], .ticket-section'
            )
            row_el = card.select_one('.row, [data-testid*="row"], .ticket-row-name')

            section = section_el.get_text(strip=True) if section_el else "General"
            row = row_el.get_text(strip=True) if row_el else None

            listings.append(
                TicketListing(
                    platform=self.platform_name,
                    event_name=event_name,
                    event_date=datetime.now(),
                    venue="",
                    city="",
                    section=section,
                    row=row,
                    quantity=quantity,
                    price_per_ticket=price,
                    estimated_fees=0.0,
                    total_price_per_ticket=price,  # NO FEES
                    url="",
                    notes="TickPick: NO buyer fees - parsed from HTML",
                )
            )

        return listings

    @staticmethod
    def _parse_price_from_text(text: str) -> Optional[float]:
        """Extract a price value from text like '$45' or '$1,234'."""
        match = re.search(r"\$([0-9,]+(?:\.\d{2})?)", text)
        if match:
            return float(match.group(1).replace(",", ""))
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
            "%b %d, %Y",
            "%B %d, %Y",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

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

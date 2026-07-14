"""
Gametime price fetcher using web scraping.

Gametime specializes in last-minute ticket deals (24-72 hours before events).
They use all-in pricing (no hidden fees) and their "Zone Deals" feature
assigns best available seats at the lowest prices.

Key Differentiators:
- All-in pricing (listed price = final price, like SeatGeek & TickPick)
- Specializes in last-minute deals (prices drop close to event time)
- "Zone Deals" - pick a section, Gametime picks the best seats at lowest price
- Strong for sports, concerts, comedy, and theater
- Mobile-first platform (app-centric)

Technical Approach:
- Web scraping via cloudscraper
- Gametime uses a React-based frontend with embedded JSON data
- Their internal API endpoints can sometimes be accessed directly
- Falls back to HTML parsing when needed

Fee Structure:
- All-in pricing for buyers (no service fees, no processing fees)
- Sellers pay a ~10% commission
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


class GametimeFetcher(BaseFetcher):
    """Fetcher for Gametime ticket marketplace via web scraping."""

    BASE_URL = "https://gametime.co"
    API_URL = "https://mobile.gametime.co/v1"

    # Gametime has ALL-IN pricing - no buyer fees
    BUYER_FEE_PERCENT = 0.0
    ORDER_PROCESSING_FEE = 0.0

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Gametime fetcher.
        No API key required - uses web scraping.
        """
        self.scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
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
        return "Gametime"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page with retry logic."""
        try:
            response = self.scraper.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"Gametime fetch failed for {url}: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        """Fetch JSON from Gametime's internal API."""
        try:
            headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            response = self.scraper.get(
                url, params=params, headers=headers, timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"Gametime API fetch failed: {e}")
            raise

    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search Gametime for events matching the query.
        Gametime organizes by performer/team pages.
        """
        # Try the internal search API first
        events = self._search_via_api(search)
        if events:
            return events

        # Fallback to web scraping
        return self._search_via_scraping(search)

    def _search_via_api(self, search: EventSearch) -> list[dict]:
        """Search using Gametime's internal API endpoints."""
        search_url = f"{self.API_URL}/search"
        params = {
            "q": search.query,
            "limit": 25,
        }

        if search.city:
            params["city"] = search.city

        try:
            data = self._fetch_json(search_url, params=params)
        except Exception:
            return []

        if not data:
            return []

        events = []
        items = data.get("events", data.get("results", data.get("items", [])))

        for item in items:
            event = self._parse_api_event(item)
            if event:
                events.append(event)

        logger.info(f"Gametime API found {len(events)} events for '{search.query}'")
        return events

    def _search_via_scraping(self, search: EventSearch) -> list[dict]:
        """Fallback: search by scraping the Gametime website."""
        query_encoded = quote_plus(search.query)

        # Gametime uses performer-based URLs
        # Try the search/browse page
        search_url = f"{self.BASE_URL}/search?q={query_encoded}"

        try:
            html = self._fetch_page(search_url)
        except Exception as e:
            logger.error(f"Gametime search scraping failed: {e}")
            return []

        if not html:
            return []

        events = []
        soup = BeautifulSoup(html, "lxml")

        # Extract from embedded page data
        events = self._extract_events_from_page(soup)

        # Fallback: parse HTML event cards
        if not events:
            events = self._parse_event_cards(soup)

        # Filter by location
        if search.city:
            city_lower = search.city.lower()
            events = [
                e for e in events
                if city_lower in e.get("city", "").lower()
                or city_lower in e.get("venue", "").lower()
            ]

        logger.info(
            f"Gametime scraping found {len(events)} events for '{search.query}'"
        )
        return events

    def _extract_events_from_page(self, soup: BeautifulSoup) -> list[dict]:
        """Extract event data from embedded scripts in the page."""
        events = []

        # Look for __NEXT_DATA__ or window.__DATA__
        for script in soup.find_all("script"):
            if not script.string:
                continue

            # Try Next.js data
            if script.get("id") == "__NEXT_DATA__":
                try:
                    data = json.loads(script.string)
                    page_props = data.get("props", {}).get("pageProps", {})
                    event_list = (
                        page_props.get("events", [])
                        or page_props.get("listings", [])
                        or page_props.get("performances", [])
                    )
                    for item in event_list:
                        event = self._parse_api_event(item)
                        if event:
                            events.append(event)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Try window.__INITIAL_STATE__ or similar
            if "window.__" in (script.string or ""):
                match = re.search(
                    r"window\.__\w+__\s*=\s*({.+?});?\s*(?:</script>|$)",
                    script.string,
                    re.DOTALL,
                )
                if match:
                    try:
                        state = json.loads(match.group(1))
                        # Navigate common state structures
                        event_list = (
                            state.get("events", {}).get("items", [])
                            or state.get("search", {}).get("results", [])
                            or state.get("performances", [])
                        )
                        for item in event_list:
                            event = self._parse_api_event(item)
                            if event:
                                events.append(event)
                    except (json.JSONDecodeError, TypeError):
                        pass

        return events

    def _parse_event_cards(self, soup: BeautifulSoup) -> list[dict]:
        """Parse event cards from HTML."""
        events = []

        # Gametime event card selectors
        cards = soup.select(
            '[data-testid="event-card"], .event-card, .EventCard, '
            '.performance-card, a[href*="/events/"], a[href*="/tickets/"]'
        )

        for card in cards[:20]:
            link = card if card.name == "a" else card.find("a", href=True)
            if not link or not link.get("href"):
                continue

            href = link["href"]
            event_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"

            # Extract event ID from URL
            event_id = self._extract_event_id(event_url)

            name_el = card.find("h3") or card.find("h2") or card.find(".event-title")
            event_name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)

            # Price
            price_el = card.select_one(
                '.price, [data-testid*="price"], .event-price, .starting-price'
            )
            lowest_price = None
            if price_el:
                lowest_price = self._parse_price_from_text(price_el.get_text())

            # Venue/date
            venue_el = card.select_one('.venue, .venue-name')
            venue = venue_el.get_text(strip=True) if venue_el else ""

            date_el = card.select_one('.date, .event-date, time')
            event_date = datetime.now()
            if date_el:
                date_str = date_el.get("datetime", date_el.get_text(strip=True))
                event_date = self._parse_date(date_str)

            events.append({
                "event_id": event_id or event_url,
                "event_name": event_name,
                "event_date": event_date,
                "venue": venue,
                "city": "",
                "state": "",
                "url": event_url,
                "lowest_price": lowest_price,
                "listing_count": None,
            })

        return events

    def _parse_api_event(self, item: dict) -> Optional[dict]:
        """Parse a single event from Gametime's API response."""
        if not item:
            return None

        event_id = str(
            item.get("id", item.get("eventId", item.get("event_id", "")))
        )

        name = item.get("name", item.get("title", item.get("event_name", "")))
        if not name:
            # Try to build from performers
            performers = item.get("performers", [])
            if performers:
                name = " vs ".join(
                    p.get("name", "") for p in performers[:2]
                )

        venue_data = item.get("venue", {})
        if isinstance(venue_data, dict):
            venue = venue_data.get("name", "")
            city = venue_data.get("city", "")
            state = venue_data.get("state", venue_data.get("region", ""))
        else:
            venue = item.get("venueName", item.get("venue_name", ""))
            city = item.get("city", item.get("venueCity", ""))
            state = item.get("state", item.get("venueState", ""))

        # Date
        date_str = item.get(
            "datetime", item.get("event_date", item.get("startsAt", ""))
        )
        event_date = self._parse_date(date_str)

        # Price
        lowest_price = self._safe_float(
            item.get("minPrice", item.get("min_price",
                     item.get("lowestPrice", item.get("starting_price"))))
        )

        # URL
        slug = item.get("slug", item.get("url", ""))
        if slug and not slug.startswith("http"):
            event_url = f"{self.BASE_URL}{slug}" if slug.startswith("/") else f"{self.BASE_URL}/{slug}"
        elif slug:
            event_url = slug
        else:
            event_url = f"{self.BASE_URL}/events/{event_id}"

        return {
            "event_id": event_id,
            "event_name": name,
            "event_date": event_date,
            "venue": venue,
            "city": city,
            "state": state,
            "url": event_url,
            "lowest_price": lowest_price,
            "listing_count": item.get("listingCount", item.get("ticket_count")),
        }

    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get ticket listings for a specific Gametime event.
        Gametime prices are ALL-IN - no fees added.
        Especially strong for last-minute deals.
        """
        # Try API first
        listings = self._get_listings_via_api(event_id, quantity)
        if listings:
            return listings

        # Fallback to scraping
        return self._get_listings_via_scraping(event_id, quantity)

    def _get_listings_via_api(
        self, event_id: str, quantity: int
    ) -> list[TicketListing]:
        """Get listings from Gametime's internal API."""
        listings_url = f"{self.API_URL}/events/{event_id}/listings"
        params = {"quantity": quantity, "sort": "price"}

        try:
            data = self._fetch_json(listings_url, params=params)
        except Exception:
            return []

        if not data:
            return []

        listings = []
        items = data.get("listings", data.get("tickets", data.get("items", [])))
        event_info = data.get("event", {})

        event_name = event_info.get("name", event_info.get("title", "Unknown Event"))
        venue = event_info.get("venue", {}).get("name", "")
        city = event_info.get("venue", {}).get("city", "")
        event_date = self._parse_date(
            event_info.get("datetime", event_info.get("startsAt", ""))
        )
        event_url = f"{self.BASE_URL}/events/{event_id}"

        for item in items[:20]:
            price = self._safe_float(
                item.get("price", item.get("pricePerTicket", item.get("total_price")))
            )
            if not price:
                continue

            # If price is total, divide by quantity
            if item.get("priceType") == "total":
                price = price / max(quantity, 1)

            section = item.get(
                "section", item.get("sectionName", "General Admission")
            )
            row = item.get("row", item.get("rowName"))
            qty = item.get("quantity", item.get("availableQuantity", quantity))

            # Check if this is a "Zone Deal"
            is_zone_deal = item.get("isZoneDeal", item.get("zone_deal", False))
            note = "Gametime: ALL-IN pricing (no fees)"
            if is_zone_deal:
                note = "Gametime ZONE DEAL: ALL-IN, best seats at lowest price"

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
                    total_price_per_ticket=price,  # ALL-IN
                    url=event_url,
                    notes=note,
                )
            )

        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings

    def _get_listings_via_scraping(
        self, event_id: str, quantity: int
    ) -> list[TicketListing]:
        """Fallback: scrape listing data from event page."""
        if event_id.startswith("http"):
            event_url = event_id
        else:
            event_url = f"{self.BASE_URL}/events/{event_id}"

        try:
            html = self._fetch_page(event_url)
        except Exception as e:
            logger.error(f"Gametime listing scrape failed: {e}")
            return []

        if not html:
            return []

        listings = []
        soup = BeautifulSoup(html, "lxml")

        # Get event info from page
        title_el = soup.find("h1")
        event_name = title_el.get_text(strip=True) if title_el else "Unknown Event"

        # Try embedded data first
        for script in soup.find_all("script"):
            if not script.string:
                continue
            if script.get("id") == "__NEXT_DATA__":
                try:
                    data = json.loads(script.string)
                    page_props = data.get("props", {}).get("pageProps", {})
                    listing_data = page_props.get(
                        "listings", page_props.get("tickets", [])
                    )
                    event_data = page_props.get("event", {})

                    if event_data:
                        event_name = event_data.get("name", event_name)

                    for item in listing_data[:20]:
                        price = self._safe_float(
                            item.get("price", item.get("pricePerTicket"))
                        )
                        if not price:
                            continue

                        section = item.get("section", "General Admission")
                        row = item.get("row")

                        listings.append(
                            TicketListing(
                                platform=self.platform_name,
                                event_name=event_name,
                                event_date=datetime.now(),
                                venue="",
                                city="",
                                section=str(section),
                                row=str(row) if row else None,
                                quantity=quantity,
                                price_per_ticket=price,
                                estimated_fees=0.0,
                                total_price_per_ticket=price,
                                url=event_url,
                                notes="Gametime: ALL-IN pricing (no fees)",
                            )
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

        # HTML fallback
        if not listings:
            listing_cards = soup.select(
                '[data-testid="listing"], .listing-card, .ticket-card, '
                '.TicketCard, [data-testid="ticket-row"]'
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
                    '.section, [data-testid*="section"]'
                )
                section = (
                    section_el.get_text(strip=True) if section_el else "General"
                )

                listings.append(
                    TicketListing(
                        platform=self.platform_name,
                        event_name=event_name,
                        event_date=datetime.now(),
                        venue="",
                        city="",
                        section=section,
                        row=None,
                        quantity=quantity,
                        price_per_ticket=price,
                        estimated_fees=0.0,
                        total_price_per_ticket=price,
                        url=event_url,
                        notes="Gametime: ALL-IN - parsed from HTML",
                    )
                )

        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings

    @staticmethod
    def _extract_event_id(url: str) -> Optional[str]:
        """Extract Gametime event ID from URL."""
        # URLs like: /events/abc123 or /event/abc123-name-here
        match = re.search(r"/events?/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_price_from_text(text: str) -> Optional[float]:
        """Extract a price value from text."""
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
            "%a, %b %d",
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

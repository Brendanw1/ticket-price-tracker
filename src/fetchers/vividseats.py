"""
Vivid Seats price fetcher using web scraping.

Vivid Seats has a semi-public API that powers their website. We can
intercept the API calls their frontend makes to get structured listing data.

Fee Structure (approximate):
- Service fee: ~20-30% of ticket price (varies by event)
- Delivery fee: $0-10 (usually waived for mobile delivery)
- Vivid Seats tends to have slightly lower fees than StubHub

Note: Vivid Seats acquired Vegas.com tickets and merged inventory.
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



class VividSeatsFetcher(BaseFetcher):
    """Fetcher for Vivid Seats ticket marketplace via web scraping."""

    BASE_URL = "https://www.vividseats.com"
    # Vivid Seats internal API (used by their frontend)
    API_URL = "https://www.vividseats.com/hermes/api/v1"

    # Vivid Seats fee structure
    BUYER_FEE_PERCENT = 0.22  # ~22% service fee (slightly less than StubHub)
    DELIVERY_FEE = 0.0  # Usually free for mobile delivery

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Vivid Seats fetcher.
        No API key required for scraping approach.
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
                "Accept": "application/json, text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        super().__init__(api_key=api_key)

    def _get_platform_name(self) -> str:
        return "VividSeats"


    def _estimate_fees(self, listed_price: float, quantity: int) -> float:
        """
        Estimate Vivid Seats fees per ticket.
        Vivid Seats charges ~22% service fee + optional delivery fee.
        """
        service_fee = listed_price * self.BUYER_FEE_PERCENT
        delivery_per_ticket = self.DELIVERY_FEE / max(quantity, 1)
        return round(service_fee + delivery_per_ticket, 2)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page with retry logic."""
        try:
            response = self.scraper.get(url, timeout=30)
            # Don't retry on 404/403 — these are definitive, not transient
            if response.status_code in (404, 403):
                logger.debug(f"VividSeats {response.status_code}: {url}")
                return None
            response.raise_for_status()
            return response.text
        except Exception as e:
            if "404" in str(e) or "403" in str(e):
                return None
            logger.warning(f"VividSeats fetch failed for {url}: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        """Fetch JSON data from Vivid Seats internal API."""
        try:
            response = self.scraper.get(url, params=params, timeout=30)
            # Don't retry on 404/403
            if response.status_code in (404, 403):
                logger.debug(f"VividSeats API {response.status_code}: {url}")
                return None
            response.raise_for_status()
            # Guard against non-JSON responses (HTML error pages, etc.)
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "javascript" not in content_type:
                logger.debug(f"VividSeats API returned non-JSON content-type: {content_type}")
                return None
            return response.json()
        except ValueError:
            # JSON decode error — server returned non-JSON (HTML, empty, etc.)
            logger.debug(f"VividSeats API returned non-JSON response for: {url}")
            return None
        except Exception as e:
            if "404" in str(e) or "403" in str(e):
                return None
            logger.warning(f"VividSeats API fetch failed: {e}")
            raise


    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search Vivid Seats for events.
        Uses their internal search API endpoint.
        """
        # Try the internal API first
        events = self._search_via_api(search)
        if events:
            return events

        # Fallback to web scraping
        return self._search_via_scraping(search)

    def _search_via_api(self, search: EventSearch) -> list[dict]:
        """Search using Vivid Seats internal API."""
        search_url = f"{self.API_URL}/search"
        params = {
            "searchTerm": search.query,
            "limit": 25,
        }

        try:
            data = self._fetch_json(search_url, params=params)
        except Exception:
            return []

        if not data:
            return []

        events = []
        items = data.get("items", data.get("events", data.get("results", [])))

        for item in items:
            event_info = {
                "event_id": str(item.get("id", item.get("eventId", ""))),
                "event_name": item.get("name", item.get("title", "")),
                "event_date": self._parse_date(
                    item.get("eventDate", item.get("date", ""))
                ),
                "venue": item.get("venue", {}).get(
                    "name", item.get("venueName", "")
                ),
                "city": item.get("venue", {}).get(
                    "city", item.get("venueCity", "")
                ),
                "state": item.get("venue", {}).get(
                    "state", item.get("venueState", "")
                ),
                "url": self._build_event_url(item),
                "lowest_price": self._safe_float(
                    item.get("minPrice", item.get("minListPrice"))
                ),
                "listing_count": item.get("totalListings", None),
            }
            events.append(event_info)

        # Filter by location if specified
        if search.city:
            city_lower = search.city.lower()
            events = [
                e for e in events
                if city_lower in e.get("city", "").lower()
            ]

        logger.info(f"VividSeats API found {len(events)} events for '{search.query}'")
        return events


    def _search_via_scraping(self, search: EventSearch) -> list[dict]:
        """Fallback: search by scraping the search results page."""
        query_encoded = quote_plus(search.query)
        search_url = f"{self.BASE_URL}/search?searchTerm={query_encoded}"

        try:
            html = self._fetch_page(search_url)
        except Exception as e:
            logger.error(f"VividSeats search scraping failed: {e}")
            return []

        if not html:
            return []

        events = []
        soup = BeautifulSoup(html, "lxml")

        # Try to extract from embedded JSON data
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string and "window.__PRELOADED_STATE__" in script.string:
                try:
                    match = re.search(
                        r"window\.__PRELOADED_STATE__\s*=\s*({.+?});?\s*$",
                        script.string,
                        re.DOTALL,
                    )
                    if match:
                        state = json.loads(match.group(1))
                        search_results = (
                            state.get("search", {}).get("results", [])
                        )
                        for item in search_results:
                            events.append(
                                {
                                    "event_id": str(item.get("id", "")),
                                    "event_name": item.get("name", ""),
                                    "event_date": self._parse_date(
                                        item.get("dateTime", "")
                                    ),
                                    "venue": item.get("venueName", ""),
                                    "city": item.get("venueCity", ""),
                                    "state": item.get("venueState", ""),
                                    "url": f"{self.BASE_URL}{item.get('webPath', '')}",
                                    "lowest_price": self._safe_float(
                                        item.get("minPrice")
                                    ),
                                    "listing_count": item.get(
                                        "totalListings", None
                                    ),
                                }
                            )
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: parse HTML event cards
        if not events:
            event_cards = soup.select(
                '[data-testid="production-listing"], .ProductionCard'
            )
            for card in event_cards[:20]:
                link = card.find("a", href=True)
                if not link:
                    continue

                href = link["href"]
                event_url = (
                    href if href.startswith("http") else f"{self.BASE_URL}{href}"
                )

                name_el = card.find("h3") or card.find("h2") or link
                event_name = name_el.get_text(strip=True) if name_el else ""

                price_el = card.select_one(".price, [data-testid*='price']")
                lowest_price = None
                if price_el:
                    lowest_price = self._parse_price_from_text(price_el.get_text())

                events.append(
                    {
                        "event_id": event_url,
                        "event_name": event_name,
                        "event_date": datetime.now(),
                        "venue": "",
                        "city": "",
                        "url": event_url,
                        "lowest_price": lowest_price,
                        "listing_count": None,
                    }
                )

        logger.info(
            f"VividSeats scraping found {len(events)} events for '{search.query}'"
        )
        return events


    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get ticket listings for a specific Vivid Seats event.
        Tries the internal API first, then falls back to scraping.
        """
        # Try internal listing API
        listings = self._get_listings_via_api(event_id, quantity)
        if listings:
            return listings

        # Fallback to scraping the event page
        return self._get_listings_via_scraping(event_id, quantity)

    def _get_listings_via_api(
        self, event_id: str, quantity: int
    ) -> list[TicketListing]:
        """Get listings using Vivid Seats internal API."""
        listing_url = f"{self.API_URL}/listings"
        params = {
            "eventId": event_id,
            "quantity": quantity,
            "sortBy": "price",
            "sortDirection": "asc",
            "limit": 20,
        }

        try:
            data = self._fetch_json(listing_url, params=params)
        except Exception:
            return []

        if not data:
            return []

        listings = []
        items = data.get("listings", data.get("items", []))
        event_info = data.get("event", {})

        event_name = event_info.get("name", "Unknown Event")
        venue = event_info.get("venue", {}).get("name", "")
        city = event_info.get("venue", {}).get("city", "")
        event_date = self._parse_date(event_info.get("eventDate", ""))
        event_url = f"{self.BASE_URL}/production/{event_id}"

        for item in items:
            price = self._safe_float(
                item.get("price", item.get("listPrice"))
            )
            if not price:
                continue

            section = item.get("section", item.get("sectionName", "Unknown"))
            row = item.get("row", item.get("rowName"))
            qty = item.get("quantity", item.get("availableQuantity", quantity))
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
                    quantity=min(qty, quantity),
                    price_per_ticket=price,
                    estimated_fees=fees,
                    total_price_per_ticket=round(price + fees, 2),
                    url=event_url,
                    notes=f"VividSeats listed + ~{int(self.BUYER_FEE_PERCENT*100)}% estimated fees",
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
            event_url = f"{self.BASE_URL}/production/{event_id}"

        try:
            html = self._fetch_page(event_url)
        except Exception as e:
            logger.error(f"VividSeats listing scrape failed: {e}")
            return []

        if not html:
            return []

        listings = []
        soup = BeautifulSoup(html, "lxml")

        # Try to get data from embedded state
        scripts = soup.find_all("script")
        for script in scripts:
            if not script.string:
                continue
            if "window.__PRELOADED_STATE__" in script.string:
                try:
                    match = re.search(
                        r"window\.__PRELOADED_STATE__\s*=\s*({.+?});?\s*$",
                        script.string,
                        re.DOTALL,
                    )
                    if match:
                        state = json.loads(match.group(1))
                        listing_data = (
                            state.get("listings", {}).get("items", [])
                        )
                        event_data = state.get("event", {})

                        event_name = event_data.get("name", "Unknown")
                        venue = event_data.get("venueName", "")
                        city = event_data.get("venueCity", "")
                        event_date = self._parse_date(
                            event_data.get("dateTime", "")
                        )

                        for item in listing_data[:20]:
                            price = self._safe_float(item.get("price"))
                            if not price:
                                continue
                            section = item.get("section", "Unknown")
                            row = item.get("row")
                            fees = self._estimate_fees(price, quantity)

                            listings.append(
                                TicketListing(
                                    platform=self.platform_name,
                                    event_name=event_name,
                                    event_date=event_date,
                                    venue=venue,
                                    city=city,
                                    section=str(section),
                                    row=str(row) if row else None,
                                    quantity=quantity,
                                    price_per_ticket=price,
                                    estimated_fees=fees,
                                    total_price_per_ticket=round(
                                        price + fees, 2
                                    ),
                                    url=event_url,
                                    notes="VividSeats scraped + estimated fees",
                                )
                            )
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: parse HTML listing elements
        if not listings:
            title_el = soup.find("h1")
            event_name = (
                title_el.get_text(strip=True) if title_el else "Unknown Event"
            )

            listing_rows = soup.select(
                '[data-testid="listing-row"], .TicketRow, .listing-row'
            )
            for row_el in listing_rows[:15]:
                price_el = row_el.select_one(
                    '.price, [data-testid*="price"]'
                )
                if not price_el:
                    continue
                price = self._parse_price_from_text(price_el.get_text())
                if not price:
                    continue

                section_el = row_el.select_one(
                    '.section, [data-testid*="section"]'
                )
                section = (
                    section_el.get_text(strip=True) if section_el else "Unknown"
                )
                fees = self._estimate_fees(price, quantity)

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
                        estimated_fees=fees,
                        total_price_per_ticket=round(price + fees, 2),
                        url=event_url,
                        notes="VividSeats HTML parsed - verify on site",
                    )
                )

        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings


    def _build_event_url(self, item: dict) -> str:
        """Build a full event URL from API response data."""
        web_path = item.get("webPath", item.get("url", ""))
        if web_path.startswith("http"):
            return web_path
        return f"{self.BASE_URL}{web_path}" if web_path else self.BASE_URL

    def _parse_price_from_text(self, text: str) -> Optional[float]:
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

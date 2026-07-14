"""
Ticketmaster Resale price fetcher using web scraping.

Ticketmaster's official resale marketplace has unique inventory that often
doesn't appear on third-party platforms. Season ticket holders and verified
fans frequently list here first, making it a valuable source for otherwise
hard-to-find tickets.

Key Differentiators:
- Official resale channel (tickets are verified/transferred instantly)
- Unique inventory from season ticket holders and verified fan resale
- Integrated with the primary ticket purchase flow
- Strong for sports (NFL, NBA, NHL, MLB) and major concerts

Fee Structure (approximate):
- Service fee: ~20-25% of ticket price
- Order processing fee: $5-15 per order
- Facility charge: sometimes included (venue-specific)
- Total fees typically 22-30% on top of listed price

Technical Approach:
- Ticketmaster Discovery API (free tier available for event search)
- Web scraping for resale listings (their resale pages are on ticketmaster.com)
- Falls back to HTML parsing when API data is unavailable

API Documentation:
- Discovery API: https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/
- Free tier: 5000 API calls/day, rate limited to 5 requests/second
"""

import logging
import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

import requests
import cloudscraper
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseFetcher, EventSearch, TicketListing

logger = logging.getLogger(__name__)


class TicketmasterFetcher(BaseFetcher):
    """Fetcher for Ticketmaster Resale marketplace."""

    BASE_URL = "https://www.ticketmaster.com"
    DISCOVERY_API_URL = "https://app.ticketmaster.com/discovery/v2"

    # Ticketmaster Resale fee structure
    BUYER_FEE_PERCENT = 0.22  # ~22% service fee
    ORDER_PROCESSING_FEE = 8.50  # Per order flat fee

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Ticketmaster fetcher.

        Args:
            api_key: Ticketmaster Discovery API key (free at developer.ticketmaster.com).
                     Optional - scraping works without it, but API is more reliable.
        """
        self.discovery_api_key = api_key
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
        return "Ticketmaster"

    def _estimate_fees(self, listed_price: float, quantity: int) -> float:
        """
        Estimate Ticketmaster fees per ticket.
        ~22% service fee + per-order processing fee split across tickets.
        """
        service_fee = listed_price * self.BUYER_FEE_PERCENT
        processing_per_ticket = self.ORDER_PROCESSING_FEE / max(quantity, 1)
        return round(service_fee + processing_per_ticket, 2)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def _api_request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make a request to the Ticketmaster Discovery API."""
        if not self.discovery_api_key:
            return None

        url = f"{self.DISCOVERY_API_URL}/{endpoint}"
        request_params = {"apikey": self.discovery_api_key}
        if params:
            request_params.update(params)

        try:
            response = requests.get(url, params=request_params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                logger.warning("Ticketmaster API rate limit hit")
            else:
                logger.error(f"Ticketmaster API error: {e}")
            raise
        except Exception as e:
            logger.error(f"Ticketmaster API request failed: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page with retry logic."""
        try:
            response = self.scraper.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"Ticketmaster fetch failed for {url}: {e}")
            raise

    def search_events(self, search: EventSearch) -> list[dict]:
        """
        Search Ticketmaster for events.
        Uses the Discovery API if an API key is configured,
        otherwise falls back to web scraping.
        """
        # Try Discovery API first (more reliable, structured data)
        if self.discovery_api_key:
            events = self._search_via_api(search)
            if events:
                return events

        # Fallback to scraping
        return self._search_via_scraping(search)

    def _search_via_api(self, search: EventSearch) -> list[dict]:
        """Search using Ticketmaster Discovery API."""
        params = {
            "keyword": search.query,
            "size": 25,
            "sort": "date,asc",
            "source": "ticketmaster",
        }

        # Location filter
        if search.city:
            params["city"] = search.city
        if search.state:
            params["stateCode"] = search.state

        # Date filters
        if search.date_from:
            params["startDateTime"] = search.date_from.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if search.date_to:
            params["endDateTime"] = search.date_to.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        # Event type mapping
        if search.event_type:
            segment_map = {
                "sports": "KZFzniwnSyZfZ7v7nE",  # Sports segment ID
                "concert": "KZFzniwnSyZfZ7v7nJ",  # Music segment ID
                "theater": "KZFzniwnSyZfZ7v7na",  # Arts & Theatre
            }
            segment_id = segment_map.get(search.event_type)
            if segment_id:
                params["segmentId"] = segment_id

        try:
            data = self._api_request("events.json", params)
        except Exception:
            return []

        if not data:
            return []

        events = []
        embedded = data.get("_embedded", {})
        event_list = embedded.get("events", [])

        for item in event_list:
            # Check if event has resale tickets
            # Ticketmaster marks events with resale availability
            event_info = self._parse_discovery_event(item)
            if event_info:
                events.append(event_info)

        logger.info(
            f"Ticketmaster API found {len(events)} events for '{search.query}'"
        )
        return events

    def _parse_discovery_event(self, item: dict) -> Optional[dict]:
        """Parse a single event from Discovery API response."""
        event_id = item.get("id", "")
        name = item.get("name", "")

        # Get venue info
        venues = (
            item.get("_embedded", {}).get("venues", [])
        )
        venue_info = venues[0] if venues else {}
        venue_name = venue_info.get("name", "")
        city = venue_info.get("city", {}).get("name", "")
        state = venue_info.get("state", {}).get("stateCode", "")

        # Get date
        dates = item.get("dates", {}).get("start", {})
        date_str = dates.get("dateTime", dates.get("localDate", ""))
        event_date = self._parse_date(date_str)

        # Get price range (if available)
        price_ranges = item.get("priceRanges", [])
        lowest_price = None
        if price_ranges:
            prices = [
                self._safe_float(pr.get("min"))
                for pr in price_ranges
                if pr.get("type") == "standard" or not pr.get("type")
            ]
            prices = [p for p in prices if p is not None]
            lowest_price = min(prices) if prices else None

        # Build URL - resale tickets are on the same event page
        event_url = item.get("url", f"{self.BASE_URL}/event/{event_id}")

        return {
            "event_id": event_id,
            "event_name": name,
            "event_date": event_date,
            "venue": venue_name,
            "city": city,
            "state": state,
            "url": event_url,
            "lowest_price": lowest_price,
            "listing_count": None,
            "has_resale": self._check_resale_flag(item),
        }

    def _check_resale_flag(self, item: dict) -> bool:
        """Check if a Ticketmaster event has resale tickets available."""
        # The sales field sometimes indicates resale availability
        sales = item.get("sales", {})
        if "resale" in sales or "presales" in sales:
            return True
        # Check ticket limit info
        ticket_limit = item.get("ticketLimit", {})
        if ticket_limit.get("info"):
            return True
        return True  # Default to True - we'll verify when fetching listings

    def _search_via_scraping(self, search: EventSearch) -> list[dict]:
        """Fallback: search by scraping Ticketmaster's website."""
        query_encoded = quote_plus(search.query)
        search_url = f"{self.BASE_URL}/search?q={query_encoded}"

        if search.city:
            search_url += f"&city={quote_plus(search.city)}"

        try:
            html = self._fetch_page(search_url)
        except Exception as e:
            logger.error(f"Ticketmaster search scraping failed: {e}")
            return []

        if not html:
            return []

        events = []
        soup = BeautifulSoup(html, "lxml")

        # Try JSON-LD first
        json_ld_scripts = soup.find_all("script", type="application/ld+json")
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Event":
                        events.append(self._parse_json_ld_event(item))
            except (json.JSONDecodeError, TypeError):
                continue

        # Try __NEXT_DATA__
        if not events:
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    data = json.loads(next_script.string)
                    page_props = data.get("props", {}).get("pageProps", {})
                    event_list = (
                        page_props.get("events", [])
                        or page_props.get("searchResults", {}).get("events", [])
                    )
                    for item in event_list:
                        event = self._parse_scraped_event(item)
                        if event:
                            events.append(event)
                except (json.JSONDecodeError, TypeError):
                    pass

        # HTML fallback
        if not events:
            events = self._parse_search_html(soup)

        # Filter by location
        if search.city:
            city_lower = search.city.lower()
            events = [
                e for e in events
                if city_lower in e.get("city", "").lower()
                or city_lower in e.get("venue", "").lower()
            ]

        logger.info(
            f"Ticketmaster scraping found {len(events)} events for '{search.query}'"
        )
        return events

    def _parse_json_ld_event(self, item: dict) -> dict:
        """Parse a JSON-LD Event object from Ticketmaster."""
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

        event_url = item.get("url", "")

        return {
            "event_id": self._extract_event_id(event_url) or event_url,
            "event_name": item.get("name", ""),
            "event_date": self._parse_date(item.get("startDate", "")),
            "venue": location.get("name", ""),
            "city": address.get("addressLocality", ""),
            "state": address.get("addressRegion", ""),
            "url": event_url if event_url.startswith("http") else f"{self.BASE_URL}{event_url}",
            "lowest_price": lowest_price,
            "listing_count": None,
        }

    def _parse_scraped_event(self, item: dict) -> Optional[dict]:
        """Parse an event from Ticketmaster's page data."""
        if not item:
            return None

        event_id = str(item.get("id", item.get("eventId", "")))
        name = item.get("name", item.get("title", ""))

        venue_data = item.get("venue", {})
        if isinstance(venue_data, dict):
            venue = venue_data.get("name", "")
            city = venue_data.get("city", "")
            state = venue_data.get("state", venue_data.get("stateCode", ""))
        else:
            venue = item.get("venueName", "")
            city = item.get("city", "")
            state = item.get("state", "")

        date_str = item.get("date", item.get("eventDate", item.get("startDate", "")))
        event_date = self._parse_date(date_str)

        lowest_price = self._safe_float(
            item.get("minPrice", item.get("lowestPrice"))
        )

        url = item.get("url", "")
        if url and not url.startswith("http"):
            url = f"{self.BASE_URL}{url}"
        elif not url:
            url = f"{self.BASE_URL}/event/{event_id}"

        return {
            "event_id": event_id,
            "event_name": name,
            "event_date": event_date,
            "venue": venue,
            "city": city,
            "state": state,
            "url": url,
            "lowest_price": lowest_price,
            "listing_count": None,
        }

    def _parse_search_html(self, soup: BeautifulSoup) -> list[dict]:
        """Parse search results from HTML."""
        events = []

        event_cards = soup.select(
            '[data-testid="event-card"], .event-listing__item, '
            '.search-event-card, a[href*="/event/"]'
        )

        for card in event_cards[:20]:
            link = card if card.name == "a" else card.find("a", href=True)
            if not link or not link.get("href"):
                continue

            href = link["href"]
            event_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            event_id = self._extract_event_id(event_url)

            name_el = card.find("h3") or card.find("h2") or card.find(".event-name")
            event_name = name_el.get_text(strip=True) if name_el else link.get_text(strip=True)

            price_el = card.select_one('.price, [data-testid*="price"]')
            lowest_price = None
            if price_el:
                lowest_price = self._parse_price_from_text(price_el.get_text())

            venue_el = card.select_one('.venue, .venue-name')
            venue = venue_el.get_text(strip=True) if venue_el else ""

            events.append({
                "event_id": event_id or event_url,
                "event_name": event_name,
                "event_date": datetime.now(),
                "venue": venue,
                "city": "",
                "state": "",
                "url": event_url,
                "lowest_price": lowest_price,
                "listing_count": None,
            })

        return events

    def get_listings(self, event_id: str, quantity: int = 2) -> list[TicketListing]:
        """
        Get resale ticket listings for a specific Ticketmaster event.
        Scrapes the event page for resale ticket data.
        """
        # Build event URL
        if event_id.startswith("http"):
            event_url = event_id
        else:
            event_url = f"{self.BASE_URL}/event/{event_id}"

        try:
            html = self._fetch_page(event_url)
        except Exception as e:
            logger.error(f"Ticketmaster listing fetch failed: {e}")
            return []

        if not html:
            return []

        listings = []
        soup = BeautifulSoup(html, "lxml")

        # Try embedded page data
        listings = self._parse_listings_from_page_data(soup, quantity, event_url)

        # Try JSON-LD
        if not listings:
            listings = self._parse_listings_from_json_ld(soup, quantity)

        # HTML fallback
        if not listings:
            listings = self._parse_listing_html(soup, quantity, event_url)

        listings.sort(key=lambda x: x.total_price_per_ticket)
        return listings

    def _parse_listings_from_page_data(
        self, soup: BeautifulSoup, quantity: int, event_url: str
    ) -> list[TicketListing]:
        """Parse listings from embedded page data."""
        listings = []

        next_script = soup.find("script", id="__NEXT_DATA__")
        if not next_script or not next_script.string:
            return []

        try:
            data = json.loads(next_script.string)
            page_props = data.get("props", {}).get("pageProps", {})

            event_data = page_props.get("event", page_props.get("eventData", {}))
            listing_data = (
                page_props.get("resaleListings", [])
                or page_props.get("listings", [])
                or page_props.get("offers", [])
            )

            if not event_data:
                return []

            event_name = event_data.get("name", event_data.get("title", "Unknown"))
            venue = event_data.get("venue", {}).get(
                "name", event_data.get("venueName", "")
            )
            city = event_data.get("venue", {}).get(
                "city", event_data.get("city", "")
            )
            event_date = self._parse_date(
                event_data.get("dates", {}).get("start", {}).get("dateTime", "")
                or event_data.get("date", "")
            )

            if isinstance(listing_data, list):
                for item in listing_data[:20]:
                    price = self._safe_float(
                        item.get("price", item.get("currentPrice", {}).get("amount"))
                        or item.get("listPrice")
                    )
                    if not price:
                        continue

                    section = item.get(
                        "section", item.get("sectionName", "Resale - General")
                    )
                    row = item.get("row", item.get("rowName"))
                    qty = item.get("quantity", quantity)
                    fees = self._estimate_fees(price, qty)

                    # Check if this is specifically a resale listing
                    is_resale = item.get("isResale", item.get("resale", True))
                    listing_type = "Resale" if is_resale else "Primary"

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
                            notes=f"Ticketmaster {listing_type}: ~{int(self.BUYER_FEE_PERCENT*100)}% service fee + ${self.ORDER_PROCESSING_FEE} order fee",
                        )
                    )
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.debug(f"Error parsing Ticketmaster page data: {e}")

        return listings

    def _parse_listings_from_json_ld(
        self, soup: BeautifulSoup, quantity: int
    ) -> list[TicketListing]:
        """Parse listings from JSON-LD structured data."""
        listings = []

        json_ld_scripts = soup.find_all("script", type="application/ld+json")
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "Event":
                    event_name = data.get("name", "Unknown")
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
                        high = self._safe_float(offers.get("highPrice"))

                        if low:
                            fees = self._estimate_fees(low, quantity)
                            listings.append(
                                TicketListing(
                                    platform=self.platform_name,
                                    event_name=event_name,
                                    event_date=event_date,
                                    venue=venue,
                                    city=city,
                                    section="Best Available (Resale)",
                                    row=None,
                                    quantity=quantity,
                                    price_per_ticket=low,
                                    estimated_fees=fees,
                                    total_price_per_ticket=round(low + fees, 2),
                                    url=data.get("url", ""),
                                    notes=f"Ticketmaster Resale: ~{int(self.BUYER_FEE_PERCENT*100)}% fees estimated",
                                )
                            )

                        if high and high != low:
                            mid = (low + high) / 2
                            fees = self._estimate_fees(mid, quantity)
                            listings.append(
                                TicketListing(
                                    platform=self.platform_name,
                                    event_name=event_name,
                                    event_date=event_date,
                                    venue=venue,
                                    city=city,
                                    section="Mid-Range (Resale)",
                                    row=None,
                                    quantity=quantity,
                                    price_per_ticket=round(mid, 2),
                                    estimated_fees=fees,
                                    total_price_per_ticket=round(mid + fees, 2),
                                    url=data.get("url", ""),
                                    notes=f"Ticketmaster Resale: ~{int(self.BUYER_FEE_PERCENT*100)}% fees estimated",
                                )
                            )
            except (json.JSONDecodeError, TypeError):
                continue

        return listings

    def _parse_listing_html(
        self, soup: BeautifulSoup, quantity: int, event_url: str
    ) -> list[TicketListing]:
        """Parse listings directly from HTML."""
        listings = []

        title_el = soup.find("h1")
        event_name = title_el.get_text(strip=True) if title_el else "Unknown Event"

        # Look for resale listing elements
        listing_cards = soup.select(
            '[data-testid="resale-listing"], [data-testid="offer-card"], '
            '.resale-listing, .offer-card, [data-testid="ticket-card"]'
        )

        for card in listing_cards[:15]:
            price_el = card.select_one(
                '.price, [data-testid*="price"], .offer-price'
            )
            if not price_el:
                continue

            price = self._parse_price_from_text(price_el.get_text())
            if not price:
                continue

            section_el = card.select_one(
                '.section, [data-testid*="section"], .offer-section'
            )
            row_el = card.select_one(
                '.row, [data-testid*="row"], .offer-row'
            )

            section = section_el.get_text(strip=True) if section_el else "Resale"
            row = row_el.get_text(strip=True) if row_el else None
            fees = self._estimate_fees(price, quantity)

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
                    estimated_fees=fees,
                    total_price_per_ticket=round(price + fees, 2),
                    url=event_url,
                    notes="Ticketmaster Resale: parsed from HTML, verify fees on site",
                )
            )

        return listings

    @staticmethod
    def _extract_event_id(url: str) -> Optional[str]:
        """Extract Ticketmaster event ID from URL."""
        # URLs like: /event/Z7r9jZ1A7a0e2 or /event/name-here/Z7r9jZ1A7a0e2
        match = re.search(r"/event/(?:[^/]+/)?([A-Za-z0-9]+)(?:\?|$|#)", url)
        if match:
            return match.group(1)
        # Try pattern with just alphanumeric ID at end
        match = re.search(r"([A-Z][A-Za-z0-9]{10,20})(?:\?|$|#)", url)
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
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y %I:%M %p",
            "%b %d, %Y",
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

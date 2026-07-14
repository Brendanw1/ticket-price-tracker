# Ticket Price Tracker

A Python-based ticket price monitoring system that tracks resale prices across **6 major platforms** — SeatGeek, TickPick, Gametime, StubHub, Vivid Seats, and Ticketmaster Resale — compares true all-in costs (including hidden fees), and sends **SMS alerts** when prices drop below your target.

## Why This Exists

Resale ticket platforms each have different fee structures that make it nearly impossible to compare prices at a glance:

| Platform | Listed Price | Hidden Fees | You Actually Pay |
|----------|-------------|-------------|------------------|
| **TickPick** | $100 | $0 (zero fees!) | **$100** |
| **SeatGeek** | $100 | $0 (all-in) | **$100** |
| **Gametime** | $100 | $0 (all-in) | **$100** |
| Vivid Seats | $100 | ~$22 (22%) | **$122** |
| Ticketmaster | $100 | ~$27 (22% + $8.50) | **$127** |
| StubHub | $100 | ~$29 (25% + $7.95) | **$129** |

This tracker normalizes everything to **true all-in cost** so you can make apples-to-apples comparisons and never overpay.

## Features

- **6-platform monitoring** — SeatGeek, TickPick, Gametime, StubHub, Vivid Seats, Ticketmaster Resale
- **Fee normalization** — Shows true all-in price accounting for each platform's fee structure
- **SMS alerts via Email-to-SMS gateway** — 100% free, no Twilio, uses your Gmail/Outlook
- **Smart alerting** — Price drop alerts, new all-time low alerts, buy/wait recommendations
- **Price history tracking** — Records prices over time to identify trends
- **Trend analysis** — Tells you if prices are dropping (wait) or rising (buy now)
- **Rate limiting** — Won't spam you; respects quiet hours (no 3AM texts)
- **Scheduled monitoring** — Runs continuously or as a one-shot check
- **Zero-fee platform priority** — Highlights TickPick, SeatGeek, and Gametime deals first

## Quick Start

### 1. Clone and Install

```bash
git clone https://github.com/Brendanw1/ticket-price-tracker.git
cd ticket-price-tracker

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Get Your API Keys

Only SeatGeek requires a key. The others work via web scraping out of the box.

| Service | Required? | Free Tier | Link |
|---------|-----------|-----------|------|
| **SeatGeek API** | Yes | 1000 req/day | https://seatgeek.com/account/develop |
| **Gmail (for SMS)** | Yes (for alerts) | Free forever | https://myaccount.google.com/apppasswords |
| **Ticketmaster API** | Optional | 5000 req/day | https://developer.ticketmaster.com |

> **Note**: SMS alerts use the free Email-to-SMS gateway (no Twilio, no paid services). You just need a Gmail account with an App Password.

#### SMS Setup (Free via Email-to-SMS Gateway)

Instead of Twilio, we use carrier email gateways. Every carrier converts emails sent to `yournumber@carrier-gateway.com` into SMS texts — completely free.

**Gmail Setup (recommended):**
1. Enable 2-Factor Authentication on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password for "Mail"
4. Copy the 16-character password (e.g., `abcd efgh ijkl mnop`)

**Find your carrier gateway:**

| Carrier | Gateway Format |
|---------|---------------|
| AT&T | number@txt.att.net |
| T-Mobile / Mint | number@tmomail.net |
| Verizon / Visible | number@vtext.com |
| Sprint | number@messaging.sprintpcs.com |
| Cricket | number@sms.cricketwireless.net |
| Metro PCS | number@mymetropcs.com |
| Boost Mobile | number@sms.myboostmobile.com |
| Google Fi | number@msg.fi.google.com |
| US Cellular | number@email.uscc.net |
| Rogers (CA) | number@pcs.rogers.com |
| Bell (CA) | number@txt.bell.ca |
| Telus (CA) | number@msg.telus.com |

In your `.env`, just specify the carrier name (e.g., `verizon`, `att`, `tmobile`) and the tracker handles the rest.

### 3. Configure

**Option A: Interactive Setup (Recommended)**
```bash
python setup.py
```

**Option B: Manual Setup**
```bash
# Create your .env file
cp .env.example .env
# Edit .env with your actual credentials
nano .env

# Edit the config to add your events
nano config/config.yaml
```

### 4. Test It

```bash
# Verify SMS is working
python main.py --test-sms

# Run a single price check
python main.py

# Check status
python main.py --status
```

### 5. Start Monitoring

```bash
# Run continuously (checks every 30 min by default)
python main.py --schedule

# Or with verbose logging
python main.py --schedule --verbose
```

## Usage

### Command Line Interface

```bash
# Single price check (run once and exit)
python main.py

# Continuous scheduled monitoring
python main.py --schedule

# Send test SMS to verify setup
python main.py --test-sms

# Show tracker status
python main.py --status

# Add an event to watch via CLI
python main.py --add "Lakers vs Celtics" --max-price 150 --city "Los Angeles" --qty 2
python main.py --add "Bad Bunny" --max-price 200 --event-type concert

# Use a custom config file
python main.py --config /path/to/my-config.yaml

# Verbose logging (debug mode)
python main.py --verbose
```

### Configuration (config/config.yaml)

```yaml
# Check prices every 15 minutes
check_interval_minutes: 15

# Enable/disable platforms individually
platforms:
  seatgeek:
    enabled: true
    client_id: "${SEATGEEK_CLIENT_ID}"
  tickpick:
    enabled: true        # No key needed - zero fees!
  gametime:
    enabled: true        # No key needed - last-minute deals
  stubhub:
    enabled: true        # No key needed
  vividseats:
    enabled: true        # No key needed
  ticketmaster:
    enabled: true
    api_key: "${TICKETMASTER_API_KEY}"  # Optional, improves results

# Events to monitor
watched_events:
  - query: "Lakers vs Celtics"
    max_price: 150          # Alert when all-in price <= $150
    city: "Los Angeles"
    quantity: 2
    event_type: "sports"

  - query: "Taylor Swift Eras Tour"
    max_price: 250
    quantity: 2
    event_type: "concert"

  - query: "Hamilton Broadway"
    max_price: 200
    city: "New York"
    event_type: "theater"
```

## How Alerts Work

You'll receive SMS alerts in three scenarios:

### 1. Price Drop Alert
Triggers when the best available price drops **below your target price**.

```
PRICE DROP ALERT!
Lakers vs Celtics
$135/ticket (all-in) on TickPick
$15 BELOW your $150 target (10% under)
Section: Upper Level
Qty: 2
Trend: Prices dropping (may go lower)
https://tickpick.com/...
```

### 2. New All-Time Low
Triggers when a price is the **lowest ever recorded** for that event.

```
NEW LOW PRICE!
Taylor Swift Eras Tour
$210/ticket on Gametime
Down $35 (14%) from previous low
Section: Mid-Level Seating
https://gametime.co/...
```

### 3. Buy Recommendation
Triggers when trend analysis suggests it's an **optimal time to buy**.

```
BUY RECOMMENDATION
Lakers vs Celtics
$142/ticket (TickPick)
Currently 15% below average
Avg price: $167
Section: Best Available
https://tickpick.com/...
```

## Project Structure

```
ticket-price-tracker/
├── main.py                    # Entry point with CLI
├── setup.py                   # Interactive setup wizard
├── requirements.txt           # Python dependencies
├── .env.example              # Template for credentials
├── .gitignore
├── config/
│   └── config.yaml           # Main configuration
├── data/
│   ├── price_history.json    # Price tracking database (auto-created)
│   └── tracker.log           # Application log (auto-created)
└── src/
    ├── __init__.py
    ├── tracker.py            # Main orchestrator (6 platforms)
    ├── fetchers/
    │   ├── __init__.py
    │   ├── base.py           # Base classes (TicketListing, EventSearch)
    │   ├── seatgeek.py       # SeatGeek API fetcher
    │   ├── tickpick.py       # TickPick scraper (zero fees!)
    │   ├── gametime.py       # Gametime scraper (last-minute deals)
    │   ├── stubhub.py        # StubHub web scraper
    │   ├── vividseats.py     # Vivid Seats scraper
    │   └── ticketmaster.py   # Ticketmaster Resale (API + scraping)
    ├── alerts/
    │   ├── __init__.py
    │   └── sms.py            # Twilio SMS alert system
    └── utils/
        ├── __init__.py
        ├── fee_calculator.py  # 6-platform fee comparison engine
        └── price_history.py   # TinyDB price tracking & trends
```

## Running 24/7

### Option A: Using cron (Linux/Mac)

```bash
# Check every 30 minutes
crontab -e
# Add this line:
*/30 * * * * cd /path/to/ticket-price-tracker && /path/to/venv/bin/python main.py >> data/cron.log 2>&1
```

### Option B: Using systemd (Linux)

Create `/etc/systemd/system/ticket-tracker.service`:

```ini
[Unit]
Description=Ticket Price Tracker
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/ticket-price-tracker
ExecStart=/path/to/venv/bin/python main.py --schedule
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable ticket-tracker
sudo systemctl start ticket-tracker
sudo systemctl status ticket-tracker
```

### Option C: Docker (coming soon)

## Buying Strategy Tips

This tracker is most effective when combined with smart timing:

| Strategy | When to Use | Expected Savings |
|----------|-------------|-----------------|
| **Last-minute (24-72h before)** | Flexible schedule, OK with any seats | 15-30% |
| **Dead zone (2-4 weeks after on-sale)** | Want seat selection + good price | 10-20% |
| **Weather drops (outdoor events)** | Rain in forecast, flexible | 20-40% |
| **Weekday games** | Any Tue/Wed/Thu event | 10-25% |
| **Losing streaks (sports)** | Team on 3+ game skid | 15-35% |
| **Use zero-fee platforms** | Always check TickPick/Gametime first | 20-30% vs StubHub |

The tracker helps you **automate the monitoring** so you don't miss these windows.

## Platform Notes

### TickPick (NEW)
- **Pricing**: ZERO buyer fees — listed price is final price
- **Data Source**: Web scraping (cloudscraper)
- **Best For**: Cheapest all-in price, especially when listed price matches other platforms

### SeatGeek
- **Pricing**: All-in (what you see = what you pay)
- **Data Source**: Official API (most reliable)
- **Rate Limit**: ~1000 requests/day on free tier
- **Best For**: Most accurate pricing, good deal scores

### Gametime (NEW)
- **Pricing**: All-in (no hidden fees)
- **Data Source**: Internal API + web scraping
- **Best For**: Last-minute deals (24-72h before), Zone Deals feature

### StubHub
- **Pricing**: Listed price + ~25% fees (calculated by tracker)
- **Data Source**: Web scraping (may occasionally be blocked by Cloudflare)
- **Best For**: Largest inventory, especially for popular events

### Vivid Seats
- **Pricing**: Listed price + ~22% fees (calculated by tracker)
- **Data Source**: Internal API + web scraping fallback
- **Best For**: Slightly lower fees than StubHub, good sports inventory

### Ticketmaster Resale (NEW)
- **Pricing**: Listed price + ~22% service fee + $8.50 order processing
- **Data Source**: Discovery API (free tier) + web scraping
- **Rate Limit**: 5000 API calls/day on free tier
- **Best For**: Unique inventory from season ticket holders, official verified transfers

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No platform fetchers initialized" | Check your SeatGeek API key in .env |
| SMS not sending | Run `python main.py --test-sms`; check email/password and carrier name |
| SMTP authentication failed | For Gmail, use an App Password (not your regular password). Enable 2FA first |
| SMS delayed or not received | Check carrier name is correct; try sending a test email directly to the gateway |
| StubHub returning no results | Cloudflare may be blocking; try again in a few minutes |
| "Rate limit reached" | Reduce `check_interval_minutes` or increase `max_alerts_per_hour` |
| No listings found | Try a broader search query or remove city/state filters |
| TickPick/Gametime not returning data | Sites may have updated their HTML structure; check logs |
| Ticketmaster API errors | Verify your API key; free tier allows 5 requests/second |
| "Unsupported carrier" error | Check spelling matches one of: att, tmobile, verizon, sprint, etc. |

## Cost

- **SeatGeek API**: Free (1000 req/day)
- **Ticketmaster API**: Free (5000 req/day, optional)
- **TickPick/Gametime/StubHub/VividSeats scraping**: Free
- **SMS Alerts**: FREE (Email-to-SMS gateway — uses your existing email, no paid service)
- **Server/hosting**: Free if running on your own machine; ~$5/month on a VPS

**Total cost: $0** (unless you want a VPS for 24/7 uptime)

## License

MIT - Use it, modify it, save money on tickets.

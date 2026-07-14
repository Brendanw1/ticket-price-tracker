# Ticket Price Tracker

A Python-based ticket price monitoring system that tracks resale prices across **SeatGeek**, **StubHub**, and **Vivid Seats**, compares true all-in costs (including hidden fees), and sends **SMS alerts** when prices drop below your target.

## Why This Exists

Resale ticket platforms each have different fee structures that make it nearly impossible to compare prices at a glance:

| Platform | Listed Price | Hidden Fees | You Actually Pay |
|----------|-------------|-------------|------------------|
| SeatGeek | $100 | $0 (all-in) | **$100** |
| StubHub | $100 | ~$27 (25% + $7.95) | **$127** |
| Vivid Seats | $100 | ~$22 (22%) | **$122** |

This tracker normalizes everything to **true all-in cost** so you can make apples-to-apples comparisons and never overpay.

## Features

- **Multi-platform monitoring** - SeatGeek (API), StubHub (scraping), Vivid Seats (scraping)
- **Fee normalization** - Shows true all-in price accounting for each platform's fee structure
- **SMS alerts via Twilio** - Get notified instantly when prices hit your target
- **Smart alerting** - Price drop alerts, new all-time low alerts, buy/wait recommendations
- **Price history tracking** - Records prices over time to identify trends
- **Trend analysis** - Tells you if prices are dropping (wait) or rising (buy now)
- **Rate limiting** - Won't spam you; respects quiet hours (no 3AM texts)
- **Scheduled monitoring** - Runs continuously or as a one-shot check

## Quick Start

### 1. Clone and Install

```bash
git clone <this-repo>
cd ticket-tracker

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Get Your API Keys

You need two things (both have free tiers):

#### SeatGeek API Key (Free)
1. Go to https://seatgeek.com/account/develop
2. Create an application
3. Copy your **Client ID** (and optionally Client Secret)

#### Twilio Account (Free Trial = $15 credit)
1. Sign up at https://www.twilio.com
2. Get a phone number from Twilio
3. Verify your personal phone number
4. Copy your **Account SID**, **Auth Token**, and **Twilio phone number**

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

# Events to monitor
watched_events:
  - query: "Lakers vs Celtics"
    max_price: 150          # Alert when all-in price is at or below $150
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
$135/ticket (all-in) on SeatGeek
$15 BELOW your $150 target (10% under)
Section: Upper Level
Qty: 2
Trend: Prices dropping (may go lower)
https://seatgeek.com/...
```

### 2. New All-Time Low
Triggers when a price is the **lowest ever recorded** for that event.

```
NEW LOW PRICE!
Taylor Swift Eras Tour
$220/ticket on VividSeats
Down $30 (12%) from previous low
Section: Mid-Level Seating
https://vividseats.com/...
```

### 3. Buy Recommendation
Triggers when trend analysis suggests it's an **optimal time to buy**.

```
BUY RECOMMENDATION
Lakers vs Celtics
$142/ticket (SeatGeek)
Currently 15% below average
Avg price: $167
Section: Best Available
https://seatgeek.com/...
```

## Project Structure

```
ticket-tracker/
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
    ├── tracker.py            # Main orchestrator
    ├── fetchers/
    │   ├── __init__.py
    │   ├── base.py           # Base classes (TicketListing, EventSearch)
    │   ├── seatgeek.py       # SeatGeek API fetcher
    │   ├── stubhub.py        # StubHub web scraper
    │   └── vividseats.py     # Vivid Seats scraper
    ├── alerts/
    │   ├── __init__.py
    │   └── sms.py            # Twilio SMS alert system
    └── utils/
        ├── __init__.py
        ├── fee_calculator.py  # Multi-platform fee comparison
        └── price_history.py   # TinyDB price tracking & trends
```

## Running 24/7

### Option A: Using cron (Linux/Mac)

```bash
# Check every 30 minutes
crontab -e
# Add this line:
*/30 * * * * cd /path/to/ticket-tracker && /path/to/venv/bin/python main.py >> data/cron.log 2>&1
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
WorkingDirectory=/path/to/ticket-tracker
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

The tracker helps you **automate the monitoring** so you don't miss these windows.

## Platform Notes

### SeatGeek
- **Pricing**: All-in (what you see = what you pay)
- **Data Source**: Official API (most reliable)
- **Rate Limit**: ~1000 requests/day on free tier
- **Best For**: Most accurate pricing, good deal scores

### StubHub
- **Pricing**: Listed price + ~25% fees (calculated by tracker)
- **Data Source**: Web scraping (may occasionally be blocked)
- **Best For**: Large inventory, especially for popular events

### Vivid Seats
- **Pricing**: Listed price + ~22% fees (calculated by tracker)
- **Data Source**: Internal API + web scraping fallback
- **Best For**: Often has slightly lower fees than StubHub

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No platform fetchers initialized" | Check your SeatGeek API key in .env |
| SMS not sending | Run `python main.py --test-sms` and check Twilio console for errors |
| StubHub returning no results | Cloudflare may be blocking; try again in a few minutes |
| "Rate limit reached" | Reduce `check_interval_minutes` or increase `max_alerts_per_hour` |
| No listings found | Try a broader search query or remove city/state filters |

## Cost

- **SeatGeek API**: Free
- **StubHub/Vivid Seats scraping**: Free
- **Twilio SMS**: ~$0.0079/message (free trial has $15 credit = ~1900 messages)
- **Server/hosting**: Free if running on your own machine; ~$5/month on a VPS

## License

MIT - Use it, modify it, save money on tickets.

#!/usr/bin/env python3
"""
SPY drop watcher — designed to run every 5 minutes as a GitHub Actions
scheduled workflow (Claude's own scheduler has a 1-hour minimum interval,
which can't catch a fast 10/30-minute move, so this runs on GitHub instead).

Each run:
  1. Skips entirely (no API calls) unless it's currently a weekday between
     9:30am and 4:00pm US/Eastern.
  2. Fetches SPY's current quote from Finnhub.
  3. Appends it to history.json (committed back to the repo each run so
     history survives between runs — GitHub Actions runners are ephemeral).
  4. Checks the rolling history for:
       - a drop of >= $0.75 within the trailing 10 minutes
       - a drop of >= $1.00 within the trailing 30 minutes
     ("drop" = highest recorded price in that window minus the current price)
  5. If either rule fires, emails the alert via Resend.

Required environment variables (set as GitHub Actions secrets):
  FINNHUB_API_KEY   - free key from finnhub.io
  RESEND_API_KEY    - free key from resend.com

Optional:
  ALERT_EMAIL_TO    - defaults to ericp@dcicon.ca
  ALERT_EMAIL_FROM  - defaults to onboarding@resend.dev (Resend's shared
                      sandbox sender — works with no domain setup, as long
                      as ALERT_EMAIL_TO is the same address you signed up
                      to Resend with)
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("ERROR zoneinfo unavailable")
    sys.exit(1)

EASTERN = ZoneInfo("America/New_York")
STATE_FILE = Path(__file__).parent / "history.json"
SYMBOL = "SPY"

MARKET_OPEN = (9, 30)
MARKET_CLOSE = (16, 0)

RULE_10MIN_MINUTES = 10
RULE_10MIN_DROP = 0.75
RULE_30MIN_MINUTES = 30
RULE_30MIN_DROP = 1.00

# Keep a little slack past the longest window so we always have enough
# history to evaluate the 30-minute rule.
PRUNE_AFTER_MINUTES = 40

ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "ericp@dcicon.ca")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "onboarding@resend.dev")


def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def is_market_open(dt: datetime) -> bool:
    if dt.weekday() > 4:  # Sat/Sun
        return False
    open_t = dt.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_t = dt.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_t <= dt <= close_t


def fetch_price() -> float:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY is not set")
    resp = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": SYMBOL, "token": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    price = data.get("c")
    if not price:
        raise RuntimeError(f"Unexpected Finnhub response: {data}")
    return float(price)


def load_history():
    if not STATE_FILE.exists():
        return []
    try:
        raw = json.loads(STATE_FILE.read_text())
        return [{"ts": datetime.fromisoformat(e["ts"]), "price": float(e["price"])} for e in raw]
    except Exception:
        return []


def save_history(history):
    payload = [{"ts": h["ts"].isoformat(), "price": h["price"]} for h in history]
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def high_in_window(history, now, minutes):
    cutoff = now - timedelta(minutes=minutes)
    window = [h for h in history if h["ts"] >= cutoff]
    if not window:
        return None, None
    best = max(window, key=lambda h: h["price"])
    return best["price"], best["ts"]


def send_alert_email(subject: str, body: str):
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("WARNING RESEND_API_KEY not set — cannot send email. Would have sent:")
        print(subject)
        print(body)
        return
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": ALERT_EMAIL_FROM,
            "to": [ALERT_EMAIL_TO],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"ERROR sending email: {resp.status_code} {resp.text}")
    else:
        print(f"Alert email sent to

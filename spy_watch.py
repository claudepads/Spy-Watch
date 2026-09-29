#!/usr/bin/env python3
"""
SPY drop watcher — designed to run every 5 minutes as a GitHub Actions
scheduled workflow (Claude's own scheduler has a 1-hour minimum interval,
which can't catch a fast few-minute move, so this runs on GitHub instead).

GitHub Actions' schedule itself can't fire more often than every 5 minutes,
so to get finer resolution than that, each 5-minute run POLLS the price
several times internally (once a minute, for a few minutes) before exiting,
rather than taking just one snapshot. Combined with history persisted
across runs (committed back to the repo each time — GitHub Actions runners
are otherwise ephemeral), this gives roughly 1-minute price resolution,
which is what makes short rolling windows like "4 minutes" meaningful.

Each run:
  1. Skips entirely (no API calls) unless it's currently a weekday between
     9:30am and 4:00pm US/Eastern.
  2. Otherwise, polls SPY's current quote from Finnhub every POLL_INTERVAL_SECONDS
     for POLL_ITERATIONS samples (a few minutes total, comfortably inside the
     5-minute gap before the next scheduled run).
  3. After each sample, checks the rolling history for:
       - a drop of >= RULE_FAST_DROP within the trailing RULE_FAST_MINUTES
       - a drop of >= RULE_SLOW_DROP within the trailing RULE_SLOW_MINUTES
     ("drop" = highest recorded price in that window minus the current price)
     and emails the alert via Resend the moment either one fires, then stops
     polling for this run (the next scheduled run resumes monitoring).

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
import time
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

RULE_FAST_MINUTES = 4
RULE_FAST_DROP = 0.75
RULE_SLOW_MINUTES = 9
RULE_SLOW_DROP = 1.00

# How this run polls within its 5-minute slot before the next scheduled run.
POLL_INTERVAL_SECONDS = 60
POLL_ITERATIONS = 4  # samples at t=0, 60, 120, 180s -> ~3 minutes, leaving buffer

# Keep a little slack past the longest window so we always have enough
# history to evaluate the slower rule even right after a run starts.
PRUNE_AFTER_MINUTES = 20

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
        print(f"Alert email sent to {ALERT_EMAIL_TO}")


def check_once(history):
    """Fetch one price sample, update history, evaluate both rules.

    Returns True if an alert fired (caller should stop polling), False otherwise.
    """
    now = now_eastern()
    try:
        price = fetch_price()
    except Exception as exc:
        print(f"ERROR fetching price: {exc}")
        return False

    history.append({"ts": now, "price": price})
    cutoff = now - timedelta(minutes=PRUNE_AFTER_MINUTES)
    history[:] = [h for h in history if h["ts"] >= cutoff]
    save_history(history)

    high_fast, high_fast_ts = high_in_window(history, now, RULE_FAST_MINUTES)
    high_slow, high_slow_ts = high_in_window(history, now, RULE_SLOW_MINUTES)

    if high_fast is not None:
        drop_fast = high_fast - price
        if drop_fast >= RULE_FAST_DROP:
            print(f"ALERT_FAST current={price:.2f} high={high_fast:.2f} drop={drop_fast:.2f}")
            send_alert_email(
                f"\U0001F53B SPY dropped ${drop_fast:.2f} in ~{RULE_FAST_MINUTES} minutes",
                f"SPY dropped ${drop_fast:.2f} in the last ~{RULE_FAST_MINUTES} minutes: "
                f"from ${high_fast:.2f} at {high_fast_ts.strftime('%I:%M:%S %p %Z')} "
                f"down to ${price:.2f} just now ({now.strftime('%I:%M:%S %p %Z')}).\n\n"
                f"This breaches your ${RULE_FAST_DROP:.2f}-in-{RULE_FAST_MINUTES}-minutes threshold.",
            )
            return True

    if high_slow is not None:
        drop_slow = high_slow - price
        if drop_slow >= RULE_SLOW_DROP:
            print(f"ALERT_SLOW current={price:.2f} high={high_slow:.2f} drop={drop_slow:.2f}")
            send_alert_email(
                f"\U0001F53B SPY dropped ${drop_slow:.2f} in ~{RULE_SLOW_MINUTES} minutes",
                f"SPY dropped ${drop_slow:.2f} in the last ~{RULE_SLOW_MINUTES} minutes: "
                f"from ${high_slow:.2f} at {high_slow_ts.strftime('%I:%M:%S %p %Z')} "
                f"down to ${price:.2f} just now ({now.strftime('%I:%M:%S %p %Z')}).\n\n"
                f"This breaches your ${RULE_SLOW_DROP:.2f}-in-{RULE_SLOW_MINUTES}-minutes threshold.",
            )
            return True

    print(f"OK current={price:.2f} highFast={high_fast} highSlow={high_slow} — no threshold breached.")
    return False


def main():
    now = now_eastern()
    if not is_market_open(now):
        print(f"CLOSED {now.isoformat()} — outside trading hours, skipping.")
        return

    history = load_history()
    for i in range(POLL_ITERATIONS):
        alerted = check_once(history)
        if alerted:
            break
        if i < POLL_ITERATIONS - 1:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

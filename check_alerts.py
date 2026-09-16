"""
Daily-drop stock alert checker.

Fetches the current quote for each ticker via Finnhub's free API, checks
the day's percent change against a threshold, and sends a push
notification via ntfy.io the first time a ticker crosses that threshold
each day (won't spam you every run once it's already fired).

Environment variables required:
  FINNHUB_API_KEY  - free key from https://finnhub.io/register
  NTFY_TOPIC       - a topic name you chose at https://ntfy.sh (any
                     unique string works, e.g. "fazal-stock-drops-8f2k")

Optional:
  DROP_THRESHOLD   - percent drop to trigger on (default: -1.0)
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
THRESHOLD = float(os.environ.get("DROP_THRESHOLD", "-1.0"))

# All tickers from the Robinhood margin account statement.
# (Opendoor warrants OPENL/OPENW/OPENZ are excluded - they're not
# standard equities and most free quote APIs don't cover them.)
TICKERS = [
    "AAPL", "ADBE", "AMD", "AMZN", "ANET", "ARKK", "ASTS", "AVAV", "AVGO",
    "BB", "CEG", "CLYM", "CMC", "CRWD", "DAL", "DDOG", "GEV", "GLD", "GOOG",
    "GOOGL", "INTC", "IOT", "IRDM", "LHX", "LLY", "META", "MH", "MP",
    "MRVL", "MSFT", "MU", "NEM", "NET", "NKE", "NOC", "NOK", "NOW", "NTAP",
    "NVDA", "ORCL", "PANW", "PATH", "QCOM", "QQQ", "RGTI", "RKLB", "RTX",
    "RYCEY", "SMCI", "SMH", "SNDK", "SPMO", "SPY", "SPYG", "SYM", "TEM",
    "UBER", "VOO", "VST", "VVX", "ZS",
]

STATE_FILE = "alert_state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_quote(symbol):
    url = "https://finnhub.io/api/v1/quote"
    r = requests.get(
        url, params={"symbol": symbol, "token": FINNHUB_API_KEY}, timeout=10
    )
    r.raise_for_status()
    return r.json()


def send_push(title, message):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "chart_with_downwards_trend",
        },
        timeout=10,
    )


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state()
    if state.get("date") != today:
        state = {"date": today, "alerted": []}

    triggered = []

    for symbol in TICKERS:
        try:
            quote = get_quote(symbol)
            pct_change = quote.get("dp")  # Finnhub's daily % change
            current = quote.get("c")
            if pct_change is None or not current:
                print(f"{symbol}: no data, skipping")
                continue

            if pct_change <= THRESHOLD and symbol not in state["alerted"]:
                send_push(
                    f"{symbol} down {pct_change:.2f}%",
                    f"{symbol} is at ${current:.2f}, down {pct_change:.2f}% today.",
                )
                state["alerted"].append(symbol)
                triggered.append(symbol)
                print(f"{symbol}: ALERT SENT ({pct_change:.2f}%)")
            else:
                print(f"{symbol}: {pct_change:.2f}%")

        except Exception as e:
            print(f"{symbol}: error - {e}")

        time.sleep(1)  # stay well under Finnhub's free rate limit

    save_state(state)

    if triggered:
        print(f"\nAlerts sent for: {', '.join(triggered)}")
    else:
        print("\nNo new alerts this run.")


if __name__ == "__main__":
    main()

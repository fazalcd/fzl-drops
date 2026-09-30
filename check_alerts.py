"""
Daily-drop stock alert checker (v3).

Adds to v2:
  - A SEPARATE, distinctly-tagged notification when a ticker's current
    price falls below its own trailing 30-trading-day low (based on daily
    closes). This is independent of the escalating 1%-tier alerts - a
    stock can fire both, one, or neither on a given day.
  - History is fetched fresh each run via Finnhub's /stock/candle
    endpoint (no need to accumulate our own daily log - Finnhub already
    has the history, we just ask for it).

Environment variables required:
  FINNHUB_API_KEY  - free key from https://finnhub.io/register
  NTFY_TOPIC       - your ntfy.sh topic name

Optional:
  TICKERS_FILE     - path to the ticker list (default: tickers.txt)
"""

import json
import math
import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
TICKERS_FILE = os.environ.get("TICKERS_FILE", "tickers.txt")
LOOKBACK_DAYS = 30

STATE_FILE = "alert_state.json"


def load_tickers():
    with open(TICKERS_FILE) as f:
        lines = [line.strip() for line in f]
    return [line.upper() for line in lines if line and not line.startswith("#")]


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


def get_trailing_low(symbol, days=LOOKBACK_DAYS):
    """Returns the lowest daily CLOSE over the trailing `days` trading
    days, not counting today. Returns None if there isn't enough history
    yet (e.g. a newly-listed ticker) or the request fails."""
    now = datetime.now(timezone.utc)
    # ask for extra calendar days to comfortably cover `days` trading days
    frm = now - timedelta(days=days * 2 + 10)
    url = "https://finnhub.io/api/v1/stock/candle"
    r = requests.get(
        url,
        params={
            "symbol": symbol,
            "resolution": "D",
            "from": int(frm.timestamp()),
            "to": int(now.timestamp()),
            "token": FINNHUB_API_KEY,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("s") != "ok" or not data.get("c"):
        return None

    closes = data["c"]
    timestamps = data["t"]

    today_str_ = now.strftime("%Y-%m-%d")
    trimmed = [
        c for c, t in zip(closes, timestamps)
        if datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d") != today_str_
    ]

    if len(trimmed) < days:
        return None  # not enough history yet

    return min(trimmed[-days:])


def send_push(title, message, priority="urgent", tags="chart_with_downwards_trend"):
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        timeout=10,
    )
    r.raise_for_status()


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main():
    today = today_str()
    state = load_state()
    if state.get("date") != today:
        state = {
            "date": today,
            "tiers": {},
            "thirty_day_low_alerted": [],
            "error_notified": False,
        }
    state.setdefault("tiers", {})
    state.setdefault("thirty_day_low_alerted", [])
    state.setdefault("error_notified", False)

    tickers = load_tickers()
    errors = []

    for symbol in tickers:
        # --- daily % change / escalating tier check ---
        try:
            quote = get_quote(symbol)
            pct_change = quote.get("dp")
            current = quote.get("c")

            if pct_change is None or not current:
                errors.append((symbol, "no quote data returned from Finnhub"))
                print(f"{symbol}: no quote data, skipping tier check")
            else:
                current_tier = math.floor(abs(pct_change)) if pct_change <= -1 else 0
                prev_tier = state["tiers"].get(symbol, 0)

                if current_tier > prev_tier:
                    try:
                        send_push(
                            f"{symbol} down {current_tier}%+ ({pct_change:.2f}%)",
                            f"{symbol} is at ${current:.2f}, down {pct_change:.2f}% today "
                            f"- crossed the {current_tier}% mark.",
                        )
                        state["tiers"][symbol] = current_tier
                        print(f"{symbol}: TIER ALERT SENT ({prev_tier} -> {current_tier}, {pct_change:.2f}%)")
                    except Exception as e:
                        errors.append((symbol, f"failed to send tier push: {e}"))
                        print(f"{symbol}: tier alert FAILED to send - {e}")
                else:
                    print(f"{symbol}: {pct_change:.2f}% (tier {current_tier})")
        except Exception as e:
            errors.append((symbol, f"quote fetch error: {e}"))
            print(f"{symbol}: quote error - {e}")
            current = None  # so the 30-day check below can still be attempted independently

        time.sleep(1)  # pace requests under Finnhub's free rate limit

        # --- 30-day-low check (independent of the tier check above) ---
        try:
            trailing_low = get_trailing_low(symbol)
            if trailing_low is None:
                print(f"{symbol}: not enough history yet for 30-day-low check")
            elif current is not None:
                already_alerted = symbol in state["thirty_day_low_alerted"]
                if current < trailing_low and not already_alerted:
                    try:
                        send_push(
                            f"\U0001F53B {symbol} hit a new 30-day low",
                            f"{symbol} is at ${current:.2f}, below its 30-day low of ${trailing_low:.2f}.",
                            priority="urgent",
                            tags="rotating_light",
                        )
                        state["thirty_day_low_alerted"].append(symbol)
                        print(f"{symbol}: 30-DAY-LOW ALERT SENT (${current:.2f} < ${trailing_low:.2f})")
                    except Exception as e:
                        errors.append((symbol, f"failed to send 30-day-low push: {e}"))
                        print(f"{symbol}: 30-day-low alert FAILED to send - {e}")
                elif current < trailing_low:
                    print(f"{symbol}: still below 30-day low (${current:.2f} < ${trailing_low:.2f}), already alerted today")
                else:
                    print(f"{symbol}: above 30-day low (${current:.2f} vs ${trailing_low:.2f})")
        except Exception as e:
            errors.append((symbol, f"30-day-low fetch error: {e}"))
            print(f"{symbol}: 30-day-low check error - {e}")

        time.sleep(1)  # pace requests for the candle call too

    save_state(state)

    if errors and not state["error_notified"]:
        lines = [f"- {sym}: {msg}" for sym, msg in errors[:10]]
        extra = f"\n(+{len(errors) - 10} more)" if len(errors) > 10 else ""
        try:
            send_push(
                f"Stock alert script: {len(errors)} issue(s) today",
                "\n".join(lines) + extra,
                priority="high",
                tags="warning",
            )
            state["error_notified"] = True
            save_state(state)
            print(f"\nSent error summary notification ({len(errors)} issues).")
        except Exception as e:
            print(f"\nCould not send error notification: {e}")
    elif errors:
        print(f"\n{len(errors)} issue(s) occurred, but an error notification was already sent today.")
    else:
        print("\nNo errors this run.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            today = today_str()
            state = load_state()
            if state.get("date") != today:
                state = {
                    "date": today,
                    "tiers": {},
                    "thirty_day_low_alerted": [],
                    "error_notified": False,
                }
            if not state.get("error_notified"):
                send_push(
                    "Stock alert script crashed",
                    f"The script failed to run:\n{tb[-500:]}",
                    priority="high",
                    tags="rotating_light",
                )
                state["error_notified"] = True
                save_state(state)
        except Exception:
            pass
        raise

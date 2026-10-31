"""
Stock alert checker (v4).

Each run, for every ticker in tickers.txt:
  1. Escalating drop alerts: push at -1% today, again at -2%, -3%, ...
     (each deeper 1% tier notifies once per day).
  2. 30-day high/low alerts: today's intraday low/high is compared with the
     lows/highs recorded in history.csv for the previous (up to) 30 trading
     days. Breaking below the low or above the high sends a push.
  3. Repeat-break alert: if the same ticker already broke its 30-day low
     (or high) earlier in the last 30 trading days, the push is upgraded to
     a special "again" alert.
  4. Problems (bad ticker, API or push failures, crashes) send ONE summary
     push per day, however many times they recur.

Data files (the workflow commits these back to the repo after each run):
  history.csv       date,symbol,low,high,close    one row per ticker per trading day
  breaks.csv        date,symbol,type,price,level  every 30-day low/high break alerted
  alert_state.json  today's escalating-tier and error-alert bookkeeping

Environment variables required:
  FINNHUB_API_KEY, NTFY_TOPIC

Optional:
  TICKERS_FILE      default tickers.txt
  DATA_DIR          default . (where the data files live)
  LOOKBACK_DAYS     default 30
  MIN_HISTORY_DAYS  default 5 (no high/low alerts until this many previous
                    trading days are recorded for a ticker)
"""

import csv
import json
import math
import os
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
TICKERS_FILE = os.environ.get("TICKERS_FILE", "tickers.txt")
DATA_DIR = os.environ.get("DATA_DIR", ".")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
MIN_HISTORY_DAYS = int(os.environ.get("MIN_HISTORY_DAYS", "5"))
KEEP_DATES = LOOKBACK_DAYS + 10  # history rows kept (distinct trading dates)

STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.csv")
BREAKS_FILE = os.path.join(DATA_DIR, "breaks.csv")

ET = ZoneInfo("America/New_York")


# ---------- small helpers ----------

def now_et():
    return datetime.now(ET)


def load_tickers():
    with open(TICKERS_FILE) as f:
        lines = [line.strip().upper() for line in f]
    seen, out = set(), []
    for line in lines:
        if line and not line.startswith("#") and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def trade_date(quote):
    """Trading date (ET) of the quote's last trade, or None."""
    t = quote.get("t")
    if not t:
        return None
    return datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")


# ---------- state / history / breaks files ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_history():
    """{symbol: {date: {"low":..., "high":..., "close":...}}}"""
    hist = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, newline="") as f:
            for row in csv.DictReader(f):
                hist.setdefault(row["symbol"], {})[row["date"]] = {
                    "low": float(row["low"]),
                    "high": float(row["high"]),
                    "close": float(row["close"]),
                }
    return hist


def save_history(hist):
    """Writes history.csv (pruned to the most recent KEEP_DATES trading
    dates) and returns the set of dates kept."""
    all_dates = sorted({d for days in hist.values() for d in days})
    keep = set(all_dates[-KEEP_DATES:])
    with open(HISTORY_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "low", "high", "close"])
        for d in sorted(keep):
            for sym in sorted(hist):
                r = hist[sym].get(d)
                if r:
                    w.writerow([d, sym, round(r["low"], 4), round(r["high"], 4), round(r["close"], 4)])
    return keep


def load_breaks():
    if os.path.exists(BREAKS_FILE):
        with open(BREAKS_FILE, newline="") as f:
            return list(csv.DictReader(f))
    return []


def save_breaks(breaks, keep_dates):
    min_date = min(keep_dates) if keep_dates else ""
    with open(BREAKS_FILE, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "type", "price", "level"])
        for b in breaks:
            if b["date"] >= min_date:
                w.writerow([b["date"], b["symbol"], b["type"], b["price"], b["level"]])


# ---------- network ----------

def get_quote(symbol):
    r = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": symbol, "token": FINNHUB_API_KEY},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}


def send_push(title, message, priority="urgent", tags="chart_with_downwards_trend"):
    # JSON publish (not headers): HTTP headers are Latin-1 only, so emoji in
    # a title sent as a header raises an encoding error.
    r = requests.post(
        "https://ntfy.sh/",
        json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": PRIORITIES.get(priority, 3),
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
        },
        timeout=10,
    )
    r.raise_for_status()


# ---------- per-ticker logic ----------

def process_symbol(symbol, today, state, history, breaks, errors):
    quote = get_quote(symbol)
    pct, price = quote.get("dp"), quote.get("c")
    day_low, day_high = quote.get("l"), quote.get("h")

    if pct is None or not price or not day_low or not day_high:
        errors.append((symbol, "no/incomplete quote data (check the ticker symbol)"))
        print(f"{symbol}: incomplete quote data, skipping")
        return

    qdate = trade_date(quote)
    if qdate != today:
        # Market closed (weekend/holiday) or hasn't traded yet today.
        print(f"{symbol}: no trades yet today (last quote {qdate}), skipping")
        return

    # 1) escalating % drop tiers
    tier = math.floor(abs(pct)) if pct <= -1 else 0
    prev_tier = state["tiers"].get(symbol, 0)
    if tier > prev_tier:
        try:
            send_push(
                f"{symbol} down {tier}%+ ({pct:.2f}%)",
                f"{symbol} is at ${price:.2f}, down {abs(pct):.2f}% today - crossed the {tier}% mark.",
            )
            state["tiers"][symbol] = tier
            print(f"{symbol}: TIER ALERT ({prev_tier} -> {tier}, {pct:.2f}%)")
        except Exception as e:
            errors.append((symbol, f"failed to send tier push: {e}"))
    else:
        print(f"{symbol}: {pct:.2f}% (tier {tier})")

    # 2) record today's range, compare against the previous days only
    days = history.setdefault(symbol, {})
    prev_dates = sorted(d for d in days if d < today)[-LOOKBACK_DAYS:]
    row = days.get(today)
    new_low = min(day_low, row["low"]) if row else day_low
    new_high = max(day_high, row["high"]) if row else day_high
    days[today] = {"low": new_low, "high": new_high, "close": price}

    if len(prev_dates) < MIN_HISTORY_DAYS:
        print(f"{symbol}: building history ({len(prev_dates)}/{MIN_HISTORY_DAYS} days), no high/low check yet")
        return

    n = len(prev_dates)
    prev_low = min(days[d]["low"] for d in prev_dates)
    prev_high = max(days[d]["high"] for d in prev_dates)
    prev_set = set(prev_dates)

    checks = (
        ("low", new_low < prev_low, new_low, prev_low),
        ("high", new_high > prev_high, new_high, prev_high),
    )
    for kind, broke, extreme, level in checks:
        if not broke:
            continue
        if any(b["date"] == today and b["symbol"] == symbol and b["type"] == kind for b in breaks):
            continue  # already alerted today

        earlier = sorted(
            b["date"] for b in breaks
            if b["symbol"] == symbol and b["type"] == kind and b["date"] in prev_set
        )
        count = len(earlier) + 1
        arrow = "\U0001F53B" if kind == "low" else "\U0001F53A"
        trend = "chart_with_downwards_trend" if kind == "low" else "chart_with_upwards_trend"
        where = "under" if kind == "low" else "over"

        if count >= 2:
            title = f"\U0001F6A8 {symbol}: {ordinal(count)} {n}-day {kind} break"
            body = (
                f"{symbol} hit a new {n}-day {kind} of ${extreme:.2f} "
                f"(previous {kind}: ${level:.2f}), now ${price:.2f}.\n"
                f"This is break #{count} in the last {n} trading days. Earlier: {', '.join(earlier)}."
            )
            priority, tags = "urgent", f"rotating_light,{trend}"
        else:
            title = f"{arrow} {symbol} new {n}-day {kind}"
            body = (
                f"{symbol} traded {where} its {n}-day {kind} of ${level:.2f}, "
                f"reaching ${extreme:.2f} (now ${price:.2f}, {pct:+.2f}% today)."
            )
            priority, tags = "high", trend

        try:
            send_push(title, body, priority=priority, tags=tags)
            breaks.append({
                "date": today, "symbol": symbol, "type": kind,
                "price": f"{extreme:.2f}", "level": f"{level:.2f}",
            })
            print(f"{symbol}: {n}-DAY {kind.upper()} ALERT (break #{count})")
        except Exception as e:
            errors.append((symbol, f"failed to send {kind} break push: {e}"))


# ---------- main ----------

def main():
    today = now_et().strftime("%Y-%m-%d")
    state = load_state()
    if state.get("date") != today:
        state = {"date": today, "tiers": {}, "error_notified": False}
    state.setdefault("tiers", {})
    state.setdefault("error_notified", False)

    tickers = load_tickers()
    history = load_history()
    breaks = load_breaks()
    errors = []

    for symbol in tickers:
        try:
            process_symbol(symbol, today, state, history, breaks, errors)
        except Exception as e:
            errors.append((symbol, f"check failed: {e}"))
            print(f"{symbol}: error - {e}")
        time.sleep(1)  # stay under Finnhub's free 60 calls/minute

    keep_dates = save_history(history)
    save_breaks(breaks, keep_dates)
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
        print(f"\n{len(errors)} issue(s) occurred, error notification already sent today.")
    else:
        print("\nNo errors this run.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            today = now_et().strftime("%Y-%m-%d")
            state = load_state()
            if state.get("date") != today:
                state = {"date": today, "tiers": {}, "error_notified": False}
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

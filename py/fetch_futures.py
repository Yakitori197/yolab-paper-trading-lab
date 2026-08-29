"""Fetch USDS-M perpetual futures data (klines + funding rate history) via
ccxt binanceusdm into SQLite. Batch #3 (hypothesis E, funding rate) data
pipeline.

Klines reuse fetch_klines.fetch_series (exchange-agnostic: it only calls
ex.fetch_ohlcv). Funding rate history is paginated separately here since it
is not an OHLCV feed. Open interest is NOT fetched by this module for
storage -- `--step oi-test` only probes how far back the endpoint actually
returns data (Binance's OI history endpoint is widely reported to cover only
the most recent ~30 days); per the batch #3 pre-registration, OI is dropped
from this batch unless that probe shows otherwise.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from fetch_klines import date_ms, fetch_series, iso, now_ms

EXCHANGE_ID = "binanceusdm"

# perpetual klines: 4h/1d, warmup start before the 2021-01-01 IS start
PLAN_KLINES = [
    ("BTC/USDT:USDT", "4h", "2020-10-01"),
    ("BTC/USDT:USDT", "1d", "2020-10-01"),
    ("ETH/USDT:USDT", "4h", "2020-10-01"),
    ("ETH/USDT:USDT", "1d", "2020-10-01"),
    ("SOL/USDT:USDT", "4h", "2020-10-01"),
    ("SOL/USDT:USDT", "1d", "2020-10-01"),
]

# funding rate history: same three symbols, same warmup start
FUNDING_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
FUNDING_START = "2020-10-01"


def _fetch_funding_forward(con, ex, exchange_id, symbol, start_ms, end_ms):
    """Forward-paginate ccxt.fetch_funding_rate_history from start_ms through
    end_ms inclusive. ts is the funding settlement time (ms)."""
    cursor = start_ms
    added = 0
    while cursor <= end_ms:
        batch = ex.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
        if not batch:
            break
        rows = [(b["timestamp"], b["fundingRate"]) for b in batch
                if b["timestamp"] is not None and cursor <= b["timestamp"] <= end_ms]
        if rows:
            added += db.upsert_funding(con, exchange_id, symbol, rows)
        last_ts = batch[-1]["timestamp"]
        new_cursor = last_ts + 1
        if new_cursor <= cursor:
            break
        cursor = new_cursor
    return added


def fetch_funding_series(con, ex, exchange_id, symbol, start_ms):
    n0, tmin, tmax = db.funding_stats(con, exchange_id, symbol)
    end_ms = now_ms()
    added = 0
    if n0 and start_ms < tmin:
        added += _fetch_funding_forward(con, ex, exchange_id, symbol, start_ms, tmin - 1)
    cursor_start = max(start_ms, tmax + 1) if tmax else start_ms
    added += _fetch_funding_forward(con, ex, exchange_id, symbol, cursor_start, end_ms)
    n, tmin, tmax = db.funding_stats(con, exchange_id, symbol)
    if n:
        print(f"{exchange_id:11s} {symbol:15s} funding added={added:6d} total={n:6d} "
              f"first={iso(tmin)} last={iso(tmax)}")
    else:
        print(f"{exchange_id:11s} {symbol:15s} funding added=0 total=0  [NO DATA RETURNED]")
    return added


def oi_test(ex, symbol, since_ms):
    """One-shot probe: how far back does fetch_open_interest_history actually
    reach? Report the raw result, do not attempt to work around any limit."""
    rows = ex.fetch_open_interest_history(symbol, timeframe="4h", since=since_ms, limit=500)
    if not rows:
        print(f"OI TEST {symbol}: 0 rows returned for since={iso(since_ms)}")
        return rows
    first_ts = rows[0]["timestamp"]
    last_ts = rows[-1]["timestamp"]
    print(f"OI TEST {symbol}: {len(rows)} rows, requested since={iso(since_ms)}, "
          f"actual first={iso(first_ts)}  last={iso(last_ts)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["klines", "funding", "oi-test"], required=True)
    args = ap.parse_args()
    import ccxt  # lazy import: tests do not need ccxt
    con = db.connect()
    ex = ccxt.binanceusdm({"enableRateLimit": True})

    if args.step == "klines":
        total = 0
        for symbol, timeframe, start in PLAN_KLINES:
            total += fetch_series(con, ex, EXCHANGE_ID, symbol, timeframe, date_ms(start))
        print(f"DONE: {total} kline bars added")
    elif args.step == "funding":
        total = 0
        for symbol in FUNDING_SYMBOLS:
            total += fetch_funding_series(con, ex, EXCHANGE_ID, symbol, date_ms(FUNDING_START))
        print(f"DONE: {total} funding rows added")
    elif args.step == "oi-test":
        oi_test(ex, "BTC/USDT:USDT", date_ms("2021-01-01"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

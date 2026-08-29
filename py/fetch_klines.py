"""Fetch OHLCV from exchanges via ccxt into SQLite. Resumable; stores closed bars only."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

TIMEFRAME_MS = {"4h": 4 * 3600 * 1000, "1d": 24 * 3600 * 1000}

# (exchange, symbol, timeframe, start date UTC)
# bitstamp ETH/USD  : calibration source (same venue as TradingView BITSTAMP data)
# binance  */USDT   : production research data; start 2020-12-01 gives >100 bars
#                     of indicator warmup before the 2021-01-01 full-window IS start
PLAN_M1 = [
    ("bitstamp", "ETH/USD",  "4h", "2023-10-01"),
    ("binance",  "BTC/USDT", "4h", "2020-12-01"),
    ("binance",  "ETH/USDT", "4h", "2020-12-01"),
    ("binance",  "SOL/USDT", "4h", "2020-12-01"),
]

# M6: backfill earlier history so the 2021-01-01 IS start gets a much deeper
# indicator warmup (M1's 2020-12-01 start only gave ~186 pre-window bars).
# SOL/USDT listed on binance ~2020-08; if 2020-08-01 predates the exchange's
# actual earliest bar, ccxt/binance clamps to whatever really exists there,
# which is reported as-is by fetch_series (no special-casing needed).
PLAN_M6 = [
    ("binance",  "BTC/USDT", "4h", "2019-01-01"),
    ("binance",  "ETH/USDT", "4h", "2019-01-01"),
    ("binance",  "SOL/USDT", "4h", "2020-08-01"),
]


def date_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def now_ms():
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _fetch_forward(con, ex, exchange_id, symbol, timeframe, start_ms, end_ms):
    """Forward-paginate ccxt.fetch_ohlcv from start_ms through end_ms inclusive."""
    step = TIMEFRAME_MS[timeframe]
    cursor = start_ms
    added = 0
    while cursor <= end_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows = [r for r in batch if cursor <= r[0] <= end_ms]
        if rows:
            added += db.upsert_klines(con, exchange_id, symbol, timeframe, rows)
        new_cursor = batch[-1][0] + step
        if new_cursor <= cursor:
            break
        cursor = new_cursor
    return added


def fetch_series(con, ex, exchange_id, symbol, timeframe, start_ms):
    step = TIMEFRAME_MS[timeframe]
    n0, tmin, tmax = db.series_stats(con, exchange_id, symbol, timeframe)
    last_closed = (now_ms() // step) * step - step  # open time of last fully closed bar
    added = 0
    if n0 and start_ms < tmin:
        # backfill the gap before the earliest bar already stored (e.g. a
        # later plan asking for deeper history than a prior run fetched)
        added += _fetch_forward(con, ex, exchange_id, symbol, timeframe, start_ms, tmin - step)
    cursor_start = max(start_ms, tmax + step) if tmax else start_ms
    added += _fetch_forward(con, ex, exchange_id, symbol, timeframe, cursor_start, last_closed)
    n, tmin, tmax = db.series_stats(con, exchange_id, symbol, timeframe)
    if n:
        print(f"{exchange_id:9s} {symbol:9s} {timeframe:3s} added={added:6d} total={n:6d} "
              f"first={iso(tmin)} last={iso(tmax)}")
    else:
        print(f"{exchange_id:9s} {symbol:9s} {timeframe:3s} added=0 total=0  [NO DATA RETURNED]")
    return added


PLANS = {"m1": PLAN_M1, "m6": PLAN_M6}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", choices=list(PLANS), required=True)
    args = ap.parse_args()
    plan = PLANS[args.plan]
    import ccxt  # lazy import: tests do not need ccxt
    con = db.connect()
    exchanges = {}
    total = 0
    for exchange_id, symbol, timeframe, start in plan:
        if exchange_id not in exchanges:
            exchanges[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        total += fetch_series(con, exchanges[exchange_id], exchange_id, symbol, timeframe,
                              date_ms(start))
    print(f"DONE: {total} bars added")
    return 0


if __name__ == "__main__":
    sys.exit(main())

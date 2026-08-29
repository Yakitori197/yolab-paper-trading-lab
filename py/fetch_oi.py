"""E14 rolling collector: open-interest history + taker buy/sell volume
ratio from Binance USDS-M `/futures/data/*` endpoints into SQLite (market.db
tables `oi` / `taker_ratio`).

Why this exists: those endpoints only retain ~30 days (verified for OI by
batch #3's --step oi-test probe), so history is NOT backfillable -- data not
collected now is gone. This module therefore only ever extends forward from
what is already stored (resumable, INSERT OR REPLACE idempotent), clamped to
the endpoint's retention window. No hypothesis is attached to this data;
using it for research requires its own pre-registered batch (BATCH_PLAN E14
note).

Endpoint facts this code relies on:
- period "4h", limit <= 500 per call; rows carry period-aligned `timestamp`
- requesting startTime older than retention just returns what is retained
  (no error), so the resume cursor is simply max(stored ts)+1 clamped to
  now - RETENTION_DAYS
- taker ratio has no unified ccxt method -> implicit
  fapiDataGetTakerlongshortRatio with the raw market id
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
from fetch_klines import date_ms, iso, now_ms

EXCHANGE_ID = "binanceusdm"
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
PERIOD = "4h"
STEP_MS = 4 * 3600 * 1000
RETENTION_DAYS = 29          # endpoint keeps ~30 days; stay inside it
LIMIT = 500


def resume_start(stats_row, now):
    """Pure: given (count, min_ts, max_ts) and now(ms), return the fetch
    cursor -- one step past what is stored, clamped into the endpoint's
    retention window. Standalone so the clamp logic is testable without
    network."""
    _n, _tmin, tmax = stats_row
    earliest = now - RETENTION_DAYS * 86400000
    if tmax is None:
        return earliest
    return max(tmax + 1, earliest)


def _paginate(fetch_page, start_ms, end_ms):
    """Forward-paginate fetch_page(cursor) -> list of (ts, row) until end_ms.
    Shared by both feeds; stops on empty page or non-advancing cursor."""
    cursor = start_ms
    out = []
    while cursor <= end_ms:
        page = fetch_page(cursor)
        if not page:
            break
        out.extend((ts, row) for ts, row in page if cursor <= ts <= end_ms)
        last_ts = page[-1][0]
        if last_ts + 1 <= cursor:
            break
        cursor = last_ts + 1
    return out


def fetch_oi_series(con, ex, exchange_id, symbol, period=PERIOD):
    """Extend the `oi` table forward to now. Returns rows added."""
    now = now_ms()
    start = resume_start(db.oi_stats(con, exchange_id, symbol), now)

    def page(cursor):
        rows = ex.fetch_open_interest_history(
            symbol, timeframe=period, since=cursor, limit=LIMIT)
        out = []
        for r in rows:
            ts = r.get("timestamp")
            if ts is None:
                continue
            info = r.get("info") or {}
            amt = r.get("openInterestAmount")
            if amt is None:
                amt = info.get("sumOpenInterest")
            val = r.get("openInterestValue")
            if val is None:
                val = info.get("sumOpenInterestValue")
            if amt is None:
                continue
            out.append((int(ts), (int(ts), float(amt), None if val is None else float(val))))
        return out

    got = _paginate(page, start, now)
    added = db.upsert_oi(con, exchange_id, symbol, [row for _ts, row in got]) if got else 0
    n, tmin, tmax = db.oi_stats(con, exchange_id, symbol)
    tag = f"first={iso(tmin)} last={iso(tmax)}" if n else "[NO DATA]"
    print(f"{exchange_id:11s} {symbol:15s} oi          added={added:5d} total={n:6d} {tag}")
    return added


def fetch_taker_series(con, ex, exchange_id, symbol, period=PERIOD):
    """Extend the `taker_ratio` table forward to now. Returns rows added."""
    now = now_ms()
    start = resume_start(db.taker_ratio_stats(con, exchange_id, symbol), now)
    ex.load_markets()
    market_id = ex.market(symbol)["id"]

    def page(cursor):
        rows = ex.fapiDataGetTakerlongshortRatio({
            "symbol": market_id, "period": period,
            "startTime": int(cursor), "limit": LIMIT})
        out = []
        for r in rows:
            ts = r.get("timestamp")
            if ts is None or r.get("buyVol") is None or r.get("sellVol") is None:
                continue
            ratio = r.get("buySellRatio")
            out.append((int(ts), (int(ts), float(r["buyVol"]), float(r["sellVol"]),
                                  None if ratio is None else float(ratio))))
        return out

    got = _paginate(page, start, now)
    added = db.upsert_taker_ratio(con, exchange_id, symbol,
                                  [row for _ts, row in got]) if got else 0
    n, tmin, tmax = db.taker_ratio_stats(con, exchange_id, symbol)
    tag = f"first={iso(tmin)} last={iso(tmax)}" if n else "[NO DATA]"
    print(f"{exchange_id:11s} {symbol:15s} taker_ratio added={added:5d} total={n:6d} {tag}")
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["oi", "taker", "all"], default="all")
    args = ap.parse_args()
    import ccxt  # lazy import: tests do not need ccxt
    con = db.connect()
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    total = 0
    for symbol in SYMBOLS:
        if args.step in ("oi", "all"):
            total += fetch_oi_series(con, ex, EXCHANGE_ID, symbol)
        if args.step in ("taker", "all"):
            total += fetch_taker_series(con, ex, EXCHANGE_ID, symbol)
    print(f"DONE: {total} rows added")
    return 0


if __name__ == "__main__":
    sys.exit(main())

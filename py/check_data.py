"""Data quality report: per-series row counts, bounds, gap detection."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

TIMEFRAME_MS = {"4h": 4 * 3600 * 1000, "1d": 24 * 3600 * 1000, "3d": 3 * 24 * 3600 * 1000}


def find_gaps(ts_list, step_ms):
    """ts_list: bar open times (ms). Returns [(first_missing_ts, missing_count), ...]."""
    ts = sorted(ts_list)
    gaps = []
    for a, b in zip(ts, ts[1:]):
        if b - a > step_ms:
            gaps.append((a + step_ms, (b - a) // step_ms - 1))
    return gaps


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def main():
    con = db.connect()
    series = db.list_series(con)
    if not series:
        print("NO DATA: klines table is empty")
        return 1
    for exchange, symbol, timeframe in series:
        step = TIMEFRAME_MS[timeframe]
        n, tmin, tmax = db.series_stats(con, exchange, symbol, timeframe)
        gaps = find_gaps(db.series_ts(con, exchange, symbol, timeframe), step)
        missing = sum(g[1] for g in gaps)
        expected = (tmax - tmin) // step + 1
        status = "CONTINUITY OK" if not gaps else f"GAPS={len(gaps)} MISSING={missing}"
        if n + missing != expected:
            status += "  [COUNT MISMATCH: unaligned timestamps]"
        print(f"{exchange:9s} {symbol:9s} {timeframe:3s} rows={n:6d} "
              f"first={iso(tmin)} last={iso(tmax)}  {status}")
        for g_ts, g_n in gaps[:10]:
            print(f"    gap: {g_n} bar(s) missing from {iso(g_ts)}")
        if len(gaps) > 10:
            print(f"    ... and {len(gaps) - 10} more gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())

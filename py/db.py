"""SQLite storage for OHLCV klines. Single table, natural primary key."""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    PRIMARY KEY (exchange, symbol, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS funding (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    rate      REAL    NOT NULL,
    PRIMARY KEY (exchange, symbol, ts)
);
CREATE TABLE IF NOT EXISTS oi (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    oi        REAL    NOT NULL,
    oi_value  REAL,
    PRIMARY KEY (exchange, symbol, ts)
);
CREATE TABLE IF NOT EXISTS taker_ratio (
    exchange  TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    buy_vol   REAL    NOT NULL,
    sell_vol  REAL    NOT NULL,
    ratio     REAL,
    PRIMARY KEY (exchange, symbol, ts)
);
"""


def connect(db_path=None):
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def upsert_klines(con, exchange, symbol, timeframe, rows):
    """rows: iterable of [ts, open, high, low, close, volume]; INSERT OR REPLACE (idempotent)."""
    data = [(exchange, symbol, timeframe, int(r[0]),
             float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO klines "
        "(exchange, symbol, timeframe, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?,?)", data)
    con.commit()
    return len(data)


def upsert_funding(con, exchange, symbol, rows):
    """rows: iterable of [ts, rate]; INSERT OR REPLACE (idempotent)."""
    data = [(exchange, symbol, int(r[0]), float(r[1])) for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO funding (exchange, symbol, ts, rate) VALUES (?,?,?,?)", data)
    con.commit()
    return len(data)


def upsert_oi(con, exchange, symbol, rows):
    """rows: iterable of [ts, oi, oi_value_or_None]; INSERT OR REPLACE (idempotent)."""
    data = [(exchange, symbol, int(r[0]), float(r[1]),
             None if r[2] is None else float(r[2])) for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO oi (exchange, symbol, ts, oi, oi_value) VALUES (?,?,?,?,?)", data)
    con.commit()
    return len(data)


def upsert_taker_ratio(con, exchange, symbol, rows):
    """rows: iterable of [ts, buy_vol, sell_vol, ratio_or_None]; INSERT OR REPLACE (idempotent)."""
    data = [(exchange, symbol, int(r[0]), float(r[1]), float(r[2]),
             None if r[3] is None else float(r[3])) for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO taker_ratio (exchange, symbol, ts, buy_vol, sell_vol, ratio) "
        "VALUES (?,?,?,?,?,?)", data)
    con.commit()
    return len(data)


def oi_stats(con, exchange, symbol):
    cur = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM oi WHERE exchange=? AND symbol=?",
        (exchange, symbol))
    return cur.fetchone()


def taker_ratio_stats(con, exchange, symbol):
    cur = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM taker_ratio WHERE exchange=? AND symbol=?",
        (exchange, symbol))
    return cur.fetchone()


def funding_stats(con, exchange, symbol):
    cur = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM funding WHERE exchange=? AND symbol=?",
        (exchange, symbol))
    return cur.fetchone()


def funding_ts(con, exchange, symbol):
    cur = con.execute(
        "SELECT ts FROM funding WHERE exchange=? AND symbol=? ORDER BY ts", (exchange, symbol))
    return [r[0] for r in cur.fetchall()]


def series_stats(con, exchange, symbol, timeframe):
    cur = con.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM klines "
        "WHERE exchange=? AND symbol=? AND timeframe=?",
        (exchange, symbol, timeframe))
    return cur.fetchone()


def series_ts(con, exchange, symbol, timeframe):
    cur = con.execute(
        "SELECT ts FROM klines WHERE exchange=? AND symbol=? AND timeframe=? ORDER BY ts",
        (exchange, symbol, timeframe))
    return [r[0] for r in cur.fetchall()]


def list_series(con):
    cur = con.execute(
        "SELECT DISTINCT exchange, symbol, timeframe FROM klines "
        "ORDER BY exchange, symbol, timeframe")
    return cur.fetchall()

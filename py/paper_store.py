"""E1 paper-trading storage layer: SQLite file data/paper.db, entirely
separate from data/market.db. Schema owned by this module only -- does not
reuse db.py's SCHEMA/connect (those are for klines/funding, market.db is
only ever written to via fetch_klines.py's existing resumable fetch path).
All upserts are INSERT OR REPLACE keyed on the table's own primary key, so
re-running a tick on unchanged inputs is idempotent by construction.
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "paper.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    symbol     TEXT    NOT NULL,
    entry_ts   INTEGER NOT NULL,
    exit_ts    INTEGER,
    direction  TEXT    NOT NULL,
    qty        REAL    NOT NULL,
    entry_px   REAL    NOT NULL,
    exit_px    REAL,
    fees       REAL    NOT NULL,
    funding    REAL    NOT NULL DEFAULT 0,
    pnl        REAL,
    reason     TEXT,
    PRIMARY KEY (symbol, entry_ts)
);
CREATE TABLE IF NOT EXISTS paper_equity (
    symbol TEXT    NOT NULL,
    ts     INTEGER NOT NULL,
    equity REAL    NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS paper_state (
    symbol       TEXT PRIMARY KEY,
    last_ts      INTEGER,
    cash         REAL,
    position_dir TEXT,
    qty          REAL,
    entry_px     REAL,
    entry_ts     INTEGER,
    stop_disp    REAL,
    equity       REAL,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS paper_signals (
    symbol      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    close       REAL,
    upper       REAL,
    lower       REAL,
    width_rank  REAL,
    sqz_ok      INTEGER,
    long_sig    INTEGER,
    short_sig   INTEGER,
    position    TEXT,
    stop_disp   REAL,
    action      TEXT,
    reason      TEXT,
    PRIMARY KEY (symbol, ts)
);
"""


def _migrate(con):
    """E13: adds paper_trades.funding to databases created before the
    cost-realism change (CREATE TABLE IF NOT EXISTS never alters an existing
    table). Idempotent -- a no-op once the column exists."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(paper_trades)")]
    if "funding" not in cols:
        con.execute("ALTER TABLE paper_trades ADD COLUMN funding REAL NOT NULL DEFAULT 0")
        con.commit()


def connect(db_path=None):
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def upsert_trades(con, symbol, trades_df):
    """trades_df: engine.run()'s trades_df, columns entry_ts, exit_ts,
    direction, qty, entry_px, exit_px, fees, funding, pnl, exit_reason. Open
    (not yet closed) trades have exit_ts/exit_px/pnl = NaN/None -- stored as
    NULL; funding is the settled-so-far amount (0.0 when disabled)."""
    def _n(v):
        return None if v is None or (isinstance(v, float) and v != v) else v

    rows = []
    for r in trades_df.itertuples():
        exit_ts = _n(r.exit_ts)
        exit_px = _n(r.exit_px)
        pnl = _n(r.pnl)
        rows.append((
            symbol, int(r.entry_ts), int(exit_ts) if exit_ts is not None else None,
            r.direction, float(r.qty), float(r.entry_px),
            float(exit_px) if exit_px is not None else None,
            float(r.fees), float(getattr(r, "funding", 0.0)),
            float(pnl) if pnl is not None else None,
            r.exit_reason,
        ))
    con.executemany(
        "INSERT OR REPLACE INTO paper_trades "
        "(symbol, entry_ts, exit_ts, direction, qty, entry_px, exit_px, fees, funding, pnl, reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def upsert_signals(con, symbol, rows):
    """rows: iterable of dicts with keys ts, close, upper, lower, width_rank,
    sqz_ok, long_sig, short_sig, position, stop_disp, action, reason.

    upper/lower/width_rank/sqz_ok belong to the built-in squeeze rule; a
    different strategy plugin leaves them as None and they are stored NULL.
    sqz_ok in particular must NOT collapse to 0 there -- 0 would read as
    "the squeeze gate was closed", a claim about a rule that never ran."""
    data = [(symbol, int(r["ts"]), r["close"], r["upper"], r["lower"], r["width_rank"],
              None if r["sqz_ok"] is None else int(bool(r["sqz_ok"])),
              int(bool(r["long_sig"])), int(bool(r["short_sig"])),
              r["position"], r["stop_disp"], r["action"], r["reason"])
             for r in rows]
    con.executemany(
        "INSERT OR REPLACE INTO paper_signals "
        "(symbol, ts, close, upper, lower, width_rank, sqz_ok, long_sig, short_sig, "
        "position, stop_disp, action, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    con.commit()
    return len(data)


def upsert_equity(con, symbol, rows):
    """rows: iterable of (ts, equity)."""
    data = [(symbol, int(ts), float(eq)) for ts, eq in rows]
    con.executemany(
        "INSERT OR REPLACE INTO paper_equity (symbol, ts, equity) VALUES (?,?,?)", data)
    con.commit()
    return len(data)


def upsert_state(con, symbol, state):
    """state: dict with keys last_ts, cash, position_dir, qty, entry_px,
    entry_ts, stop_disp, equity, updated_at."""
    con.execute(
        "INSERT OR REPLACE INTO paper_state "
        "(symbol, last_ts, cash, position_dir, qty, entry_px, entry_ts, stop_disp, equity, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (symbol, state["last_ts"], state["cash"], state["position_dir"], state["qty"],
          state["entry_px"], state["entry_ts"], state["stop_disp"], state["equity"],
          state["updated_at"]))
    con.commit()


def count_trades(con, symbol):
    cur = con.execute("SELECT COUNT(*) FROM paper_trades WHERE symbol=?", (symbol,))
    return cur.fetchone()[0]


def count_signals(con, symbol):
    cur = con.execute("SELECT COUNT(*) FROM paper_signals WHERE symbol=?", (symbol,))
    return cur.fetchone()[0]


def get_trades(con, symbol):
    cur = con.execute(
        "SELECT symbol, entry_ts, exit_ts, direction, qty, entry_px, exit_px, fees, funding, pnl, reason "
        "FROM paper_trades WHERE symbol=? ORDER BY entry_ts", (symbol,))
    cols = ["symbol", "entry_ts", "exit_ts", "direction", "qty", "entry_px", "exit_px",
            "fees", "funding", "pnl", "reason"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def table_counts(con):
    out = {}
    for t in ("paper_trades", "paper_signals", "paper_equity", "paper_state"):
        cur = con.execute(f"SELECT COUNT(*) FROM {t}")
        out[t] = cur.fetchone()[0]
    return out

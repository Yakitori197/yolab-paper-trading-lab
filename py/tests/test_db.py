import db

ROWS = [
    [1000, 1.0, 2.0, 0.5, 1.5, 10.0],
    [2000, 1.5, 2.5, 1.0, 2.0, 11.0],
    [3000, 2.0, 3.0, 1.5, 2.5, 12.0],
]


def make_con(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_upsert_and_count(tmp_path):
    con = make_con(tmp_path)
    assert db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS) == 3
    assert db.series_stats(con, "binance", "BTC/USDT", "4h")[0] == 3


def test_upsert_idempotent(tmp_path):
    con = make_con(tmp_path)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS)
    assert db.series_stats(con, "binance", "BTC/USDT", "4h")[0] == 3


def test_replace_updates_row(tmp_path):
    con = make_con(tmp_path)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", [[2000, 9, 9, 9, 9, 9]])
    assert con.execute("SELECT close FROM klines WHERE ts=2000").fetchone()[0] == 9
    assert db.series_stats(con, "binance", "BTC/USDT", "4h")[0] == 3


def test_series_isolation(tmp_path):
    con = make_con(tmp_path)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS)
    db.upsert_klines(con, "bitstamp", "ETH/USD", "4h", ROWS[:2])
    assert db.series_stats(con, "binance", "BTC/USDT", "4h")[0] == 3
    assert db.series_stats(con, "bitstamp", "ETH/USD", "4h")[0] == 2
    assert len(db.list_series(con)) == 2


def test_bounds(tmp_path):
    con = make_con(tmp_path)
    db.upsert_klines(con, "binance", "BTC/USDT", "4h", ROWS)
    n, tmin, tmax = db.series_stats(con, "binance", "BTC/USDT", "4h")
    assert (n, tmin, tmax) == (3, 1000, 3000)

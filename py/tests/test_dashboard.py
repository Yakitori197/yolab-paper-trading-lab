"""E2/E5 tests for dashboard.py. Synthetic data only, in temp sqlite files
(never the real data/paper.db or data/market.db) -- paper_store.DB_PATH and
db.DB_PATH are monkeypatched to point at those temp files before each
TestClient request, since dashboard.py resolves them fresh (module
attribute lookup) on every call."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import db as market_db
import paper_loop as pl
import paper_store as ps

import dashboard

STEP_MS = pl.STEP_MS


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    market_path = tmp_path / "market.db"
    paper_path = tmp_path / "paper.db"

    mcon = sqlite3.connect(str(market_path))
    mcon.executescript(market_db.SCHEMA)
    mcon.commit()
    mcon.close()

    pcon = sqlite3.connect(str(paper_path))
    pcon.executescript(ps.SCHEMA)
    pcon.commit()
    pcon.close()

    monkeypatch.setattr(market_db, "DB_PATH", market_path)
    monkeypatch.setattr(ps, "DB_PATH", paper_path)

    return dict(market_path=market_path, paper_path=paper_path)


def _seed_state(paper_path, symbol, last_ts, equity=10500.0, cash=9000.0, position_dir="long",
                 qty=1.5, entry_px=100.0, stop_disp=95.0):
    con = sqlite3.connect(str(paper_path))
    con.execute(
        "INSERT OR REPLACE INTO paper_state "
        "(symbol, last_ts, cash, position_dir, qty, entry_px, entry_ts, stop_disp, equity, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (symbol, last_ts, cash, position_dir, qty, entry_px, last_ts if position_dir else None,
         stop_disp if position_dir else None, equity, datetime.now(tz=timezone.utc).isoformat()))
    con.commit()
    con.close()


def _insert_signals(paper_path, rows):
    con = sqlite3.connect(str(paper_path))
    con.executemany(
        "INSERT INTO paper_signals (symbol, ts, close, upper, lower, width_rank, sqz_ok, "
        "long_sig, short_sig, position, stop_disp, action, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    con.commit()
    con.close()


def _insert_trades(paper_path, rows):
    con = sqlite3.connect(str(paper_path))
    con.executemany(
        "INSERT INTO paper_trades (symbol, entry_ts, exit_ts, direction, qty, entry_px, exit_px, "
        "fees, pnl, reason) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _aligned_past_ts(bars_ago):
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    return (now_ms // STEP_MS - bars_ago) * STEP_MS


def test_summary_contains_announcement_and_three_symbol_snapshots(dbs):
    aligned = _aligned_past_ts(3)  # 12h old, well clear of the 4h freshness check
    for symbol in pl.SYMBOLS:
        _seed_state(dbs["paper_path"], symbol, aligned)

    client = TestClient(dashboard.app)
    r = client.get("/api/summary")

    assert r.status_code == 200
    body = r.json()
    assert body["announcement"] == pl.ANNOUNCEMENT
    assert set(body["symbols"].keys()) == set(pl.SYMBOLS)
    for symbol in pl.SYMBOLS:
        assert body["symbols"][symbol]["last_ts"] == aligned


def test_equity_endpoint_is_ascending_by_ts_with_correct_values(dbs):
    symbol = "BTC/USDT"
    con = sqlite3.connect(str(dbs["paper_path"]))
    # inserted deliberately out of order
    rows = [(symbol, 3000, 10300.0), (symbol, 1000, 10100.0), (symbol, 2000, 10200.5)]
    con.executemany("INSERT INTO paper_equity (symbol, ts, equity) VALUES (?,?,?)", rows)
    con.commit()
    con.close()

    client = TestClient(dashboard.app)
    r = client.get("/api/equity", params={"symbol": symbol})

    assert r.status_code == 200
    body = r.json()
    assert [row["ts"] for row in body] == [1000, 2000, 3000]
    assert body[0]["equity"] == pytest.approx(10100.0)
    assert body[1]["equity"] == pytest.approx(10200.5)
    assert body[2]["equity"] == pytest.approx(10300.0)


def test_signals_only_action_filter(dbs):
    symbol = "ETH/USDT"
    rows = [
        (symbol, 1000, 100.0, 105.0, 95.0, 10.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        (symbol, 2000, 101.0, 105.0, 95.0, 8.0, 1, 1, 0, "long", 99.0, "entry", "entry long @ 101.0"),
        (symbol, 3000, 102.0, 105.0, 95.0, 30.0, 0, 0, 0, "long", 99.5, "hold", "已持倉同向"),
        (symbol, 4000, 90.0, 105.0, 95.0, 12.0, 0, 0, 0, None, None, "exit", "exit long @ 90.0 (stop)"),
    ]
    _insert_signals(dbs["paper_path"], rows)

    client = TestClient(dashboard.app)

    r_all = client.get("/api/signals", params={"symbol": symbol, "limit": 200, "only_action": 0})
    assert r_all.status_code == 200
    all_rows = r_all.json()
    assert [row["ts"] for row in all_rows] == [4000, 3000, 2000, 1000]  # DESC

    r_action = client.get("/api/signals", params={"symbol": symbol, "limit": 200, "only_action": 1})
    assert r_action.status_code == 200
    action_rows = r_action.json()
    assert [row["ts"] for row in action_rows] == [4000, 2000]
    assert all(row["action"] != "hold" for row in action_rows)


def test_health_flags_an_unclosed_bar(dbs):
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    unclosed_ts = now_ms - 3600 * 1000  # 1h ago -- within the last 4h, must fail
    for symbol in pl.SYMBOLS:
        _seed_state(dbs["paper_path"], symbol, unclosed_ts)

    client = TestClient(dashboard.app)
    r = client.get("/api/health")

    assert r.status_code == 200
    body = r.json()
    assert body["health"]["ok"] is False
    for symbol in pl.SYMBOLS:
        sym_health = body["health"]["symbols"][symbol]
        assert sym_health["ok"] is False
        assert any("未收盤" in reason for reason in sym_health["reasons"])


# ---- E5 additions -----------------------------------------------------

def test_summary_ret_pct_and_position_structure(dbs):
    aligned = _aligned_past_ts(3)
    _seed_state(dbs["paper_path"], "BTC/USDT", aligned, equity=11000.0, cash=9500.0,
                position_dir="long", entry_px=100.0, stop_disp=95.0)
    _seed_state(dbs["paper_path"], "ETH/USDT", aligned, equity=9800.0, cash=9800.0,
                position_dir=None)
    _seed_state(dbs["paper_path"], "SOL/USDT", aligned, equity=10000.0, cash=10000.0,
                position_dir=None)

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()

    btc = body["symbols"]["BTC/USDT"]
    assert btc["ret_pct"] == pytest.approx(11000.0 / pl.CASH0 - 1.0)
    assert btc["position"]["dir"] == "long"
    assert btc["position"]["entry_px"] == pytest.approx(100.0)
    assert btc["position"]["stop"] == pytest.approx(95.0)
    assert btc["position"]["unrealized"] == pytest.approx(11000.0 - 9500.0)

    eth = body["symbols"]["ETH/USDT"]
    assert eth["position"] is None
    assert eth["ret_pct"] == pytest.approx(9800.0 / pl.CASH0 - 1.0)


def test_trade_reason_text_stop_branch_with_and_without_gap(dbs):
    symbol = "BTC/USDT"
    # trade 1: no-gap stop-out (exit_px close to the last ratcheted stop)
    _insert_signals(dbs["paper_path"], [
        (symbol, 1000, 100.0, 105.0, 95.0, 10.0, 1, 1, 0, "long", 90.0, "entry", "entry long @ 100.0"),
        (symbol, 2000, 101.0, 105.0, 95.0, 10.0, 0, 0, 0, "long", 92.0, "hold", "已持倉同向"),
        (symbol, 3000, 102.0, 105.0, 95.0, 10.0, 0, 0, 0, "long", 95.0, "hold", "已持倉同向"),
        (symbol, 4000, 103.0, 105.0, 95.0, 10.0, 0, 0, 0, "long", 95.0, "hold", "已持倉同向"),
        (symbol, 5000, 94.8, 105.0, 95.0, 10.0, 0, 0, 0, None, None, "exit", "exit long @ 94.8 (stop)"),
    ])
    _insert_trades(dbs["paper_path"], [
        (symbol, 1000, 5000, "long", 1.0, 100.0, 94.8, 1.0, -10.0, "stop"),
    ])
    # trade 2: gapped stop-out (exit_px far below the last ratcheted stop)
    _insert_signals(dbs["paper_path"], [
        (symbol, 10000, 200.0, 210.0, 190.0, 10.0, 1, 1, 0, "long", 180.0, "entry", "entry long @ 200.0"),
        (symbol, 11000, 201.0, 210.0, 190.0, 10.0, 0, 0, 0, "long", 190.0, "hold", "已持倉同向"),
        (symbol, 12000, 202.0, 210.0, 190.0, 10.0, 0, 0, 0, "long", 195.0, "hold", "已持倉同向"),
        (symbol, 13000, 203.0, 210.0, 190.0, 10.0, 0, 0, 0, "long", 195.0, "hold", "已持倉同向"),
        (symbol, 14000, 180.0, 210.0, 190.0, 10.0, 0, 0, 0, None, None, "exit", "exit long @ 180.0 (stop)"),
    ])
    _insert_trades(dbs["paper_path"], [
        (symbol, 10000, 14000, "long", 1.0, 200.0, 180.0, 1.0, -21.0, "stop"),
    ])

    client = TestClient(dashboard.app)
    rows = client.get("/api/trades", params={"symbol": symbol}).json()
    by_entry = {r["entry_ts"]: r for r in rows}

    text1 = by_entry[1000]["reason_text"]
    assert "多單觸及追蹤停損" in text1
    assert "90.00" in text1  # 首停損
    assert "95.00" in text1  # 末停損 (last non-null stop before/at exit)
    assert "跳空開盤" not in text1

    text2 = by_entry[10000]["reason_text"]
    assert "跳空開盤，以開盤價成交" in text2


def test_ohlc_ascending_and_range(dbs, monkeypatch):
    # E10: /api/ohlc now defaults to the live binance proxy; force the
    # network call to fail so this test exercises the market.db fallback
    # path deterministically (source="db"), same range/ordering logic as
    # the pre-E10 endpoint.
    dashboard._OHLC_CACHE.clear()
    symbol = "BTC/USDT"
    old_epoch_ms = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(pl, "epoch_ms", lambda: old_epoch_ms)

    def fake_get(*a, **kw):
        raise Exception("network down")
    monkeypatch.setattr(dashboard.httpx, "get", fake_get)

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    day_ms = 24 * 3600 * 1000
    too_old = now_ms - 40 * day_ms   # outside the 30d window -> excluded
    within = now_ms - 20 * day_ms    # inside the 30d window -> included
    recent = now_ms - 1 * day_ms     # inside the 30d window -> included

    con = sqlite3.connect(str(dbs["market_path"]))
    rows = [
        ("binance", symbol, "4h", recent, 10.0, 11.0, 9.0, 10.5, 100.0),
        ("binance", symbol, "4h", too_old, 1.0, 2.0, 0.5, 1.5, 50.0),
        ("binance", symbol, "4h", within, 5.0, 6.0, 4.0, 5.5, 75.0),
    ]
    con.executemany(
        "INSERT INTO klines (exchange, symbol, timeframe, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    client = TestClient(dashboard.app)
    body = client.get("/api/ohlc", params={"symbol": symbol, "tf": "4h"}).json()

    assert body["source"] == "db"
    bars = body["bars"]
    assert [r["ts"] for r in bars] == [within, recent]
    assert set(bars[0].keys()) == {"ts", "open", "high", "low", "close", "volume", "closed"}
    assert all(b["closed"] is True for b in bars)


def test_events_silence_and_action_segmentation(dbs):
    symbol = "BTC/USDT"
    t0 = 1_000_000
    ts = [t0 + i * STEP_MS for i in range(8)]

    _insert_signals(dbs["paper_path"], [
        (symbol, ts[0], 90.0, 105.0, 95.0, 25.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        (symbol, ts[1], 91.0, 105.0, 95.0, 22.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        (symbol, ts[2], 111.0, 110.0, 90.0, 15.0, 1, 1, 0, "long", 85.0, "entry", "entry long @ 111.0"),
        (symbol, ts[3], 112.0, 110.0, 90.0, 15.0, 0, 0, 0, "long", 90.0, "hold", "已持倉同向"),
        (symbol, ts[4], 113.0, 110.0, 90.0, 15.0, 0, 0, 0, "long", 92.0, "hold", "已持倉同向"),
        (symbol, ts[5], 89.0, 110.0, 90.0, 15.0, 0, 0, 0, None, None, "exit", "exit long @ 89.0 (stop)"),
        (symbol, ts[6], 88.0, 105.0, 90.0, 30.0, 0, 0, 0, None, None, "hold", "未突破上/下軌"),
        (symbol, ts[7], 87.0, 105.0, 90.0, 28.0, 0, 0, 0, None, None, "hold", "未突破上/下軌"),
    ])
    _insert_trades(dbs["paper_path"], [
        (symbol, ts[2], ts[5], "long", 1.0, 111.0, 89.0, 1.0, -25.0, "stop"),
    ])

    client = TestClient(dashboard.app)
    items = client.get("/api/events", params={"symbol": symbol}).json()

    assert len(items) == 5
    kinds = [(it["type"], it.get("kind")) for it in items]
    assert kinds == [
        ("silence", None), ("event", "exit"), ("silence", None), ("event", "entry"), ("silence", None),
    ]

    silence_after = items[0]
    assert silence_after["from_ts"] == ts[6] and silence_after["to_ts"] == ts[7]
    assert silence_after["n"] == 2
    assert "未進場" in silence_after["text"]
    assert "未突破上/下軌 ×2" in silence_after["text"]

    exit_ev = items[1]
    assert "多單觸及追蹤停損 92.00" in exit_ev["text"]
    assert "出場 89.00" in exit_ev["text"]
    assert "-25.00" in exit_ev["text"]
    assert "隨行情棘輪上移 2 次" in exit_ev["sub"]

    silence_mid = items[2]
    assert silence_mid["n"] == 2
    assert "持倉未動作" in silence_mid["text"]
    assert "已持倉同向 ×2" in silence_mid["text"]
    assert "停損上移 1 次（90.00 → 92.00）" in silence_mid["text"]

    entry_ev = items[3]
    assert entry_ev["text"] == "做多 111.00"
    assert "布林通道寬度排名 15.0" in entry_ev["sub"]
    assert "突破上軌 110.00" in entry_ev["sub"]

    silence_before = items[4]
    assert silence_before["n"] == 2
    assert "rank 未低於 20 ×2" in silence_before["text"]


def test_events_near_miss_width_rank(dbs):
    symbol = "ETH/USDT"
    t0 = 2_000_000
    ts = [t0 + i * STEP_MS for i in range(3)]
    _insert_signals(dbs["paper_path"], [
        (symbol, ts[0], 50.0, 55.0, 45.0, 30.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        (symbol, ts[1], 51.0, 55.0, 45.0, 22.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        (symbol, ts[2], 52.0, 55.0, 45.0, 45.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
    ])

    client = TestClient(dashboard.app)
    items = client.get("/api/events", params={"symbol": symbol}).json()

    assert len(items) == 1
    assert items[0]["type"] == "silence"
    assert "（最接近 22.0）" in items[0]["text"]


# ---- E9: heartbeat (last_check_ts / latest_verdict / overdue flag) -----

def test_summary_last_check_ts_is_max_of_updated_at(dbs):
    aligned = _aligned_past_ts(3)
    for symbol in pl.SYMBOLS:
        _seed_state(dbs["paper_path"], symbol, aligned)

    con = sqlite3.connect(str(dbs["paper_path"]))
    ts_map = {
        "BTC/USDT": "2026-08-20T10:00:00+00:00",
        "ETH/USDT": "2026-08-20T12:30:00+00:00",  # max
        "SOL/USDT": "2026-08-20T09:15:00+00:00",
    }
    for symbol, iso in ts_map.items():
        con.execute("UPDATE paper_state SET updated_at=? WHERE symbol=?", (iso, symbol))
    con.commit()
    con.close()

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()
    assert body["last_check_ts"] == "2026-08-20T12:30:00Z"


def test_summary_latest_verdict_from_latest_signal_row(dbs):
    aligned = _aligned_past_ts(3)
    for symbol in pl.SYMBOLS:
        _seed_state(dbs["paper_path"], symbol, aligned)
    _insert_signals(dbs["paper_path"], [
        ("BTC/USDT", 1000, 100.0, 105.0, 95.0, 10.0, 0, 0, 0, None, None, "hold", "rank 未低於 20"),
        ("BTC/USDT", 2000, 101.0, 105.0, 95.0, 8.0, 0, 0, 0, None, None, "hold", "未突破上/下軌"),
    ])

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()
    verdict = body["symbols"]["BTC/USDT"]["latest_verdict"]
    assert verdict["ts"] == 2000
    assert verdict["action"] == "hold"
    assert verdict["reason"] == "未突破上/下軌"


def test_summary_last_check_overdue_flag(dbs):
    aligned = _aligned_past_ts(3)
    for symbol in pl.SYMBOLS:
        _seed_state(dbs["paper_path"], symbol, aligned)

    stale_iso = (datetime.now(tz=timezone.utc) - timedelta(hours=6)).isoformat()
    con = sqlite3.connect(str(dbs["paper_path"]))
    for symbol in pl.SYMBOLS:
        con.execute("UPDATE paper_state SET updated_at=? WHERE symbol=?", (stale_iso, symbol))
    con.commit()
    con.close()

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()
    assert body["last_check_overdue"] is True

    fresh_iso = datetime.now(tz=timezone.utc).isoformat()
    con = sqlite3.connect(str(dbs["paper_path"]))
    for symbol in pl.SYMBOLS:
        con.execute("UPDATE paper_state SET updated_at=? WHERE symbol=?", (fresh_iso, symbol))
    con.commit()
    con.close()

    body2 = client.get("/api/summary").json()
    assert body2["last_check_overdue"] is False


# ---- E10: /api/ohlc tf switch (live proxy + cache + db fallback) -------

class _FakeBinanceResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_kline_rows(n, interval_ms, now_ms):
    """Synthetic Binance kline rows, ascending, ending at now_ms -- every
    bar's closeTime is in the past except the last (still forming)."""
    rows = []
    start = now_ms - n * interval_ms
    for i in range(n):
        open_ts = start + i * interval_ms
        close_ts = open_ts + interval_ms - 1
        rows.append([open_ts, "100.0", "105.0", "95.0", "102.0", "10.0",
                     close_ts, "0", 0, "0", "0", "0"])
    return rows


def test_ohlc_invalid_tf_returns_400(dbs):
    client = TestClient(dashboard.app)
    r = client.get("/api/ohlc", params={"symbol": "BTC/USDT", "tf": "2h"})
    assert r.status_code == 400


def test_ohlc_cache_avoids_repeat_external_calls_within_30s(dbs, monkeypatch):
    dashboard._OHLC_CACHE.clear()
    calls = {"n": 0}
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeBinanceResp(_fake_kline_rows(5, 15 * 60 * 1000, now_ms))
    monkeypatch.setattr(dashboard.httpx, "get", fake_get)

    client = TestClient(dashboard.app)
    r1 = client.get("/api/ohlc", params={"symbol": "BTC/USDT", "tf": "15m"})
    r2 = client.get("/api/ohlc", params={"symbol": "BTC/USDT", "tf": "15m"})

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert calls["n"] == 1


def test_ohlc_4h_network_failure_falls_back_to_db_with_source_tag(dbs, monkeypatch):
    dashboard._OHLC_CACHE.clear()
    symbol = "BTC/USDT"
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    con = sqlite3.connect(str(dbs["market_path"]))
    con.execute(
        "INSERT INTO klines (exchange, symbol, timeframe, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("binance", symbol, "4h", now_ms - STEP_MS, 10.0, 11.0, 9.0, 10.5, 100.0))
    con.commit()
    con.close()

    def fake_get(*a, **kw):
        raise Exception("network down")
    monkeypatch.setattr(dashboard.httpx, "get", fake_get)

    client = TestClient(dashboard.app)
    r = client.get("/api/ohlc", params={"symbol": symbol, "tf": "4h"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "db"
    assert len(body["bars"]) == 1
    assert body["bars"][0]["closed"] is True


def test_ohlc_15m_network_failure_returns_live_unavailable_not_500(dbs, monkeypatch):
    dashboard._OHLC_CACHE.clear()

    def fake_get(*a, **kw):
        raise Exception("network down")
    monkeypatch.setattr(dashboard.httpx, "get", fake_get)

    client = TestClient(dashboard.app)
    r = client.get("/api/ohlc", params={"symbol": "BTC/USDT", "tf": "15m"})
    assert r.status_code == 200
    assert r.json() == {"error": "live_unavailable"}


def test_stall_exit_reason_text_and_event_wording(dbs):
    """Batch #8 stall exits close AT the bar's close -- the stop-touch
    narrative must NOT be used for them. Hand-derived: short entry@100 at
    ts[0], stall exit@101 at ts[6] -> held 6 bars, close still ABOVE the
    short's entry -> '高於進場價'; both /api/trades reason_text and the
    /api/events exit item must say 停滯出場 and never 觸及追蹤停損."""
    symbol = "ETH/USDT"
    t0 = 5_000_000
    ts = [t0 + i * STEP_MS for i in range(7)]
    sig_rows = [(symbol, ts[0], 100.0, 105.0, 99.5, 10.0, 1, 0, 1, "short", 108.0,
                 "entry", "entry short @ 100.0")]
    for i in range(1, 6):
        sig_rows.append((symbol, ts[i], 100.5, 105.0, 95.0, 30.0, 0, 0, 0, "short", 107.0,
                         "hold", "已持倉同向"))
    sig_rows.append((symbol, ts[6], 101.0, 105.0, 95.0, 30.0, 0, 0, 0, None, None,
                     "exit", "exit short @ 101.0 (stall)"))
    _insert_signals(dbs["paper_path"], sig_rows)
    _insert_trades(dbs["paper_path"], [
        (symbol, ts[0], ts[6], "short", 1.0, 100.0, 101.0, 0.2, -1.2, "stall"),
    ])

    client = TestClient(dashboard.app)

    trades = client.get("/api/trades", params={"symbol": symbol}).json()
    text = trades[0]["reason_text"]
    assert "空單停滯出場" in text
    assert f"第 {pl.STALL_BARS} 棒起" in text
    assert "第 6 棒收盤仍高於進場價" in text
    assert "101.00" in text
    assert "追蹤停損" not in text

    items = client.get("/api/events", params={"symbol": symbol}).json()
    exit_ev = next(it for it in items if it["type"] == "event" and it["kind"] == "exit")
    assert "空單停滯出場 101.00" in exit_ev["text"]
    assert "-1.20" in exit_ev["text"]
    assert "第 6 棒" in exit_ev["sub"]
    assert "追蹤停損" not in exit_ev["text"] and "追蹤停損" not in exit_ev["sub"]


# ---- E15: /api/summary trigger / managed / exit_config blocks ----------

def _insert_state_row(paper_path, symbol, last_ts, position_dir, qty, entry_px, entry_ts,
                       stop_disp, cash=9000.0, equity=10500.0):
    con = sqlite3.connect(str(paper_path))
    con.execute(
        "INSERT OR REPLACE INTO paper_state "
        "(symbol, last_ts, cash, position_dir, qty, entry_px, entry_ts, stop_disp, equity, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (symbol, last_ts, cash, position_dir, qty, entry_px, entry_ts, stop_disp, equity,
         datetime.now(tz=timezone.utc).isoformat()))
    con.commit()
    con.close()


def test_summary_trigger_distances_and_ranks(dbs):
    """Hand-derived: latest bar close=100 upper=102 lower=97 ->
    dist_up = (102-100)/100*100 = 2.0, dist_dn = (100-97)/100*100 = 3.0.
    Ranks ascending [60,40,25,30,51] -> newest-first [51,30,25,40,60],
    min 25 -> gate_next False (25 is not < 20)."""
    symbol = "BTC/USDT"
    aligned = _aligned_past_ts(3)
    _seed_state(dbs["paper_path"], symbol, aligned, position_dir=None)
    t0 = 1_000_000
    ranks_asc = [60.0, 40.0, 25.0, 30.0, 51.0]
    rows = []
    for i, rk in enumerate(ranks_asc):
        close, upper, lower = (100.0, 102.0, 97.0) if i == 4 else (99.0, 103.0, 96.0)
        rows.append((symbol, t0 + i * STEP_MS, close, upper, lower, rk, 0, 0, 0,
                     None, None, "hold", "rank 未低於 20"))
    _insert_signals(dbs["paper_path"], rows)

    client = TestClient(dashboard.app)
    tg = client.get("/api/summary").json()["symbols"][symbol]["trigger"]

    assert tg["close"] == pytest.approx(100.0)
    assert tg["dist_up_pct"] == pytest.approx(2.0)
    assert tg["dist_dn_pct"] == pytest.approx(3.0)
    assert tg["recent_ranks"] == [51.0, 30.0, 25.0, 40.0, 60.0]
    assert tg["min_rank"] == pytest.approx(25.0)
    assert tg["gate_next"] is False
    assert tg["sqz_win"] == 5 and tg["sqz_thresh"] == pytest.approx(20.0)


def test_summary_trigger_gate_strict_threshold_boundary(dbs):
    """strategy_squeeze uses a STRICT `< sqz_thresh`: min rank exactly 20.0
    keeps the gate closed; 19.9 opens it."""
    t0 = 2_000_000

    def _rows(symbol, min_rank):
        ranks = [80.0, 70.0, min_rank, 60.0, 50.0]
        return [(symbol, t0 + i * STEP_MS, 100.0, 102.0, 97.0, rk, 0, 0, 0,
                 None, None, "hold", "rank 未低於 20") for i, rk in enumerate(ranks)]

    _insert_signals(dbs["paper_path"], _rows("BTC/USDT", 20.0))
    _insert_signals(dbs["paper_path"], _rows("ETH/USDT", 19.9))

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()
    assert body["symbols"]["BTC/USDT"]["trigger"]["gate_next"] is False
    assert body["symbols"]["ETH/USDT"]["trigger"]["gate_next"] is True


def test_summary_trigger_gate_requires_full_rank_window(dbs):
    """Only 3 stored bars (window needs 5): pandas' rolling(5).min() would be
    NaN -> gate closed, even though the min of what exists is far below 20.
    The display mirror must agree."""
    symbol = "SOL/USDT"
    t0 = 3_000_000
    rows = [(symbol, t0 + i * STEP_MS, 100.0, 102.0, 97.0, rk, 0, 0, 0,
             None, None, "hold", "rank 未低於 20") for i, rk in enumerate([5.0, 8.0, 3.0])]
    _insert_signals(dbs["paper_path"], rows)

    client = TestClient(dashboard.app)
    tg = client.get("/api/summary").json()["symbols"][symbol]["trigger"]
    assert tg["min_rank"] == pytest.approx(3.0)
    assert tg["gate_next"] is False


def test_summary_managed_stall_and_breakeven(dbs):
    """Hand-derived from the deployed constants (STALL_BARS=6, STALL_GAIN=0,
    BE_TRIGGER=1.0):
    - BTC long held 25 bars, stop 95 < entry 100 -> stall active (25>=6),
      threshold px = entry px (gain=0 makes it r0-independent), BE not locked.
    - ETH short held 3 bars, stop 95 <= entry 100 -> stall not active yet
      (3<6), BE locked (short: stop at/below entry).
    - SOL flat -> managed is None. exit_config echoes the constants."""
    aligned = _aligned_past_ts(30)
    _insert_state_row(dbs["paper_path"], "BTC/USDT", aligned, "long", 1.0, 100.0,
                       aligned - 25 * STEP_MS, 95.0)
    _insert_state_row(dbs["paper_path"], "ETH/USDT", aligned, "short", 1.0, 100.0,
                       aligned - 3 * STEP_MS, 95.0)
    _insert_state_row(dbs["paper_path"], "SOL/USDT", aligned, None, None, None, None, None,
                       cash=10000.0, equity=10000.0)

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()

    btc = body["symbols"]["BTC/USDT"]["managed"]
    assert btc["bars_held"] == 25
    assert btc["stall_active"] is True
    assert btc["stall_threshold_px"] == pytest.approx(100.0)
    assert btc["breakeven_locked"] is False

    eth = body["symbols"]["ETH/USDT"]["managed"]
    assert eth["bars_held"] == 3
    assert eth["stall_active"] is False
    assert eth["breakeven_locked"] is True

    assert body["symbols"]["SOL/USDT"]["managed"] is None
    assert body["exit_config"] == dict(stall_bars=pl.STALL_BARS, stall_gain=pl.STALL_GAIN,
                                        be_trigger=pl.BE_TRIGGER)


def test_summary_managed_boundaries(dbs):
    """Stall eligibility mirrors engine.py's `held >= stall_bars`: held
    exactly 6 -> active, held 5 -> not yet. A stop exactly AT entry counts
    as breakeven-locked (d*(stop-entry) >= 0 with equality)."""
    aligned = _aligned_past_ts(30)
    _insert_state_row(dbs["paper_path"], "BTC/USDT", aligned, "long", 1.0, 100.0,
                       aligned - 6 * STEP_MS, 100.0)
    _insert_state_row(dbs["paper_path"], "ETH/USDT", aligned, "long", 1.0, 100.0,
                       aligned - 5 * STEP_MS, 99.0)

    client = TestClient(dashboard.app)
    body = client.get("/api/summary").json()

    btc = body["symbols"]["BTC/USDT"]["managed"]
    assert btc["bars_held"] == 6 and btc["stall_active"] is True
    assert btc["breakeven_locked"] is True  # stop == entry

    eth = body["symbols"]["ETH/USDT"]["managed"]
    assert eth["bars_held"] == 5 and eth["stall_active"] is False


def test_ohlc_closed_flag_matches_close_time(dbs, monkeypatch):
    dashboard._OHLC_CACHE.clear()
    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    day_ms = 24 * 3600 * 1000
    past_close = now_ms - 1000        # already closed
    future_close = now_ms + 600_000   # not yet closed (10 min from now)
    rows = [
        [now_ms - 2 * day_ms, "1", "2", "0.5", "1.5", "10", past_close, "0", 0, "0", "0", "0"],
        [now_ms - 1 * day_ms, "1", "2", "0.5", "1.5", "10", future_close, "0", 0, "0", "0", "0"],
    ]

    def fake_get(*a, **kw):
        return _FakeBinanceResp(rows)
    monkeypatch.setattr(dashboard.httpx, "get", fake_get)

    client = TestClient(dashboard.app)
    body = client.get("/api/ohlc", params={"symbol": "BTC/USDT", "tf": "1h"}).json()
    bars = body["bars"]
    assert bars[0]["closed"] is True
    assert bars[1]["closed"] is False

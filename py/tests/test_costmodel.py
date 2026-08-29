"""E13 cost-realism contract tests: engine.py funding settlement, the
paper_loop bookkeeping mirror, per-symbol tick resolution, load_funding's
grid normalization, and paper_store's schema migration. Expected numeric
values are hand-derived from engine.py's documented settlement rule (module
docstring), not from running the engine first -- do not adjust them to match
engine output. Synthetic data only; no network, no real databases."""
import sqlite3
import time

import pandas as pd
import pytest

import db
import engine
import paper_loop as pl
import paper_store as ps
from strategy_squeeze import build_signals
from test_paper import _paper_con, _synthetic_df

STEP = pl.STEP_MS


def make_df(rows):
    """Same shape helper as test_engine.py's: ts/Open/High/Low/Close/atr with
    optional long_sig/short_sig/in_win (default False/False/True)."""
    out = []
    for r in rows:
        out.append(dict(
            ts=r["ts"], Open=r["Open"], High=r["High"], Low=r["Low"], Close=r["Close"],
            atr=r["atr"], long_sig=r.get("long_sig", False), short_sig=r.get("short_sig", False),
            in_win=r.get("in_win", True)))
    return pd.DataFrame(out)


def flat_bar(ts, px=100.0, atr=5.0, **kw):
    return dict(ts=ts, Open=px, High=px, Low=px, Close=px, atr=atr, **kw)


def test_long_pays_one_settlement_included_in_pnl_and_cash():
    # entry at i0 close (fill instant = STEP); the only supplied rate sits at
    # ts=2*STEP where (i - entry_bar) == 2 -> eligible. Hand calc: qty=100,
    # Open[2]=100, rate=0.001 -> amt = +1 * 100 * 100 * 0.001 = 10.0 paid.
    # window_end close at i3 @100 with fee=0/slip=0 -> gross 0, pnl = -10.
    df = make_df([
        flat_bar(0, long_sig=True),
        flat_bar(STEP),
        flat_bar(2 * STEP),
        flat_bar(3 * STEP, in_win=False),
    ])
    metrics, trades = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                                 funding={2 * STEP: 0.001})
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["funding"] == pytest.approx(10.0)
    assert tr["pnl"] == pytest.approx(-10.0)
    assert tr["exit_reason"] == "window_end"
    assert metrics["funding_total"] == pytest.approx(10.0)
    assert metrics["funding_events"] == 1
    assert metrics["net"] == pytest.approx(-10.0)


def test_short_receives_positive_rate():
    # same skeleton, short side: amt = -1 * 100 * 100 * 0.001 = -10 (received),
    # so the flat round-trip nets +10.
    df = make_df([
        flat_bar(0, short_sig=True),
        flat_bar(STEP),
        flat_bar(2 * STEP),
        flat_bar(3 * STEP, in_win=False),
    ])
    metrics, trades = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                                 funding={2 * STEP: 0.001})
    tr = trades.iloc[0]
    assert tr["funding"] == pytest.approx(-10.0)
    assert tr["pnl"] == pytest.approx(10.0)
    assert metrics["funding_total"] == pytest.approx(-10.0)


def test_entry_fill_at_settlement_instant_pays_nothing():
    # entry on bar i0 fills AT instant STEP (the entry bar's close); a rate
    # keyed exactly there must NOT settle (position entered at bar i-1 of the
    # ts=STEP bar). No other rate exists -> zero funding end to end.
    df = make_df([
        flat_bar(0, long_sig=True),
        flat_bar(STEP),
        flat_bar(2 * STEP),
        flat_bar(3 * STEP, in_win=False),
    ])
    metrics, trades = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                                 funding={STEP: 0.01})
    assert metrics["funding_events"] == 0
    assert metrics["funding_total"] == pytest.approx(0.0)
    assert trades.iloc[0]["pnl"] == pytest.approx(0.0)


def test_stop_exit_bar_still_pays_that_instants_funding():
    # i2 opens at a settlement instant AND trades down through the working
    # stop (stop 90 after two flat ratchet bars). Settlement precedes the
    # exit check: amt = 100 * Open[2]=95 * 0.001 = 9.5; then the stop fills
    # at 90 (ref = min(Open, stop) = 90 -- the open did not gap through).
    # pnl = 100*(90-100) - 9.5 = -1009.5.
    df = make_df([
        flat_bar(0, long_sig=True),
        flat_bar(STEP),
        dict(ts=2 * STEP, Open=95.0, High=95.0, Low=85.0, Close=90.0, atr=5.0),
    ])
    metrics, trades = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                                 funding={2 * STEP: 0.001})
    tr = trades.iloc[0]
    assert tr["exit_ts"] == 2 * STEP
    assert tr["exit_reason"] == "stop"
    assert tr["exit_px"] == pytest.approx(90.0)
    assert tr["funding"] == pytest.approx(9.5)
    assert tr["pnl"] == pytest.approx(-1009.5)
    assert metrics["funding_events"] == 1


def test_inapplicable_or_absent_funding_reproduces_legacy_hand_values():
    # test_engine.py scenario 1's hand-derived pnl (-800) must be reproduced
    # bit-for-bit with funding disabled AND with a funding dict whose keys
    # never match a bar -- proving default behavior is untouched.
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=101, High=103, Low=89, Close=102, atr=5),
        dict(ts=2, Open=101, High=101, Low=91, Close=95, atr=5),
    ])
    m_none, t_none = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)
    m_miss, t_miss = engine.run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                                funding={999_999_999: 0.5})
    for trades in (t_none, t_miss):
        tr = trades.iloc[0]
        assert tr["pnl"] == -800
        assert tr["funding"] == 0.0
    for metrics in (m_none, m_miss):
        assert metrics["funding_total"] == 0.0
        assert metrics["funding_events"] == 0
    assert t_none["pnl"].tolist() == t_miss["pnl"].tolist()


def test_paper_bookkeeping_cash_reconciles_with_engine_under_funding():
    """process_symbol's bookkeeping mirror must land on exactly the cash the
    engine's own trade ledger implies: cash0 + sum(closed pnl) minus, for a
    still-open trade, its entry_fee and settled-so-far funding."""
    df, epoch = _synthetic_df()
    funding_all = {int(t): 0.0001 for t in df["ts"] if int(t) >= epoch}

    sig = build_signals(df, epoch, int(df["ts"].max()))
    metrics, eng_trades = engine.run(sig, cash=pl.CASH0, fee=pl.FEE, tick=pl.TICK,
                                     funding=funding_all)
    assert metrics["funding_total"] != 0.0, "synthetic trade must settle at least one funding"

    con_a = _paper_con()
    pl.process_symbol(con_a, "SYN/TEST", df, epoch, funding_rates=funding_all)
    stored = ps.get_trades(con_a, "SYN/TEST")

    assert len(stored) == len(eng_trades)
    for srow, (_, erow) in zip(stored, eng_trades.iterrows()):
        assert srow["funding"] == pytest.approx(erow["funding"])

    expected_cash = pl.CASH0
    for _, erow in eng_trades.iterrows():
        if pd.isna(erow["exit_ts"]):
            entry_fee = erow["qty"] * erow["entry_px"] * pl.FEE
            expected_cash -= entry_fee + erow["funding"]
        else:
            expected_cash += erow["pnl"]
    cash, _equity = con_a.execute(
        "SELECT cash, equity FROM paper_state WHERE symbol=?", ("SYN/TEST",)).fetchone()
    assert cash == pytest.approx(expected_cash)

    # idempotent rerun with the same funding dict changes nothing
    pl.process_symbol(con_a, "SYN/TEST", df, epoch, funding_rates=funding_all)
    assert ps.get_trades(con_a, "SYN/TEST") == stored


def test_paper_equity_shifts_only_from_the_settlement_bar_onward():
    """Anti-lookahead flavor for the mirror: vs a no-funding run, per-bar
    equity is identical strictly before the first eligible settlement and
    shifted by exactly dir*qty*Open*rate from that bar on."""
    df, epoch = _synthetic_df()
    rate = 0.0001
    funding_all = {int(t): rate for t in df["ts"] if int(t) >= epoch}

    sig = build_signals(df, epoch, int(df["ts"].max()))
    _m, eng_trades = engine.run(sig, cash=pl.CASH0, fee=pl.FEE, tick=pl.TICK, funding=funding_all)
    first = eng_trades.iloc[0]
    e0 = int(first["entry_ts"])
    f0 = e0 + 2 * STEP  # first eligible settlement instant for that trade
    assert pd.isna(first["exit_ts"]) or int(first["exit_ts"]) >= f0, \
        "synthetic first trade must live >= 2 bars for this test's premise"

    con_a, con_b = _paper_con(), _paper_con()
    pl.process_symbol(con_a, "SYN/TEST", df, epoch, funding_rates=funding_all)
    pl.process_symbol(con_b, "SYN/TEST", df, epoch)

    def equity_at(con, ts):
        return con.execute("SELECT equity FROM paper_equity WHERE symbol=? AND ts=?",
                           ("SYN/TEST", ts)).fetchone()[0]

    open_at_f0 = float(df.loc[df["ts"] == f0, "Open"].iloc[0])
    dir_sign = 1.0 if first["direction"] == "long" else -1.0
    amt0 = dir_sign * float(first["qty"]) * open_at_f0 * rate

    assert equity_at(con_a, f0 - STEP) == pytest.approx(equity_at(con_b, f0 - STEP))
    assert equity_at(con_a, f0) == pytest.approx(equity_at(con_b, f0) - amt0)


def test_process_symbol_resolves_per_symbol_tick():
    # tick=None must resolve via TICKS: for ETH/USDT that is 0.01, identical
    # to passing tick=0.01 explicitly and different from the generic 0.1
    # (entry fills differ by the slip difference, 2*(0.1-0.01) = 0.18).
    df, epoch = _synthetic_df()
    con_auto, con_explicit, con_generic = _paper_con(), _paper_con(), _paper_con()
    pl.process_symbol(con_auto, "ETH/USDT", df, epoch)
    pl.process_symbol(con_explicit, "ETH/USDT", df, epoch, tick=0.01)
    pl.process_symbol(con_generic, "ETH/USDT", df, epoch, tick=0.1)

    auto = ps.get_trades(con_auto, "ETH/USDT")
    explicit = ps.get_trades(con_explicit, "ETH/USDT")
    generic = ps.get_trades(con_generic, "ETH/USDT")
    assert auto == explicit
    assert abs(auto[0]["entry_px"] - generic[0]["entry_px"]) == pytest.approx(0.18)

    assert pl.tick_for("BTC/USDT") == 0.1
    assert pl.tick_for("ETH/USDT") == 0.01
    assert pl.tick_for("SOL/USDT") == 0.01
    assert pl.tick_for("SYN/TEST") == pl.TICK


def test_load_funding_normalizes_jitter_sums_collisions_and_maps_symbol():
    g1 = 10_000 * STEP
    g2 = g1 + 4 * STEP
    con = sqlite3.connect(":memory:")
    con.executescript(db.SCHEMA)
    db.upsert_funding(con, "binanceusdm", "BTC/USDT:USDT", [
        [g1 + 7, 1e-4],               # measured-style forward jitter -> g1
        [g1 + 2 * STEP + 46, 2e-4],   # max measured jitter was 47ms
        [g2 - 7_200_000, 5e-5],       # sub-8h settlement, halfway -> rounds up to g2
        [g2, 5e-5],                   # collides with the row above -> rates sum
        [g1 - STEP, 9e-9],            # before start_ms -> excluded
    ])
    db.upsert_funding(con, "binanceusdm", "ETH/USDT:USDT", [[g1, 0.5]])

    btc = pl.load_funding(con, "BTC/USDT", g1)
    assert set(btc.keys()) == {g1, g1 + 2 * STEP, g2}
    assert btc[g1] == pytest.approx(1e-4)
    assert btc[g1 + 2 * STEP] == pytest.approx(2e-4)
    assert btc[g2] == pytest.approx(1e-4)  # 5e-5 + 5e-5 summed

    eth = pl.load_funding(con, "ETH/USDT", g1)
    assert set(eth.keys()) == {g1}
    assert eth[g1] == pytest.approx(0.5)

    assert pl.funding_symbol("SOL/USDT") == "SOL/USDT:USDT"
    assert pl.grid_ts(g1 + 47) == g1
    assert pl.grid_ts(g1 + STEP // 2) == g1 + STEP  # half rounds up


def test_health_check_flags_missing_and_stale_funding():
    mcon = sqlite3.connect(":memory:")
    mcon.executescript(db.SCHEMA)
    pcon = _paper_con()
    now_ms = int(time.time() * 1000)
    last = (now_ms // STEP - 3) * STEP  # aligned, comfortably closed
    ps.upsert_state(pcon, "BTC/USDT", dict(
        last_ts=last, cash=10_000.0, position_dir=None, qty=None, entry_px=None,
        entry_ts=None, stop_disp=None, equity=10_000.0, updated_at="2026-01-01T00:00:00Z"))
    epoch = last - 100 * STEP

    h = pl.health_check(mcon, pcon, epoch)
    assert any("無 funding 資料" in r for r in h["symbols"]["BTC/USDT"]["reasons"])

    # stale: newest settlement two intervals behind the newest bar
    db.upsert_funding(mcon, "binanceusdm", "BTC/USDT:USDT", [[last - 2 * pl.FUNDING_MS, 1e-4]])
    h2 = pl.health_check(mcon, pcon, epoch)
    assert any("funding 資料落後" in r for r in h2["symbols"]["BTC/USDT"]["reasons"])

    # fresh: settlement right at the newest bar -> no funding complaint
    db.upsert_funding(mcon, "binanceusdm", "BTC/USDT:USDT", [[last + 3, 1e-4]])
    h3 = pl.health_check(mcon, pcon, epoch)
    assert not any("funding" in r for r in h3["symbols"]["BTC/USDT"]["reasons"])


def test_paper_store_migration_adds_funding_column_idempotently(tmp_path):
    old_schema = """
    CREATE TABLE paper_trades (
        symbol     TEXT    NOT NULL,
        entry_ts   INTEGER NOT NULL,
        exit_ts    INTEGER,
        direction  TEXT    NOT NULL,
        qty        REAL    NOT NULL,
        entry_px   REAL    NOT NULL,
        exit_px    REAL,
        fees       REAL    NOT NULL,
        pnl        REAL,
        reason     TEXT,
        PRIMARY KEY (symbol, entry_ts)
    );
    """
    path = tmp_path / "old_paper.db"
    con0 = sqlite3.connect(str(path))
    con0.executescript(old_schema)
    con0.execute(
        "INSERT INTO paper_trades (symbol, entry_ts, exit_ts, direction, qty, entry_px, exit_px, "
        "fees, pnl, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("OLD/USDT", 1000, 2000, "long", 1.0, 100.0, 101.0, 0.2, 0.8, "stop"))
    con0.commit()
    con0.close()

    con1 = ps.connect(str(path))
    cols = [r[1] for r in con1.execute("PRAGMA table_info(paper_trades)")]
    assert "funding" in cols
    old_row = ps.get_trades(con1, "OLD/USDT")[0]
    assert old_row["funding"] == 0  # backfilled by the ALTER's DEFAULT
    assert old_row["pnl"] == pytest.approx(0.8)

    new_df = pd.DataFrame([dict(entry_ts=3000, exit_ts=4000, direction="short", qty=2.0,
                                entry_px=50.0, exit_px=49.0, fees=0.1, funding=3.5,
                                pnl=1.9, exit_reason="stop")])
    ps.upsert_trades(con1, "OLD/USDT", new_df)
    assert ps.get_trades(con1, "OLD/USDT")[1]["funding"] == pytest.approx(3.5)
    con1.close()

    con2 = ps.connect(str(path))  # second migration pass must be a no-op
    assert [r[1] for r in con2.execute("PRAGMA table_info(paper_trades)")].count("funding") == 1
    con2.close()

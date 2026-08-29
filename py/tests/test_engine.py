"""Semantic contract for engine.py, derived directly from the CAL-002 engine
spec (Pine strategy.exit delay, ratchet-only-tightens, unified buy/sell slip
direction). Expected numeric values are hand-derived from the spec, not from
running the engine -- do not adjust them to match engine output."""
import pandas as pd

from engine import run


def make_df(rows):
    """rows: list of dicts with ts/Open/High/Low/Close/atr and optional
    long_sig/short_sig/in_win (default False/False/True)."""
    out = []
    for r in rows:
        out.append(dict(
            ts=r["ts"], Open=r["Open"], High=r["High"], Low=r["Low"], Close=r["Close"],
            atr=r["atr"], long_sig=r.get("long_sig", False), short_sig=r.get("short_sig", False),
            in_win=r.get("in_win", True)))
    return pd.DataFrame(out)


def test_stop_delayed_one_bar_then_hits():
    # scenario 1: fee=0, slip=0 (tick=0), stop_mult=2
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=101, High=103, Low=89, Close=102, atr=5),
        dict(ts=2, Open=101, High=101, Low=91, Close=95, atr=5),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)

    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["direction"] == "long"
    assert tr["entry_px"] == 100
    assert tr["qty"] == 100
    # i1's Low=89 < initial stop=90, but the stop is not yet working
    # (Pine only submits strategy.exit the bar after entry) -- must NOT exit here.
    assert tr["exit_ts"] == 2, "stop must not trigger on i1; first working bar is i2"
    assert tr["exit_px"] == 92
    assert tr["pnl"] == -800


def test_ratchet_only_tightens_and_gap_fills_at_open():
    # scenario 2: fee=0, slip=0 (tick=0), stop_mult=2
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=2, long_sig=True),
        dict(ts=1, Open=100, High=106, Low=100, Close=106, atr=2),
        dict(ts=2, Open=104, High=105, Low=103, Close=104, atr=10),
        dict(ts=3, Open=98, High=99, Low=96, Close=97, atr=10),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)

    assert len(trades) == 1
    tr = trades.iloc[0]
    # i2's wide ATR would loosen the stop to 84 if the ratchet didn't clamp
    # to max(prev, candidate); the exit price/pnl below are only reachable
    # if the internal stop was still 102 (not 84) going into i3 -- an exit
    # at 84 would give pnl=-1600, not -200, so this doubles as the "stop
    # stayed 102 after i2" assertion from the spec.
    assert tr["exit_ts"] == 3
    assert tr["exit_px"] == 98, "gap through stop must fill at Open, not stop price"
    assert tr["pnl"] == -200


def test_same_bar_reversal_fee_and_slip_direction():
    # scenario 3: fee=0.001, tick=0.5 -> slip=1
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=110, High=110, Low=110, Close=110, atr=5, short_sig=True),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.001, tick=0.5, stop_mult=2.0)

    assert len(trades) == 2, "long trade closed + short trade opened, same bar"
    long_tr, short_tr = trades.iloc[0], trades.iloc[1]

    assert long_tr["direction"] == "long"
    assert long_tr["entry_px"] == 101  # 100 + slip (buy)
    assert long_tr["qty"] == 10_000 / 101
    assert long_tr["fees"] - (long_tr["qty"] * 109 * 0.001) == 10_000 * 0.001  # entry_fee == 10.0
    assert long_tr["exit_px"] == 109  # 110 - slip (sell to close)

    assert short_tr["direction"] == "short"
    assert short_tr["entry_px"] == 109  # 110 - slip (sell to open)
    assert pd.isna(short_tr["exit_ts"]), "short trade is still open"

    entry_fee = 10_000 * 0.001  # == 10.0, qty*entry_px*fee with qty*entry_px==cash
    exit_fee = long_tr["qty"] * 109 * 0.001
    expected_pnl = long_tr["qty"] * (109 - 101) - entry_fee - exit_fee
    assert abs(long_tr["pnl"] - expected_pnl) < 1e-6


def test_window_end_forces_close_at_that_bars_close():
    # scenario 4: fee=0, slip=0
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=100, High=106, Low=99, Close=105, atr=5),
        dict(ts=2, Open=105, High=106, Low=100, Close=103, atr=5, in_win=False),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)

    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["exit_ts"] == 2
    assert tr["exit_px"] == 103

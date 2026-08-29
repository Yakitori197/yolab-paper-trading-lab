"""M5 contract tests for engine.py's two new (default-off) options:
exit_after_bars and slippage_pct. Expected values are hand-derived from the
spec, exactly like tests/test_engine.py's existing scenarios."""
import pandas as pd

from engine import run


def make_df(rows):
    out = []
    for r in rows:
        out.append(dict(
            ts=r["ts"], Open=r["Open"], High=r["High"], Low=r["Low"], Close=r["Close"],
            atr=r["atr"], long_sig=r.get("long_sig", False), short_sig=r.get("short_sig", False),
            in_win=r.get("in_win", True)))
    return pd.DataFrame(out)


def test_exit_after_bars_1_closes_on_the_very_next_bar_at_its_close():
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=105, High=106, Low=104, Close=105, atr=5),
        dict(ts=2, Open=105, High=200, Low=1, Close=150, atr=5),  # would trip a stop if one existed
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, exit_after_bars=1)

    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["entry_ts"] == 0
    assert tr["exit_ts"] == 1, "exit must land on the bar right after entry, not later"
    assert tr["exit_px"] == 105, "exit must fill at that bar's Close (no slippage configured)"


def test_slippage_pct_direction_and_magnitude():
    pct = 0.01  # 1%, exaggerated so the effect is unambiguous
    df_long = make_df([dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True)])
    _metrics_l, trades_l = run(df_long, cash=10_000, fee=0.0, exit_after_bars=1, slippage_pct=pct)
    long_entry_px = trades_l.iloc[0]["entry_px"]
    assert long_entry_px > 100, "a long (buy) fill must be worse (higher) than the bar's close"
    assert long_entry_px == 100 * (1 + pct)

    df_short = make_df([dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, short_sig=True)])
    _metrics_s, trades_s = run(df_short, cash=10_000, fee=0.0, exit_after_bars=1, slippage_pct=pct)
    short_entry_px = trades_s.iloc[0]["entry_px"]
    assert short_entry_px < 100, "a short (sell) fill must be worse (lower) than the bar's close"
    assert short_entry_px == 100 * (1 - pct)


def test_both_new_options_none_matches_existing_stop_contract():
    # identical to test_engine.py::test_stop_delayed_one_bar_then_hits --
    # proves the new (default) code path is byte-for-byte the same result
    # as before these options existed.
    df = make_df([
        dict(ts=0, Open=100, High=100, Low=100, Close=100, atr=5, long_sig=True),
        dict(ts=1, Open=101, High=103, Low=89, Close=102, atr=5),
        dict(ts=2, Open=101, High=101, Low=91, Close=95, atr=5),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                          exit_after_bars=None, slippage_pct=None)

    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["entry_px"] == 100
    assert tr["qty"] == 100
    assert tr["exit_ts"] == 2, "stop must not trigger on i1; first working bar is i2"
    assert tr["exit_px"] == 92
    assert tr["pnl"] == -800

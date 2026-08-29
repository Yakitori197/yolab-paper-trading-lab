"""Hand-derived contracts for engine.py's batch #8 exit options (stall exit,
breakeven floor). Every expected value below is derived from the frozen
pre-registration rules in docs/BATCH_PLAN.md, not from running the engine --
do not adjust them to match engine output.

Common scenario constants: fee=0, tick=0 (slip=0), stop_mult=2, atr=1 at
entry unless stated, so R0 = 2 price units and qty = 100 on 10,000 cash at
entry price 100.
"""
import pandas as pd
import pytest

from engine import run


def make_df(rows):
    out = []
    for r in rows:
        out.append(dict(
            ts=r["ts"], Open=r["Open"], High=r["High"], Low=r["Low"], Close=r["Close"],
            atr=r["atr"], long_sig=r.get("long_sig", False), short_sig=r.get("short_sig", False),
            in_win=r.get("in_win", True)))
    return pd.DataFrame(out)


def flat_bar(ts, px, atr=1, **kw):
    return dict(ts=ts, Open=px, High=px, Low=px, Close=px, atr=atr, **kw)


def test_stall_waits_for_eligibility_then_exits_on_negative_close():
    # entry at bar0 close 100; bar4 closes at 99.5 (signed gain -0.5 < 0) but
    # holding is only 4 bars -- with stall_bars=6 the rule must NOT fire
    # before bar6. Bar6 closes 99.5 again -> stall exit at that close.
    # qty=100, pnl = 100*(99.5-100) = -50. Stop (98) is never touched.
    df = make_df([
        flat_bar(0, 100, long_sig=True),
        flat_bar(1, 100), flat_bar(2, 100), flat_bar(3, 100),
        flat_bar(4, 99.5),
        flat_bar(5, 100),
        flat_bar(6, 99.5),
        flat_bar(7, 100),
    ])
    metrics, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                          stall_bars=6)
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["exit_reason"] == "stall"
    assert tr["exit_ts"] == 6, "bar4 is below entry but not yet eligible (held 4 < 6)"
    assert tr["exit_px"] == 99.5
    assert tr["pnl"] == -50


def test_stall_gain_threshold_boundary():
    # stall_bars=2, stall_gain=0.5 -> threshold 0.5*R0 = 1.0 price units.
    # A: close 100.9 at bar2 (gain 0.9 < 1.0) -> stall exit, pnl +90.
    df_a = make_df([
        flat_bar(0, 100, long_sig=True),
        flat_bar(1, 100.9),
        flat_bar(2, 100.9),
    ])
    _m, trades_a = run(df_a, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                       stall_bars=2, stall_gain=0.5)
    tr = trades_a.iloc[0]
    assert tr["exit_reason"] == "stall"
    assert tr["exit_ts"] == 2
    assert tr["pnl"] == pytest.approx(90.0)

    # B: close 101.0 exactly at threshold (gain 1.0 >= 1.0) -> must NOT fire.
    df_b = make_df([
        flat_bar(0, 100, long_sig=True),
        flat_bar(1, 101.0),
        flat_bar(2, 101.0),
        flat_bar(3, 101.0),
    ])
    _m, trades_b = run(df_b, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                       stall_bars=2, stall_gain=0.5)
    assert len(trades_b) == 1
    assert pd.isna(trades_b.iloc[0]["exit_ts"]), "gain == threshold stays in (rule is strictly below)"


def test_stop_wins_the_bar_over_stall():
    # bar2: Low 97 pierces the working stop 98 AND the close 97.5 is below
    # entry with holding == stall_bars -- the intra-bar stop must take the
    # exit (reason "stop" at 98), not the close-based stall.
    df = make_df([
        flat_bar(0, 100, long_sig=True),
        flat_bar(1, 100),
        dict(ts=2, Open=100, High=100, Low=97, Close=97.5, atr=1),
    ])
    _m, trades = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                     stall_bars=2)
    tr = trades.iloc[0]
    assert tr["exit_reason"] == "stop"
    assert tr["exit_px"] == 98
    assert tr["pnl"] == -200


def test_breakeven_floor_binds_only_via_expanded_atr():
    # Entry bar0 close 100, atr=1 -> R0=2, stop0=98. Bar1 closes 102.5 with
    # atr grown to 3: plain ratchet candidate = 102.5-6 = 96.5 (stop stays
    # 98), but gain 2.5 >= 1.0*R0 arms the breakeven floor -> stop = 100.
    # Bar2 Low 99.5 hits 100 (would NOT hit 98): exit at 100, pnl = 0.
    rows = [
        flat_bar(0, 100, long_sig=True),
        dict(ts=1, Open=100, High=102.5, Low=100, Close=102.5, atr=3),
        dict(ts=2, Open=101, High=101, Low=99.5, Close=101, atr=3),
    ]
    _m, trades_be = run(make_df(rows), cash=10_000, fee=0.0, tick=0.0,
                        stop_mult=2.0, be_trigger=1.0)
    tr = trades_be.iloc[0]
    assert tr["exit_reason"] == "stop"
    assert tr["exit_px"] == 100
    assert tr["pnl"] == pytest.approx(0.0)

    # Control without be_trigger: stop stays 98, Low 99.5 never reaches it,
    # the trade is still open -- proving the floor (not the ratchet) exited.
    _m2, trades_ctl = run(make_df(rows), cash=10_000, fee=0.0, tick=0.0,
                          stop_mult=2.0)
    assert len(trades_ctl) == 1
    assert pd.isna(trades_ctl.iloc[0]["exit_ts"])


def test_stall_and_exit_after_bars_mutually_exclusive():
    df = make_df([flat_bar(0, 100, long_sig=True)])
    with pytest.raises(ValueError):
        run(df, cash=10_000, fee=0.0, tick=0.0, stall_bars=2, exit_after_bars=5)


def test_defaults_off_bit_identical_to_legacy():
    # Same stop-exit scenario run with no batch #8 kwargs vs. all three
    # explicitly None must produce identical trades and metrics.
    df = make_df([
        flat_bar(0, 100, long_sig=True),
        flat_bar(1, 100),
        dict(ts=2, Open=100, High=100, Low=97, Close=97.5, atr=1),
    ])
    m_legacy, t_legacy = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)
    m_none, t_none = run(df, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0,
                         stall_bars=None, stall_gain=None, be_trigger=None)
    pd.testing.assert_frame_equal(t_legacy, t_none)
    assert m_legacy == m_none

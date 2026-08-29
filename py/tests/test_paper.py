"""E1 tests for paper_loop.py / paper_store.py. Synthetic data only -- no
network access, no writes to the real data/market.db or data/paper.db (each
test uses its own temp sqlite file). Expected values are derived from how
the synthetic series is constructed, not from running the module first."""
import math
import sqlite3

import numpy as np
import pandas as pd
import pytest

import engine
import paper_loop as pl
import paper_store as ps
from strategy_squeeze import build_signals

STEP_MS = pl.STEP_MS


def _paper_con():
    """In-memory paper.db for tests -- bypasses paper_store.connect()'s
    pathlib handling, which is not meant for sqlite3's special ':memory:'
    filename (path.parent.mkdir() on a Path(':memory:') is unreliable on
    Windows)."""
    con = sqlite3.connect(":memory:")
    con.executescript(ps.SCHEMA)
    return con


def _synthetic_df():
    """110 bars of real oscillation (amplitude 3) so the 100-bar percentrank
    window has genuine history, then 150 bars of geometrically-decaying
    tight noise (a real squeeze), then a 5-unit breakout, then 30 bars of
    drift. Deterministically fires exactly one long_sig at the breakout bar
    (verified empirically before writing this test)."""
    n_wide, n_tight, n_after = 110, 150, 30
    n = n_wide + n_tight + n_after
    close = np.empty(n)
    for i in range(n_wide):
        close[i] = 100 + 3 * np.sin(i * 0.5)
    tight_base = close[n_wide - 1]
    for k in range(n_tight):
        i = n_wide + k
        amp = 0.5 * (0.97 ** k)
        close[i] = tight_base + amp * np.sin(k * 1.3)
    breakout_i = n_wide + n_tight
    close[breakout_i] = close[breakout_i - 1] + 5.0
    for k in range(1, n_after):
        i = breakout_i + k
        close[i] = close[breakout_i] + 0.1 * k * np.sin(k * 0.3)

    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1]
    high = np.maximum(openp, close) + 0.05
    low = np.minimum(openp, close) - 0.05
    ts = 1_700_000_000_000 + np.arange(n) * STEP_MS

    df = pd.DataFrame({"ts": ts, "Open": openp, "High": high, "Low": low, "Close": close,
                        "Volume": np.full(n, 1.0)})
    epoch = int(ts[breakout_i])
    return df, epoch


def test_idempotent_rerun_leaves_trades_and_signals_unchanged():
    df, epoch = _synthetic_df()
    con = _paper_con()

    pl.process_symbol(con, "SYN/TEST", df, epoch)
    trades_1 = ps.get_trades(con, "SYN/TEST")
    counts_1 = ps.table_counts(con)

    summary_2 = pl.process_symbol(con, "SYN/TEST", df, epoch)
    trades_2 = ps.get_trades(con, "SYN/TEST")
    counts_2 = ps.table_counts(con)

    assert trades_1 == trades_2
    assert counts_1 == counts_2
    assert summary_2["new_trades"] == 0
    assert summary_2["new_signal_bars"] == 0
    assert len(trades_1) >= 1  # the synthetic breakout must have actually produced a trade


def test_stored_trades_match_engine_run_exactly():
    df, epoch = _synthetic_df()
    sig_df = build_signals(df, epoch, int(df["ts"].max()))
    expected_metrics, expected_trades = engine.run(sig_df, cash=pl.CASH0, fee=pl.FEE, tick=pl.TICK)

    con = _paper_con()
    pl.process_symbol(con, "SYN/TEST", df, epoch)
    stored = ps.get_trades(con, "SYN/TEST")

    assert len(stored) == len(expected_trades)
    for stored_row, (_, exp_row) in zip(stored, expected_trades.iterrows()):
        assert stored_row["entry_ts"] == int(exp_row["entry_ts"])
        assert stored_row["direction"] == exp_row["direction"]
        assert stored_row["qty"] == pytest.approx(exp_row["qty"])
        assert stored_row["entry_px"] == pytest.approx(exp_row["entry_px"])
        assert stored_row["fees"] == pytest.approx(exp_row["fees"])
        exp_exit_ts = None if pd.isna(exp_row["exit_ts"]) else int(exp_row["exit_ts"])
        assert stored_row["exit_ts"] == exp_exit_ts
        if exp_exit_ts is not None:
            assert stored_row["exit_px"] == pytest.approx(exp_row["exit_px"])
            assert stored_row["pnl"] == pytest.approx(exp_row["pnl"])
            assert stored_row["reason"] == exp_row["exit_reason"]


def test_signal_detail_matches_build_signals_and_aborts_on_corruption():
    df, epoch = _synthetic_df()
    latest_ts = int(df["ts"].max())
    sig_df = build_signals(df, epoch, latest_ts)
    detail_df = pl.compute_signal_detail(df, epoch, latest_ts, pl.P)

    # normal case: no mismatch, must not raise
    pl.assert_signal_consistency("SYN/TEST", df, sig_df, detail_df)
    assert (sig_df["long_sig"].to_numpy() == detail_df["long_sig"].to_numpy()).all()
    assert (sig_df["short_sig"].to_numpy() == detail_df["short_sig"].to_numpy()).all()

    # corrupt exactly one bar's long_sig in the detail frame -> must abort
    corrupted = detail_df.copy()
    some_i = len(corrupted) // 2
    corrupted.loc[corrupted.index[some_i], "long_sig"] = not bool(corrupted["long_sig"].iloc[some_i])

    with pytest.raises(RuntimeError, match="ABORT"):
        pl.assert_signal_consistency("SYN/TEST", df, sig_df, corrupted)

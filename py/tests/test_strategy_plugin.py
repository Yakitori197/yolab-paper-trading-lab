"""Tests for the strategy plugin contract (strategies/__init__.py).

Two things are being proved here:
  1. a plugin that breaks the contract -- wrong shape, rewritten prices,
     non-deterministic, or peeking at future bars -- is rejected BEFORE
     anything reaches paper.db;
  2. the contract is not secretly shaped around the built-in squeeze rule:
     a completely different plugin (strategies/ema_cross.py) runs end to end
     through the same engine and the same storage path.
Synthetic data only, no network, no writes to the real databases.
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import engine
import paper_loop as pl
import paper_store as ps
import strategies
from strategy_squeeze import build_signals

FIXTURES = [Path(__file__).resolve().parent / "strategies_fixtures"]
STEP_MS = pl.STEP_MS


def _series(n=400, seed=7):
    """Trending series with real oscillation: EMA20/60 cross several times
    and the Bollinger bands both compress and expand."""
    rng = np.random.default_rng(seed)
    drift = np.cumsum(rng.normal(0.0, 0.6, n))
    wave = 6.0 * np.sin(np.arange(n) * 0.11)
    close = 100.0 + drift + wave
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1]
    high = np.maximum(openp, close) + 0.3
    low = np.minimum(openp, close) - 0.3
    ts = 1_700_000_000_000 + np.arange(n) * STEP_MS
    df = pd.DataFrame({"ts": ts, "Open": openp, "High": high, "Low": low,
                        "Close": close, "Volume": np.full(n, 1.0)})
    return df, int(ts[100])          # epoch leaves 100 warmup bars


def _load(name):
    return strategies.load(name, search_dirs=FIXTURES)


def _paper_con():
    con = sqlite3.connect(":memory:")
    con.executescript(ps.SCHEMA)
    return con


# ---- the self-proof ---------------------------------------------------------

def test_lookahead_rule_is_rejected():
    """A rule reading the NEXT bar's close changes its own past when new
    bars arrive -- the exact failure the truncation check exists for."""
    df, epoch = _series()
    mod = _load("peek_future")
    latest = int(df["ts"].max())

    # it builds fine in isolation: the frame looks perfectly reasonable
    frame = strategies.build_frame(mod, df, mod.PARAMS, epoch, latest)
    assert frame["long_sig"].any()

    with pytest.raises(strategies.StrategyError, match="未來資料"):
        strategies.check_replay_stability(mod, df, mod.PARAMS, epoch, latest)


def test_nondeterministic_rule_is_rejected():
    df, epoch = _series()
    mod = _load("unstable")
    with pytest.raises(strategies.StrategyError, match="不具決定性"):
        strategies.check_replay_stability(mod, df, mod.PARAMS, epoch, int(df["ts"].max()))


def test_builtin_and_example_plugins_pass_the_self_proof():
    df, epoch = _series()
    latest = int(df["ts"].max())
    for name in ("squeeze_breakout", "ema_cross"):
        mod = strategies.load(name)
        checked = strategies.check_replay_stability(mod, df, mod.PARAMS, epoch, latest)
        assert checked == len(strategies.DEFAULT_CUTS)


# ---- contract enforcement ---------------------------------------------------

@pytest.mark.parametrize("mode,pattern", [
    ("short_frame", "根 K 棒不符"),
    ("missing_stop", "缺少必要欄位"),
    ("mutates_price", "OHLCV 必須原封不動"),
    ("negative_stop", "恆為正數"),
    ("explodes", "執行失敗"),
    ("not_a_frame", "必須回傳 DataFrame"),
])
def test_contract_violations_are_rejected(mode, pattern):
    df, epoch = _series(n=120)
    mod = _load("contract_breakers")
    mod.MODE = mode
    with pytest.raises(strategies.StrategyError, match=pattern):
        strategies.build_frame(mod, df, mod.PARAMS, epoch, int(df["ts"].max()))


def test_plot_declaration_must_match_a_returned_column():
    df, epoch = _series(n=120)
    mod = _load("contract_breakers")
    mod.MODE = "plot_missing_column"
    mod.PLOT = dict(lines={"nope": "不存在的線"})
    try:
        with pytest.raises(strategies.StrategyError, match="沒有回傳這一欄"):
            strategies.build_frame(mod, df, mod.PARAMS, epoch, int(df["ts"].max()))
    finally:
        del mod.PLOT


def test_unknown_parameter_is_an_error_not_a_silent_noop():
    mod = strategies.load("squeeze_breakout")
    with pytest.raises(strategies.StrategyError, match="沒有參數"):
        strategies.resolve_params(mod, {"sqz_treshold": 15})      # typo
    assert strategies.resolve_params(mod, {"sqz_thresh": 15})["sqz_thresh"] == 15


def test_unknown_module_names_the_available_ones():
    with pytest.raises(strategies.StrategyError, match="squeeze_breakout"):
        strategies.load("no_such_rule")
    with pytest.raises(strategies.StrategyError, match="英數字"):
        strategies.load("../secrets")


# ---- the framework's own responsibilities ----------------------------------

def test_window_is_enforced_by_the_framework_not_the_plugin():
    """always_long wants to be long on every bar; nothing before the epoch
    may survive, however enthusiastic the plugin is."""
    df, epoch = _series()
    mod = _load("always_long")
    frame = strategies.build_frame(mod, df, mod.PARAMS, epoch, int(df["ts"].max()))
    before = frame["ts"] < epoch
    assert before.any()
    assert not frame.loc[before, "long_sig"].any()
    assert frame.loc[~before, "long_sig"].all()


def test_builtin_plugin_reproduces_the_legacy_signals_exactly():
    """strategy_squeeze.build_signals() is the frozen reference the engine
    tests were hand-derived against; the plugin path must not move it."""
    df, epoch = _series()
    latest = int(df["ts"].max())
    legacy = build_signals(df, epoch, latest)
    frame = strategies.build_frame(strategies.load("squeeze_breakout"), df, pl.P, epoch, latest)

    assert (legacy["long_sig"].to_numpy() == frame["long_sig"].to_numpy()).all()
    assert (legacy["short_sig"].to_numpy() == frame["short_sig"].to_numpy()).all()
    assert np.allclose(legacy["atr"].to_numpy(), frame["atr"].to_numpy(), equal_nan=True)
    # stop_dist is exactly what the engine used to compute internally
    assert np.allclose(frame["stop_dist"].to_numpy(),
                       pl.P["stop_mult"] * legacy["atr"].to_numpy(), equal_nan=True)


def test_engine_takes_stop_dist_and_ignores_stop_mult():
    """Same bars, same stop distance, stated the two different ways."""
    df, epoch = _series()
    latest = int(df["ts"].max())
    legacy = build_signals(df, epoch, latest)
    frame = strategies.build_frame(strategies.load("squeeze_breakout"), df, pl.P, epoch, latest)

    m_legacy, t_legacy = engine.run(legacy, cash=10_000, fee=0.0, tick=0.0, stop_mult=2.0)
    # stop_mult is deliberately absurd here: the frame's stop_dist must win
    m_plugin, t_plugin = engine.run(frame, cash=10_000, fee=0.0, tick=0.0, stop_mult=99.0)

    assert m_legacy["trades"] == m_plugin["trades"]
    assert m_legacy["net"] == pytest.approx(m_plugin["net"])
    assert list(t_legacy["exit_reason"]) == list(t_plugin["exit_reason"])


# ---- end to end through the real tick path ---------------------------------

def test_a_non_squeeze_plugin_runs_through_process_symbol(monkeypatch):
    """The proof that the contract is not squeeze-shaped: swap in the EMA
    example, run the actual tick path, and check what lands in paper.db."""
    df, epoch = _series()
    mod = strategies.load("ema_cross")
    monkeypatch.setattr(pl, "STRATEGY", mod)
    monkeypatch.setattr(pl, "STRATEGY_NAME", "ema_cross")
    monkeypatch.setattr(pl, "P", dict(mod.PARAMS))

    con = _paper_con()
    summary = pl.process_symbol(con, "SYN/TEST", df, epoch)
    trades = ps.get_trades(con, "SYN/TEST")
    assert len(trades) >= 1
    assert summary["new_trades"] == len(trades)

    rows = con.execute(
        "SELECT upper, lower, width_rank, sqz_ok, reason, stop_disp, position "
        "FROM paper_signals WHERE symbol=? ORDER BY ts", ("SYN/TEST",)).fetchall()
    assert rows
    # the squeeze-only columns are NULL rather than borrowed from another rule
    assert all(r[0] is None and r[1] is None and r[2] is None and r[3] is None for r in rows)
    # the rule still explains itself, in its own words
    assert any(r[4] == "快線尚未穿越慢線" for r in rows)
    # and a held position still gets a displayed stop, from stop_dist
    assert any(r[6] is not None and r[5] is not None for r in rows)


def test_rerunning_the_ema_plugin_is_idempotent(monkeypatch):
    df, epoch = _series()
    mod = strategies.load("ema_cross")
    monkeypatch.setattr(pl, "STRATEGY", mod)
    monkeypatch.setattr(pl, "STRATEGY_NAME", "ema_cross")
    monkeypatch.setattr(pl, "P", dict(mod.PARAMS))

    con = _paper_con()
    pl.process_symbol(con, "SYN/TEST", df, epoch)
    first = ps.get_trades(con, "SYN/TEST")
    second_summary = pl.process_symbol(con, "SYN/TEST", df, epoch)

    assert ps.get_trades(con, "SYN/TEST") == first
    assert second_summary["new_trades"] == 0
    assert second_summary["new_signal_bars"] == 0


def test_a_cheating_plugin_never_reaches_the_database(monkeypatch):
    df, epoch = _series()
    mod = _load("peek_future")
    monkeypatch.setattr(pl, "STRATEGY", mod)
    monkeypatch.setattr(pl, "STRATEGY_NAME", "peek_future")
    monkeypatch.setattr(pl, "P", dict(mod.PARAMS))

    con = _paper_con()
    with pytest.raises(strategies.StrategyError, match="ABORT"):
        pl.process_symbol(con, "SYN/TEST", df, epoch)
    assert ps.count_trades(con, "SYN/TEST") == 0
    assert ps.count_signals(con, "SYN/TEST") == 0

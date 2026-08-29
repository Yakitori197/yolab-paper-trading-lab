"""Tests for the 策略表現 statistics (py/perf.py + dashboard's read-only
derivation). Every expected value below is derived by hand from the rows the
test inserts, never by running the code first. Synthetic in-memory database
only -- the real data/paper.db is never opened.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import dashboard as dash
import export_trades as et
import paper_store as ps
import perf


def _con():
    con = sqlite3.connect(":memory:")
    con.executescript(ps.SCHEMA)
    return con


def _add_trade(con, symbol, entry_ts, pnl, fees=1.0, funding=0.5, closed=True):
    con.execute(
        "INSERT INTO paper_trades (symbol, entry_ts, exit_ts, direction, qty, entry_px, "
        "exit_px, fees, funding, pnl, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, entry_ts, entry_ts + 1000 if closed else None, "long", 1.0, 100.0,
         101.0 if closed else None, fees, funding, pnl if closed else None,
         "stop" if closed else None))


def _add_equity(con, symbol, rows):
    con.executemany("INSERT INTO paper_equity (symbol, ts, equity) VALUES (?,?,?)",
                    [(symbol, ts, eq) for ts, eq in rows])


# ---- the pure functions ----------------------------------------------------

def test_win_rate_counts_breakeven_as_a_loss():
    """pnl == 0 is not a win. A rule that scratches out flat every time must
    not be able to show a 100% win rate."""
    stats = perf.compute_stats([{"pnl": 100.0}, {"pnl": 0.0}])
    assert stats["n"] == 2
    assert stats["n_win"] == 1 and stats["n_loss"] == 1
    assert stats["win_rate"] == 50.0


def test_stats_are_hand_derivable():
    closed = [{"pnl": 100.0, "fees": 2.0, "funding": 0.5},
              {"pnl": -50.0, "fees": 2.0, "funding": -0.5},
              {"pnl": 25.0, "fees": 2.0, "funding": 0.0},
              {"pnl": 0.0, "fees": 2.0, "funding": 0.0}]
    s = perf.compute_stats(closed)
    assert s["n"] == 4 and s["n_win"] == 2 and s["n_loss"] == 2
    assert s["gross_profit"] == 125.0      # 100 + 25
    assert s["gross_loss"] == 50.0         # -((-50) + 0)
    assert s["net"] == 75.0
    assert s["win_rate"] == 50.0
    assert s["avg_win"] == 62.5            # 125 / 2
    assert s["avg_loss"] == 25.0           # 50 / 2
    assert s["fees_total"] == 8.0
    assert s["funding_total"] == 0.0       # +0.5 - 0.5
    assert perf.profit_factor(s) == 2.5    # 125 / 50
    assert perf.payoff_ratio(s) == 2.5     # 62.5 / 25


def test_undefined_ratios_are_none_not_infinity():
    """No losing trade means the ratios have no value -- not a huge one."""
    s = perf.compute_stats([{"pnl": 10.0}, {"pnl": 5.0}])
    assert perf.profit_factor(s) is None
    assert perf.payoff_ratio(s) is None
    empty = perf.compute_stats([])
    assert empty["n"] == 0 and empty["win_rate"] == 0.0
    assert perf.profit_factor(empty) is None and perf.payoff_ratio(empty) is None


def test_drawdown_is_seeded_at_starting_capital():
    """A drop on the very first bar still counts, even though no peak was
    ever recorded above the starting balance."""
    rows = [(1000, 9000.0), (2000, 9500.0)]
    assert perf.max_drawdown_pct(rows, 10_000.0) == pytest.approx(-10.0)


def test_export_and_dashboard_share_one_definition():
    """The workbook and the dashboard must not be able to drift apart."""
    assert et.compute_stats is perf.compute_stats
    assert et.max_drawdown_pct is perf.max_drawdown_pct


# ---- the dashboard's derivation --------------------------------------------

def test_performance_block_for_one_symbol(monkeypatch):
    monkeypatch.setattr(dash, "CASH0", 10_000.0)
    con = _con()
    for i, pnl in enumerate([100.0, -50.0, 25.0, 0.0]):
        _add_trade(con, "AAA/USDT", 1000 + i, pnl, fees=2.0, funding=0.0)
    _add_trade(con, "AAA/USDT", 9000, None, closed=False)      # open: must be ignored
    _add_equity(con, "AAA/USDT", [(1000, 10_000.0), (2000, 9_500.0),
                                  (3000, 10_200.0), (4000, 10_075.0)])

    p = dash._performance(con, "AAA/USDT")
    assert p["trades"] == 4                       # the open trade is not a result yet
    assert p["wins"] == 2 and p["losses"] == 2
    assert p["win_rate"] == 50.0
    assert p["net"] == 75.0
    assert p["net_pct"] == pytest.approx(0.75)    # 75 / 10000
    assert p["payoff"] == pytest.approx(2.5)
    assert p["pf"] == pytest.approx(2.5)
    assert p["fees_total"] == 8.0
    # peak 10,000 -> 9,500 is -5.00%; the later 10,200 -> 10,075 is only -1.23%
    assert p["max_dd_pct"] == pytest.approx(-5.0)


def test_pooled_performance_forward_fills_each_account(monkeypatch):
    """Symbol B has no row at ts=1000; it must contribute its starting cash
    there rather than vanishing from the total."""
    monkeypatch.setattr(dash, "CASH0", 10_000.0)
    con = _con()
    _add_trade(con, "AAA/USDT", 1000, 100.0, fees=1.0, funding=0.0)
    _add_trade(con, "BBB/USDT", 1000, -40.0, fees=1.0, funding=0.0)
    _add_equity(con, "AAA/USDT", [(1000, 10_000.0), (3000, 11_000.0)])
    _add_equity(con, "BBB/USDT", [(2000, 9_000.0), (3000, 9_500.0)])

    p = dash._performance_pooled(con, ["AAA/USDT", "BBB/USDT"])
    assert p["trades"] == 2 and p["wins"] == 1 and p["losses"] == 1
    assert p["win_rate"] == 50.0
    assert p["net"] == 60.0
    assert p["cash0"] == 20_000.0
    assert p["net_pct"] == pytest.approx(0.3)     # 60 / 20000
    # pooled curve: 20,000 -> 19,000 (10,000 held + 9,000) -> 20,500
    assert p["max_dd_pct"] == pytest.approx(-5.0)


def test_empty_account_reports_nothing_rather_than_zero(monkeypatch):
    monkeypatch.setattr(dash, "CASH0", 10_000.0)
    con = _con()
    p = dash._performance(con, "AAA/USDT")
    assert p["trades"] == 0
    assert p["win_rate"] is None                  # not 0% -- there is no rate yet
    assert p["pf"] is None and p["payoff"] is None
    assert p["max_dd_pct"] == 0.0

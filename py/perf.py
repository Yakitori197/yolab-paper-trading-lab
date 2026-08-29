"""Performance statistics over CLOSED trades -- one definition, two readers.

Both the dashboard's 策略表現 card and tools/export_trades.py's xlsx overview
import from here, so the two can never quote different win rates for the same
account. Pure functions: no I/O, no database, no engine state.

Conventions (unchanged from the E7/E8 export, which these were moved from):
  - a trade counts as a win only when pnl > 0; pnl == 0 counts as a loss,
    so break-even exits never inflate the win rate
  - pnl is already net of fees and funding (engine.py computes it that way),
    so 總損益 here is what actually hit the account
  - drawdown seeds its peak at the account's starting capital, matching
    engine.py's own max_dd convention, so a drop from the very first bar is
    still captured
"""


def compute_stats(closed_trades):
    """closed_trades: list of dicts with a 'pnl' key (fees/funding keys
    optional -- E13 cost totals default to 0 when absent, e.g. in hand-built
    test fixtures). Win/loss split purely by sign (pnl>0 wins, pnl<=0
    losses). Pure function -- no I/O."""
    n = len(closed_trades)
    wins = [t for t in closed_trades if t["pnl"] > 0]
    losses = [t for t in closed_trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    net = sum(t["pnl"] for t in closed_trades)
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    fees_total = sum(t.get("fees") or 0.0 for t in closed_trades)
    funding_total = sum(t.get("funding") or 0.0 for t in closed_trades)
    return dict(n=n, n_win=len(wins), n_loss=len(losses), win_rate=win_rate,
                avg_win=avg_win, avg_loss=avg_loss, gross_profit=gross_profit,
                gross_loss=gross_loss, net=net, fees_total=fees_total,
                funding_total=funding_total)


def max_drawdown_pct(equity_rows, seed_peak):
    """equity_rows: ascending [(ts, equity), ...]. Peak seeded at seed_peak
    (the account's starting capital) so a drawdown from the very first bar
    is still captured, matching engine.py's own max_dd convention."""
    peak = seed_peak
    max_dd = 0.0
    for _ts, eq in equity_rows:
        if eq > peak:
            peak = eq
        dd = (eq / peak - 1.0) * 100.0 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return max_dd


def profit_factor(stats):
    """總獲利 / 總虧損. None when undefined (no losing trade, or no trades) --
    the caller decides how to render that; it must never be shown as a
    number, because "∞" and "very good" are not the same claim."""
    if stats["n"] == 0 or stats["gross_loss"] <= 0:
        return None
    return stats["gross_profit"] / stats["gross_loss"]


def payoff_ratio(stats):
    """平均獲利 / 平均虧損. None when undefined, same reasoning as above."""
    if stats["n_win"] == 0 or stats["n_loss"] == 0 or stats["avg_loss"] <= 0:
        return None
    return stats["avg_win"] / stats["avg_loss"]

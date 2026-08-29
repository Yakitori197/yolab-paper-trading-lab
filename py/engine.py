"""CAL-002: minimal bar-by-bar backtest engine, independent of backtesting.py.

Consumes the signal DataFrame produced by any strategy plugin via
strategies.build_frame() -- or the legacy strategy_squeeze.build_signals()
frame -- (columns: ts, Open, High, Low, Close, long_sig, short_sig, in_win,
plus either stop_dist or atr) and replicates the Pine timing specified there:
  - stop-loss orders are only "working" starting the second bar after entry
    (Pine submits strategy.exit on the bar after the fill, never the entry bar)
  - the trailing stop only ever tightens (ratchet), computed from the bar's
    Close +- stop_mult*ATR
  - market entries / reversals / window-end closes fill at the signal bar's
    Close (plus slippage), stop exits fill at the stop price unless the open
    gapped through it, in which case the open is used instead
  - every fill fee is 0.0005 * notional, charged on both legs of a trade

Slippage/fee direction rule (uniform across all fill sites): opening a long
or covering a short is a BUY -> fills at reference + slip; closing a long or
opening a short is a SELL -> fills at reference - slip.

Funding settlement (E13, perpetual-swap cost realism; run(funding=...)):
funding is a {bar-open ts(ms): rate} dict, None (default) = disabled with
bit-identical legacy behavior. Hand-derivable settlement rule:
  - at bar i's OPEN instant: if a rate exists exactly at ts[i] and the
    position was entered at bar <= i-2 (its fill instant -- the entry bar's
    close -- lies strictly before ts[i]), the position pays
    dir * qty * Open[i] * rate (long pays positive rates, short receives),
    deducted from cash BEFORE any exit check on that bar, so a stop firing
    later that same bar still pays that instant's funding.
  - a position entered on bar i-1 fills exactly AT ts[i] (in reality the
    market order executes moments after the boundary) -> pays nothing there.
  - close-fills landing exactly on a funding instant (reversal/window_end/
    time_exit fill at a bar's close) do NOT charge that instant's settlement
    -- documented simplification, at most one ~bp-level settlement per such
    trade; stop exits are intra-bar and unaffected.
Accumulated funding is a per-trade `funding` column (0.0 when disabled) and
is part of net pnl (pnl = gross - fees - funding); metrics gain
funding_total / funding_events.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from strategy_squeeze import P as SQUEEZE_P

# "atr" is the legacy way to state the stop distance (scaled by stop_mult);
# plugin frames carry "stop_dist" instead and either one satisfies run().
REQUIRED_COLS = ("ts", "Open", "High", "Low", "Close", "long_sig", "short_sig", "atr", "in_win")


def _isnan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def run(df, cash=10_000.0, fee=0.0005, tick=0.1, stop_mult=None,
        exit_after_bars=None, slippage_pct=None, funding=None,
        stall_bars=None, stall_gain=None, be_trigger=None):
    """Bar-by-bar simulation. Returns (metrics: dict, trades: pd.DataFrame).

    exit_after_bars (default None): when set, a position is force-closed at
    market on the close of the Nth bar after entry, and the trailing stop is
    disabled entirely (mutually exclusive with it) -- step 1 becomes a
    time-based exit check instead of a price-based stop check, and the step
    2b ratchet update is skipped. Default None leaves existing behavior
    (trailing stop only) completely unchanged.

    slippage_pct (default None): when set, replaces the tick-based slip with
    a percentage of the fill reference price (buy fills at ref*(1+pct), sell
    fills at ref*(1-pct)) at every fill site. Default None leaves the
    existing tick-based `ref +/- slip` behavior completely unchanged.

    funding (default None): {bar-open ts(ms): rate} dict enabling perpetual
    funding settlement per the module docstring's rule. Default None keeps
    legacy behavior bit-identical (funding column all 0.0, no cashflows).

    Batch #8 exit options (both default None = legacy bit-identical):
      stall_bars/stall_gain: from stall_bars bars after entry, any bar whose
        CLOSE-based signed gain is below stall_gain*R0 (R0 = the entry bar's
        stop_mult*ATR; stall_gain None means 0.0) exits at that bar's close
        (reason "stall"). Runs AFTER the intra-bar stop check (stop wins the
        bar) and coexists with the trailing stop; mutually exclusive with
        exit_after_bars (ValueError when both are set).
      be_trigger: once a bar's CLOSE-based signed gain reaches
        be_trigger*R0, the stop is floored at the entry price (ratchet takes
        the tighter of the two, effective from the next bar like every
        ratchet update). Only binds when ATR expanded after entry --
        otherwise the ordinary ratchet is already at/above entry by the time
        the trigger is reached.
    """
    if stop_mult is None:
        stop_mult = SQUEEZE_P["stop_mult"]
    if stall_bars is not None and exit_after_bars is not None:
        raise ValueError("stall_bars and exit_after_bars are mutually exclusive")
    stall_gain = 0.0 if stall_gain is None else float(stall_gain)
    slip = 2 * tick
    cash0 = float(cash)

    def buy_fill(ref):
        return ref * (1.0 + slippage_pct) if slippage_pct is not None else ref + slip

    def sell_fill(ref):
        return ref * (1.0 - slippage_pct) if slippage_pct is not None else ref - slip

    ts = df["ts"].to_numpy()
    o = df["Open"].to_numpy(dtype=float)
    h = df["High"].to_numpy(dtype=float)
    l = df["Low"].to_numpy(dtype=float)
    c = df["Close"].to_numpy(dtype=float)
    long_sig = df["long_sig"].to_numpy(dtype=bool)
    short_sig = df["short_sig"].to_numpy(dtype=bool)
    in_win = df["in_win"].to_numpy(dtype=bool)
    # Per-bar stop distance. A strategy plugin states it outright
    # (stop_dist, price units, already multiplied); the legacy frames carry
    # ATR instead and the distance is stop_mult*ATR, which is what
    # stop_dist becomes for the built-in rule -- identical values, so every
    # hand-derived expectation in the tests holds verbatim either way.
    if "stop_dist" in df.columns:
        stop_dist = df["stop_dist"].to_numpy(dtype=float)
    else:
        stop_dist = stop_mult * df["atr"].to_numpy(dtype=float)
    n = len(df)

    trades = []       # list of dicts; open trades have exit_ts=None until closed
    position = None   # dict or None
    cash_now = cash0
    equity_curve = []
    funding_events = 0

    def _open(direction, price, i):
        nonlocal cash_now, position
        qty = cash_now / price
        entry_fee = qty * price * fee
        cash_now -= entry_fee
        stop0 = (price - stop_dist[i]) if direction == "long" else (price + stop_dist[i])
        trade = dict(entry_ts=ts[i], exit_ts=None, direction=direction, qty=qty,
                     entry_px=price, exit_px=None, fees=entry_fee, funding=0.0,
                     pnl=None, exit_reason=None)
        trades.append(trade)
        position = dict(direction=direction, qty=qty, entry_px=price, entry_fee=entry_fee,
                         stop_price=stop0, stop_active=False, trade=trade, entry_bar_i=i,
                         r0=stop_dist[i], be_armed=False)

    def _close(price, i, reason):
        nonlocal cash_now, position
        d = 1.0 if position["direction"] == "long" else -1.0
        qty = position["qty"]
        exit_fee = qty * price * fee
        pnl_gross = d * qty * (price - position["entry_px"])
        tr = position["trade"]
        # funding accumulated at settlement instants is part of net pnl; the
        # cash impact already happened at each settlement, so the cash update
        # below stays gross-of-funding.
        pnl_net = pnl_gross - position["entry_fee"] - exit_fee - tr["funding"]
        cash_now = cash_now - exit_fee + pnl_gross
        tr["exit_ts"] = ts[i]
        tr["exit_px"] = price
        tr["fees"] = position["entry_fee"] + exit_fee
        tr["pnl"] = pnl_net
        tr["exit_reason"] = reason
        position = None

    for i in range(n):
        sd_i = stop_dist[i]
        sd_ok = not _isnan(sd_i)   # no stop distance yet (indicator warmup) -> no entries

        # 0. funding settlement at this bar's open instant (module docstring
        # rule): needs a supplied rate exactly at ts[i] and a position whose
        # fill instant (its entry bar's close) lies strictly before ts[i],
        # i.e. entered at bar <= i-2. Runs before every exit check so a stop
        # firing later this bar still pays this instant's funding.
        if funding is not None and position is not None \
                and (i - position["entry_bar_i"]) >= 2:
            rate = funding.get(int(ts[i]))
            if rate is not None:
                fdir = 1.0 if position["direction"] == "long" else -1.0
                amt = fdir * position["qty"] * o[i] * rate
                cash_now -= amt
                position["trade"]["funding"] += amt
                funding_events += 1

        # 1. exit check: price-based stop (default) XOR time-based exit
        # (exit_after_bars) -- the two are mutually exclusive by construction.
        if exit_after_bars is None:
            # stop-loss check (only once the stop is "working", see _close/2b)
            if position is not None and position["stop_active"]:
                stop = position["stop_price"]
                if position["direction"] == "long":
                    if l[i] <= stop:
                        ref = min(o[i], stop)
                        _close(sell_fill(ref), i, "stop")
                else:
                    if h[i] >= stop:
                        ref = max(o[i], stop)
                        _close(buy_fill(ref), i, "stop")
        else:
            if position is not None and (i - position["entry_bar_i"]) >= exit_after_bars:
                if position["direction"] == "long":
                    _close(sell_fill(c[i]), i, "time_exit")
                else:
                    _close(buy_fill(c[i]), i, "time_exit")

        # 1b. stall exit (batch #8 V1/V2): close-based, after the intra-bar
        # stop check so the stop wins any bar where both would fire.
        if stall_bars is not None and position is not None \
                and (i - position["entry_bar_i"]) >= stall_bars:
            d = 1.0 if position["direction"] == "long" else -1.0
            if d * (c[i] - position["entry_px"]) < stall_gain * position["r0"]:
                if position["direction"] == "long":
                    _close(sell_fill(c[i]), i, "stall")
                else:
                    _close(buy_fill(c[i]), i, "stall")

        # 2a. window-end forced close (Close-based), then skip 2b/2c this bar
        if not in_win[i]:
            if position is not None:
                if position["direction"] == "long":
                    _close(sell_fill(c[i]), i, "window_end")
                else:
                    _close(buy_fill(c[i]), i, "window_end")
            equity_curve.append(cash_now)
            continue

        # 2b. trailing ratchet - only for a position that already existed
        # going into this bar (the entry bar itself never reaches here with
        # a position, since entries happen below in 2c). Disabled entirely
        # when exit_after_bars is set (mutually exclusive with the stop).
        if exit_after_bars is None and position is not None and sd_ok:
            if position["direction"] == "long":
                candidate = c[i] - sd_i
                position["stop_price"] = max(position["stop_price"], candidate)
            else:
                candidate = c[i] + sd_i
                position["stop_price"] = min(position["stop_price"], candidate)
            # batch #8 V3 breakeven floor: armed once a close's signed gain
            # reaches be_trigger*R0; from then on the stop never sits on the
            # losing side of the entry price. Same next-bar effectiveness as
            # every other ratchet update.
            if be_trigger is not None:
                d = 1.0 if position["direction"] == "long" else -1.0
                if not position["be_armed"] and \
                        d * (c[i] - position["entry_px"]) >= be_trigger * position["r0"]:
                    position["be_armed"] = True
                if position["be_armed"]:
                    if position["direction"] == "long":
                        position["stop_price"] = max(position["stop_price"], position["entry_px"])
                    else:
                        position["stop_price"] = min(position["stop_price"], position["entry_px"])
            position["stop_active"] = True  # working from bar i+1 onward

        # 2c. entry / reversal
        if sd_ok:
            if long_sig[i] and (position is None or position["direction"] != "long"):
                if position is not None and position["direction"] == "short":
                    _close(buy_fill(c[i]), i, "reversal")
                _open("long", buy_fill(c[i]), i)
            elif short_sig[i] and (position is None or position["direction"] != "short"):
                if position is not None and position["direction"] == "long":
                    _close(sell_fill(c[i]), i, "reversal")
                _open("short", sell_fill(c[i]), i)

        # mark-to-market equity for this bar (informational, drives max_dd)
        if position is None:
            equity_curve.append(cash_now)
        else:
            d = 1.0 if position["direction"] == "long" else -1.0
            unreal = d * position["qty"] * (c[i] - position["entry_px"])
            equity_curve.append(cash_now + unreal)

    trades_df = pd.DataFrame(trades, columns=[
        "entry_ts", "exit_ts", "direction", "qty", "entry_px", "exit_px",
        "fees", "funding", "pnl", "exit_reason"])

    if not trades_df.empty:
        closed = trades_df[trades_df["exit_ts"].notna()]
    else:
        closed = trades_df
    n_trades = len(closed)

    if n_trades:
        wins = closed[closed["pnl"] > 0]
        losses = closed[closed["pnl"] <= 0]
        gross_profit = float(wins["pnl"].sum()) if len(wins) else 0.0
        gross_loss = float(-losses["pnl"].sum()) if len(losses) else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        net = float(closed["pnl"].sum())
        win_rate = len(wins) / n_trades * 100.0
        avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
        avg_loss = float(-losses["pnl"].mean()) if len(losses) else 0.0
    else:
        gross_profit = gross_loss = net = win_rate = avg_win = avg_loss = 0.0
        pf = float("nan")

    peak = cash0
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq / peak - 1.0) * 100.0 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    metrics = dict(
        trades=n_trades,
        wins=int((closed["pnl"] > 0).sum()) if n_trades else 0,
        losses=int((closed["pnl"] <= 0).sum()) if n_trades else 0,
        funding_total=float(trades_df["funding"].sum()) if not trades_df.empty else 0.0,
        funding_events=funding_events,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        pf=pf,
        net=net,
        net_pct=net / cash0 * 100.0,
        max_dd_pct=max_dd,
        avg_win=avg_win,
        avg_loss=avg_loss,
    )
    return metrics, trades_df

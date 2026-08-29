"""End-to-end sanity on synthetic data: wide-vol phase -> tight squeeze ->
upward breakout -> decline that must hit the trailing stop; plus window force-close."""
import numpy as np
import pandas as pd
from backtesting import Backtest

from strategy_squeeze import SqueezeBreakout, build_signals

STEP = 4 * 3600 * 1000


def make_df():
    closes = []
    # phase A: 70 bars of WIDE oscillation around 100 (width high)
    for i in range(70):
        closes.append(100.0 + (8.0 if i % 2 == 0 else -8.0))
    # phase B: 60 bars of CONTRACTING oscillation (amplitude decays 2.0 -> 0.1,
    # so each new width is strictly smaller and percentrank falls below 20;
    # a constant amplitude would tie all widths and rank would stall ~ mid-range)
    for i in range(60):
        amp = 2.0 - 1.9 * i / 59
        closes.append(100.0 + (amp if i % 2 == 0 else -amp))
    # phase C: breakout + uptrend, 12 bars strongly up
    for i in range(12):
        closes.append(104.0 + 3.0 * i)
    # phase D: decline, 12 bars down (must pierce the ratcheted trail stop)
    top = closes[-1]
    for i in range(12):
        closes.append(top - 4.0 * (i + 1))
    # phase E: flat tail
    closes += [closes[-1]] * 10
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 0.5
    lo = np.minimum(o, c) - 0.5
    ts = np.arange(len(c)) * STEP + 1_700_000_000_000
    df = pd.DataFrame({"ts": ts, "Open": o, "High": h, "Low": lo,
                       "Close": c, "Volume": 1.0})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def run(df, start_ms, end_ms):
    sig = build_signals(df, start_ms, end_ms)
    bt = Backtest(sig, SqueezeBreakout, cash=10_000, commission=0.0,
                  trade_on_close=True, exclusive_orders=True)
    stats = bt.run()
    return sig, stats


def test_breakout_long_then_trail_stop():
    df = make_df()
    sig, stats = run(df, int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]))
    assert sig["long_sig"].sum() >= 1, "expected at least one long signal"
    tr = stats["_trades"]
    assert len(tr) >= 1
    first = tr.iloc[0]
    assert first["Size"] > 0, "first trade should be long"
    sig_i = int(np.where(sig["long_sig"].to_numpy())[0][0])
    assert abs(first["EntryPrice"] - float(df["Close"].iloc[sig_i])) < 1e-6, \
        "entry must fill at the signal bar's close (process_orders_on_close)"
    assert first["ExitBar"] > first["EntryBar"], "trail stop must exit later"
    assert first["PnL"] > 0, "up-then-down path should lock in gains via ratchet"


def test_window_end_force_close():
    df = make_df()
    sig_full = build_signals(df, int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]))
    sig_i = int(np.where(sig_full["long_sig"].to_numpy())[0][0])
    end_i = sig_i + 3  # close the window 3 bars after entry, while still in trend
    sig, stats = run(df, int(df["ts"].iloc[0]), int(df["ts"].iloc[end_i]))
    tr = stats["_trades"]
    assert len(tr) >= 1
    assert int(tr.iloc[-1]["ExitBar"]) == end_i + 1, \
        "position must be closed on the first bar outside the window"

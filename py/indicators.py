"""Pine-equivalent indicator math (pure pandas/numpy). Formulas frozen to match
confluence_monitor.pine / squeeze_breakout_strategy.pine exactly."""
import numpy as np
import pandas as pd


def sma(s, n):
    return s.rolling(n).mean()


def stdev_pop(s, n):
    """Pine ta.stdev default is biased (population, ddof=0)."""
    return s.rolling(n).std(ddof=0)


def bollinger(close, n=20, mult=2.0):
    basis = sma(close, n)
    dev = mult * stdev_pop(close, n)
    return basis, basis + dev, basis - dev


def bb_width_pct(basis, upper, lower):
    return (upper - lower) / basis * 100.0


def percentrank_prev(s, n=100):
    """Pine ta.percentrank: percent of the previous n values <= current value."""
    arr = s.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(n, len(arr)):
        w = arr[i - n:i]
        if np.isnan(arr[i]) or np.isnan(w).any():
            continue
        out[i] = (w <= arr[i]).sum() / n * 100.0
    return pd.Series(out, index=s.index)


def atr_rma(high, low, close, n=14):
    """Pine ta.atr = RMA (Wilder smoothing) of true range."""
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def crossover(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))

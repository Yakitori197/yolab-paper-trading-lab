"""Squeeze Breakout Follow v1 -- the built-in default rule.

Bollinger squeeze then breakout: when the band width's percentile rank has
been in the bottom `sqz_thresh`% at some point in the last `sqz_win` bars,
a close crossing above the upper band goes long and a close crossing below
the lower band goes short. The stop sits `stop_mult` ATRs away.

Formulas are frozen to the BT-006 defaults and are Pine-equivalent
(indicators.py mirrors ta.stdev's population denominator, ta.percentrank's
"previous n values" window and ta.atr's Wilder smoothing). This file is the
reference implementation of the plugin contract -- copy it as a starting
point for your own rule; see docs/STRATEGY_API.md.

Not an edge. It is a placeholder whose only job is to exercise the machinery
honestly, and its simulated P&L argues nothing about deployment.
"""
import numpy as np
import pandas as pd

import indicators as ind

NAME = "Squeeze Breakout Follow v1"

PARAMS = dict(bb_len=20, bb_mult=2.0, rank_len=100, sqz_thresh=20.0, sqz_win=5,
              atr_len=14, stop_mult=2.0)

PLOT = dict(bands={"upper": "布林上軌", "lower": "布林下軌"})

WATCH = dict(label="距離上/下軌", text="watch_text")


def build(df, params):
    """df: ts / Open / High / Low / Close / Volume, ascending closed bars.
    Returns the same frame plus the signal columns. The paper-epoch window is
    NOT applied here -- the framework ANDs it in (see strategies/__init__)."""
    close = df["Close"]
    basis, upper, lower = ind.bollinger(close, params["bb_len"], params["bb_mult"])
    width = ind.bb_width_pct(basis, upper, lower)
    rank = ind.percentrank_prev(width, params["rank_len"])
    # the squeeze gate is shifted one bar: today's breakout is judged against
    # the compression that had already happened by yesterday's close
    sqz_ok = rank.rolling(params["sqz_win"]).min().shift(1) < params["sqz_thresh"]
    cross_up = ind.crossover(close, upper)
    cross_dn = ind.crossunder(close, lower)
    atr = ind.atr_rma(df["High"], df["Low"], close, params["atr_len"])

    out = df.copy()
    out["upper"] = upper
    out["lower"] = lower
    out["width_rank"] = rank
    out["sqz_ok"] = sqz_ok
    out["cross_up"] = cross_up
    out["cross_dn"] = cross_dn
    out["long_sig"] = cross_up & sqz_ok
    out["short_sig"] = cross_dn & sqz_ok
    out["atr"] = atr
    out["stop_dist"] = params["stop_mult"] * atr
    out["reason"] = _reasons(sqz_ok, cross_up, cross_dn, params["sqz_thresh"])
    out["watch_text"] = _watch_text(close, upper, lower)
    return out


def _reasons(sqz_ok, cross_up, cross_dn, thresh):
    """Why this bar produced no entry signal, in the rule's own words. None
    on bars where both conditions held (the framework then reports whether
    the position was already open in that direction)."""
    no_squeeze = f"寬度排名未低於 {thresh:g}"
    out = np.where(~sqz_ok.to_numpy(dtype=bool), no_squeeze,
                   np.where(~(cross_up.to_numpy(dtype=bool) | cross_dn.to_numpy(dtype=bool)),
                            "未突破上/下軌", None))
    return pd.Series(out, index=sqz_ok.index, dtype=object)


def _watch_text(close, upper, lower):
    """Per-bar distance to each band, for the entry-watch panel."""
    up_pct = (upper - close) / close * 100.0
    dn_pct = (close - lower) / close * 100.0
    return pd.Series(
        [None if (u != u or d != d) else f"距上軌 {u:+.2f}% / 距下軌 {d:+.2f}%"
         for u, d in zip(up_pct.to_numpy(dtype=float), dn_pct.to_numpy(dtype=float))],
        index=close.index, dtype=object)

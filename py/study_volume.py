"""Batch #8 V4 stage-1 measurement helpers: does breakout-bar relative
volume separate true from false breakouts?

Pure functions only (no I/O, no network) so every piece is contract-testable;
py/run_batch8.py wires them to market.db and prints the judged report.

Frozen definitions (BATCH_PLAN batch #8 pre-registration):
- event: close crossing the BB(20, 2sigma) upper band (dir=+1) or lower band
  (dir=-1) -- NO squeeze gate (larger n; the squeeze-gated subset is reported
  separately as a consistency reference)
- relvol at bar i = Volume[i] / mean(Volume[i-20 .. i-1]) -- the baseline
  window excludes the event bar itself, so a breakout spike cannot inflate
  its own denominator; no lookahead (entries fill at that same bar's close)
- signed forward return sr = dir * (Close[i+H]/Close[i] - 1), H=12 bars;
  sr > 0 means price kept going in the breakout direction
- independent sample: events closer than H bars to the last KEPT event are
  dropped (sequential thinning), Welch t on the thinned sets
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import indicators as ind

BB_LEN = 20
BB_MULT = 2.0
VOL_LEN = 20
HORIZON = 12


def rel_volume(volume):
    """pd.Series -> np.ndarray of Volume / SMA(Volume, VOL_LEN) where the SMA
    covers the PREVIOUS VOL_LEN bars (shift 1). NaN during warmup or when the
    baseline mean is 0."""
    base = volume.rolling(VOL_LEN).mean().shift(1)
    rv = volume / base
    return rv.to_numpy(dtype=float)


def breakout_events(df):
    """df: ts/Open/High/Low/Close/Volume. Returns list of dicts
    {i, ts, dir, relvol, sr} for every band cross with a defined relvol and a
    full HORIZON of future bars. sr per the frozen sign convention."""
    close = df["Close"]
    _basis, upper, lower = ind.bollinger(close, BB_LEN, BB_MULT)
    up = ind.crossover(close, upper).to_numpy(dtype=bool)
    dn = ind.crossunder(close, lower).to_numpy(dtype=bool)
    rv = rel_volume(df["Volume"])
    c = close.to_numpy(dtype=float)
    ts = df["ts"].to_numpy()
    n = len(df)
    out = []
    for i in range(n):
        if not (up[i] or dn[i]):
            continue
        if i + HORIZON >= n:
            continue
        if not np.isfinite(rv[i]):
            continue
        d = 1.0 if up[i] else -1.0
        sr = d * (c[i + HORIZON] / c[i] - 1.0)
        out.append(dict(i=i, ts=int(ts[i]), dir=d, relvol=float(rv[i]), sr=float(sr)))
    return out


def thin_events(events, min_gap=HORIZON):
    """Sequential thinning on bar index: keep an event only if it is at least
    min_gap bars after the last KEPT one (the first is always kept)."""
    kept = []
    last_i = None
    for e in sorted(events, key=lambda e: e["i"]):
        if last_i is None or e["i"] - last_i >= min_gap:
            kept.append(e)
            last_i = e["i"]
    return kept


def quintile_split(events):
    """Label each event 1..5 by relvol quintile of THIS sample's own
    distribution. Returns {q: [events]}. Boundary values go to the higher
    bin (searchsorted side='left' on the 20/40/60/80th percentiles)."""
    if not events:
        return {}
    vals = np.array([e["relvol"] for e in events], dtype=float)
    edges = np.quantile(vals, [0.2, 0.4, 0.6, 0.8])
    out = {q: [] for q in (1, 2, 3, 4, 5)}
    for e, v in zip(events, vals):
        q = 1 + int(np.searchsorted(edges, v, side="left"))
        out[q].append(e)
    return out


def welch_t(a, b):
    """Welch's t statistic for mean(a) - mean(b); nan when either side has
    fewer than 2 samples or both variances are 0."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    va = a.var(ddof=1) / len(a)
    vb = b.var(ddof=1) / len(b)
    denom = math.sqrt(va + vb)
    if denom == 0:
        return float("nan")
    return float((a.mean() - b.mean()) / denom)


def q5_vs_q1(events):
    """The frozen stage-1 statistic on a (thinned) event list: returns dict
    with n, per-quintile mean sr, and the Q5-Q1 difference + Welch t."""
    split = quintile_split(events)
    if not split:
        return dict(n=0)
    means = {q: (float(np.mean([e["sr"] for e in split[q]])) if split[q] else float("nan"))
             for q in split}
    a = [e["sr"] for e in split.get(5, [])]
    b = [e["sr"] for e in split.get(1, [])]
    diff = (float(np.mean(a)) - float(np.mean(b))) if a and b else float("nan")
    return dict(n=len(events), q_means=means, q_ns={q: len(split[q]) for q in split},
                diff=diff, t=welch_t(a, b))

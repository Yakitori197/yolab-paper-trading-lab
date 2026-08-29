"""Deliberately broken fixture: same input, different output each call.

Stands in for anything carrying state across calls or reading the clock --
a rule like this can never be reproduced from stored data.
"""
_calls = 0

NAME = "不具決定性（測試用）"
PARAMS = dict(stop_mult=2.0)


def build(df, params):
    global _calls
    _calls += 1
    out = df.copy()
    flip = (_calls % 2) == 0
    out["long_sig"] = [flip and i == len(df) // 2 for i in range(len(df))]
    out["short_sig"] = [False] * len(df)
    out["stop_dist"] = params["stop_mult"] * df["Close"] * 0.01
    return out

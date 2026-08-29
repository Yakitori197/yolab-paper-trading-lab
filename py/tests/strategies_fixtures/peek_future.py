"""Deliberately broken fixture: decides today's entry from TOMORROW's close.

This is the single most common way a home-made rule flatters itself, and the
framework's replay-stability check exists to catch exactly this shape.
"""
NAME = "偷看未來（測試用）"
PARAMS = dict(stop_mult=2.0)


def build(df, params):
    close = df["Close"]
    nxt = close.shift(-1)          # <-- the whole point of the fixture
    out = df.copy()
    out["long_sig"] = nxt > close * 1.001
    out["short_sig"] = nxt < close * 0.999
    out["stop_dist"] = params["stop_mult"] * close * 0.01
    return out

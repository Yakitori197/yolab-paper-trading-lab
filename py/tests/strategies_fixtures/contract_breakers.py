"""Fixtures for the contract checks. One module, several deliberately wrong
build() functions -- the test picks which one to expose via `MODE`.
"""
NAME = "契約違規（測試用）"
PARAMS = dict(stop_mult=2.0)

MODE = "short_frame"      # overwritten by the test before build() is called


def _base(df, params):
    out = df.copy()
    out["long_sig"] = [False] * len(df)
    out["short_sig"] = [False] * len(df)
    out["stop_dist"] = params["stop_mult"] * df["Close"] * 0.01
    return out


def build(df, params):
    out = _base(df, params)
    if MODE == "short_frame":
        return out.iloc[:-3]                      # rows silently dropped
    if MODE == "missing_stop":
        return out.drop(columns=["stop_dist"])    # no stop distance at all
    if MODE == "mutates_price":
        out["Close"] = out["Close"] * 1.01        # rewrites price history
        return out
    if MODE == "negative_stop":
        out["stop_dist"] = -1.0                   # stop on the wrong side
        return out
    if MODE == "plot_missing_column":
        return out                                # PLOT declares a column it never returns
    if MODE == "explodes":
        raise ZeroDivisionError("boom")
    if MODE == "not_a_frame":
        return "nope"
    return out

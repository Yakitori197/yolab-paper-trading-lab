"""Valid but maximally noisy fixture: wants to be long on every single bar.

Used to prove the paper-epoch window is enforced by the framework, not by
the plugin's good manners.
"""
NAME = "永遠做多（測試用）"
PARAMS = dict(stop_pct=1.0)


def build(df, params):
    out = df.copy()
    out["long_sig"] = True
    out["short_sig"] = False
    out["stop_dist"] = df["Close"] * (params["stop_pct"] / 100.0)
    return out

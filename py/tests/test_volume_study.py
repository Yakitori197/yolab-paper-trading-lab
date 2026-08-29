"""Hand-derived contracts for study_volume.py (batch #8 V4 stage-1 helpers)
and fetch_oi.py's pure resume logic. The sr sign-convention test exists
because batch #7 died to a pre-registration sign error -- the convention
"sr > 0 means price continued in the breakout direction" is pinned by a
synthetic example here, per the batch #8 registration.
"""
import pandas as pd
import pytest

import db
import study_volume as sv
from fetch_oi import RETENTION_DAYS, resume_start


def make_df(closes, volumes):
    n = len(closes)
    return pd.DataFrame(dict(
        ts=list(range(n)), Open=closes, High=[c * 1.001 for c in closes],
        Low=[c * 0.999 for c in closes], Close=closes, Volume=volumes))


def test_sr_sign_convention_up_cross_then_rise_is_positive():
    # 25 flat bars at 100 (BB collapses onto 100), bar 25 closes at 105 ->
    # crossover; price then keeps climbing, so sr must be POSITIVE.
    # Volume: baseline 100 for 25 bars, spike 200 at the event bar ->
    # relvol = 200 / mean(previous 20 = 100) = 2.0 exactly (own bar excluded).
    closes = [100.0] * 25 + [105.0] + [105.0 + 0.1 * k for k in range(1, 14)]
    volumes = [100.0] * 25 + [200.0] + [100.0] * 13
    events = sv.breakout_events(make_df(closes, volumes))
    e = next(ev for ev in events if ev["i"] == 25)
    assert e["dir"] == 1.0
    assert e["relvol"] == pytest.approx(2.0)
    assert e["sr"] > 0


def test_sr_sign_convention_down_cross_then_fall_is_positive():
    # Mirror case: crossunder at bar 25, price keeps falling -> the breakout
    # "worked", so sr must again be POSITIVE (direction-signed).
    closes = [100.0] * 25 + [95.0] + [95.0 - 0.1 * k for k in range(1, 14)]
    volumes = [100.0] * 39
    events = sv.breakout_events(make_df(closes, volumes))
    e = next(ev for ev in events if ev["i"] == 25)
    assert e["dir"] == -1.0
    assert e["sr"] > 0


def test_thin_events_sequential_gap():
    events = [dict(i=i) for i in (0, 5, 11, 12, 30)]
    kept = [e["i"] for e in sv.thin_events(events, min_gap=12)]
    assert kept == [0, 12, 30]


def test_welch_t_hand_value():
    # mean diff = 2, var(a)=1 (ddof=1) over n=3, var(b)=0:
    # t = 2 / sqrt(1/3) = 2*sqrt(3) = 3.4641...
    assert sv.welch_t([1, 2, 3], [0, 0, 0]) == pytest.approx(3.4641016, abs=1e-6)


def test_quintile_split_balanced():
    events = [dict(relvol=float(v), sr=0.0, i=v) for v in range(1, 11)]
    split = sv.quintile_split(events)
    assert {q: len(v) for q, v in split.items()} == {1: 2, 2: 2, 3: 2, 4: 2, 5: 2}
    assert sorted(e["relvol"] for e in split[5]) == [9.0, 10.0]


def test_resume_start_clamps_to_retention():
    now = 10_000 * 86_400_000
    earliest = now - RETENTION_DAYS * 86_400_000
    assert resume_start((0, None, None), now) == earliest
    fresh_max = now - 1_000
    assert resume_start((5, earliest, fresh_max), now) == fresh_max + 1
    stale_max = now - (RETENTION_DAYS + 10) * 86_400_000
    assert resume_start((5, stale_max - 1, stale_max), now) == earliest


def test_oi_and_taker_upserts_idempotent(tmp_path):
    con = db.connect(tmp_path / "t.db")
    rows_oi = [(1000, 5.0, 100.0), (2000, 6.0, None)]
    assert db.upsert_oi(con, "binanceusdm", "BTC/USDT:USDT", rows_oi) == 2
    assert db.upsert_oi(con, "binanceusdm", "BTC/USDT:USDT", rows_oi) == 2
    assert db.oi_stats(con, "binanceusdm", "BTC/USDT:USDT") == (2, 1000, 2000)
    rows_tr = [(1000, 10.0, 8.0, 1.25)]
    db.upsert_taker_ratio(con, "binanceusdm", "BTC/USDT:USDT", rows_tr)
    db.upsert_taker_ratio(con, "binanceusdm", "BTC/USDT:USDT", rows_tr)
    assert db.taker_ratio_stats(con, "binanceusdm", "BTC/USDT:USDT") == (1, 1000, 1000)
    con.close()

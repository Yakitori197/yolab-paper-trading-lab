import numpy as np
import pandas as pd

import indicators as ind


def test_stdev_is_population():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    got = ind.stdev_pop(s, 4).iloc[-1]
    assert abs(got - np.std([1, 2, 3, 4])) < 1e-12  # ddof=0


def test_percentrank_prev_all_below():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 10.0])
    assert ind.percentrank_prev(s, 4).iloc[-1] == 100.0


def test_percentrank_prev_none_below():
    s = pd.Series([5.0, 1.0, 2.0, 3.0, 0.0])
    assert ind.percentrank_prev(s, 4).iloc[-1] == 0.0


def test_percentrank_prev_ties_count():
    s = pd.Series([2.0, 2.0, 4.0, 4.0, 3.0])
    assert ind.percentrank_prev(s, 4).iloc[-1] == 50.0


def test_atr_constant_tr():
    high = pd.Series([2.0] * 50)
    low = pd.Series([1.0] * 50)
    close = pd.Series([1.5] * 50)
    assert abs(ind.atr_rma(high, low, close, 14).iloc[-1] - 1.0) < 1e-9


def test_crossover_needs_prev_leq():
    a = pd.Series([1.0, 3.0, 4.0])
    b = pd.Series([2.0, 2.0, 2.0])
    x = ind.crossover(a, b)
    assert bool(x.iloc[1]) is True and bool(x.iloc[2]) is False

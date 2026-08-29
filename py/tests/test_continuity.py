from check_data import find_gaps

STEP = 100


def test_no_gap():
    assert find_gaps([0, 100, 200, 300], STEP) == []


def test_single_missing_bar():
    assert find_gaps([0, 100, 300], STEP) == [(200, 1)]


def test_multi_bar_gap():
    assert find_gaps([0, 400], STEP) == [(100, 3)]


def test_unsorted_input():
    assert find_gaps([300, 0, 100], STEP) == [(200, 1)]


def test_empty_and_single():
    assert find_gaps([], STEP) == []
    assert find_gaps([500], STEP) == []

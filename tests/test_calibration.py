"""The calibration experiment's pieces (design 10 §4.3)."""

from thisness.benchmark.calibration import _triage, calibrated_thresholds, wilson_upper


def test_wilson_bound_shrinks_with_evidence():
    assert wilson_upper(0, 0) == 1.0
    assert wilson_upper(0, 10) > wilson_upper(0, 1000) > 0
    assert wilson_upper(5, 100) > 0.05


def test_thresholds_move_only_as_far_as_the_labels_support():
    # Matches from weight 5 up and non-matches below 3: plenty of labels each side.
    labelled = [(w / 10, w >= 50) for w in range(0, 120)] * 10
    high, low = calibrated_thresholds(labelled, t_high=12.0, t_low=0.0, epsilon=0.01)
    assert 5.0 <= high < 12.0 and 0.0 < low < 5.0
    # Twenty labels cannot set a threshold alone: the bound stays above 1%.
    few = [(8.0, True)] * 20
    assert calibrated_thresholds(few, t_high=12.0, t_low=0.0, epsilon=0.01) == (12.0, 0.0)


def test_overlapping_labels_leave_the_thresholds_alone():
    labelled = [(w / 10, w % 2 == 0) for w in range(0, 120)] * 10  # matches everywhere
    assert calibrated_thresholds(labelled, t_high=12.0, t_low=0.0, epsilon=0.01) == (12.0, 0.0)


def test_triage_groups_by_strong_pairs_or_says_why_not():
    members = ["a", "b", "c"]
    assert _triage(members, {("a", "b"): 15.0, ("b", "c"): -3.0}, 10.0, 0.0) == [["a", "b"], ["c"]]
    assert _triage(members, {("a", "b"): 5.0}, 10.0, 0.0) == "contested"
    conflict = {("a", "b"): 15.0, ("b", "c"): 15.0, ("a", "c"): -3.0}
    assert _triage(members, conflict, 10.0, 0.0) == "conflict"

"""Tests for quality_analytics.likely_viewers — weighted viewer attribution (pure)."""
from __future__ import annotations

from scripts.managers.machine_learning.quality_analytics.likely_viewers import (
    infer_likely_viewers,
    platform_weights_for_viewers,
)


def test_owned_watchers_use_actual_shares_over_propensity():
    # Actual plays are ground truth — propensity is ignored when watchers exist.
    v = infer_likely_viewers({"A": 1, "B": 99}, per_title_watchers={"A": 3, "B": 1})
    assert v == {"A": 0.75, "B": 0.25}


def test_new_title_normalizes_propensity():
    v = infer_likely_viewers({"A": 6, "B": 2})
    assert v == {"A": 0.75, "B": 0.25}


def test_threshold_drops_long_tail_and_renormalizes():
    v = infer_likely_viewers({"A": 70, "B": 20, "C": 10}, threshold=0.15)  # C=0.1 dropped
    assert set(v) == {"A", "B"}
    assert abs(v["A"] - 70 / 90) < 1e-9 and abs(v["B"] - 20 / 90) < 1e-9


def test_all_below_threshold_keeps_everyone():
    v = infer_likely_viewers({u: 1 for u in "ABCDEFG"}, threshold=0.15)  # each ~0.143 < 0.15
    assert len(v) == 7 and abs(sum(v.values()) - 1.0) < 1e-9


def test_empty_or_zero_signal_returns_empty():
    assert infer_likely_viewers({}) == {}
    assert infer_likely_viewers({"A": 0}, per_title_watchers={"A": 0}) == {}


def test_platform_weights_normalize_per_user():
    usage = {"A": {"PS5": 8, "iOS": 2}, "B": {"WebOS": 5}}
    w = platform_weights_for_viewers({"A": 0.7, "B": 0.3}, usage)
    assert w == {"A": {"PS5": 0.8, "iOS": 0.2}, "B": {"WebOS": 1.0}}


def test_platform_weights_no_usage_is_empty_dict():
    assert platform_weights_for_viewers({"A": 1.0}, {}) == {"A": {}}

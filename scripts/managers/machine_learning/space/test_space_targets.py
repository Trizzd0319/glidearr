"""Tests for space.space_targets — the single source of truth for disk-space gating.

Covers the 25%-of-total-drive fallback that replaced the hardcoded GB constants
when free_space_limit is unset (the user directive: nothing should ever respect a
hardcoded GB floor; default to 25% of the total drive).
"""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.space_targets import (
    PRESSURE_FALLBACK_FRACTION,
    PRESSURE_FALLBACK_GB,
    deletions_enabled,
    space_targets,
)


def test_configured_limit_drives_band():
    T, U = space_targets({"free_space_limit": 2500}, total_gb=99999.0)
    assert T == 2500.0
    assert U == pytest.approx(2500.0 * 1.10)          # default 10% headroom band


def test_configured_limit_custom_headroom():
    T, U = space_targets({"free_space_limit": 1000, "space_pressure_headroom_ratio": 0.20})
    assert (T, U) == (1000.0, pytest.approx(1200.0))


def test_unset_limit_defaults_to_25pct_of_total():
    T, U = space_targets({}, total_gb=10000.0)
    assert T == pytest.approx(PRESSURE_FALLBACK_FRACTION * 10000.0)   # 2500
    assert U == T                                                     # no headroom band on fallback floor
    assert PRESSURE_FALLBACK_FRACTION == 0.25


def test_unset_limit_total_unknown_uses_last_resort_constant():
    # total inf / None / 0 -> the constant last resort, NOT a fraction of inf.
    for tg in (float("inf"), None, 0):
        T, U = space_targets({}, total_gb=tg)
        assert (T, U) == (PRESSURE_FALLBACK_GB, PRESSURE_FALLBACK_GB), tg


def test_explicit_fallback_gb_honored_when_total_unknown():
    T, U = space_targets({}, fallback_gb=100.0, total_gb=None)
    assert (T, U) == (100.0, 100.0)


def test_25pct_of_total_takes_precedence_over_fallback_gb():
    # When total IS known, the fraction wins over the passed constant.
    T, _ = space_targets({}, fallback_gb=100.0, total_gb=8000.0)
    assert T == pytest.approx(2000.0)   # 25% of 8000, not 100


# ── deletions_enabled (the media-deletion hard safety gate) ──────────────────────
def test_deletions_enabled_requires_explicit_floor():
    assert deletions_enabled({"free_space_limit": 200}) is True
    assert deletions_enabled({"free_space_limit": 0.1}) is True


def test_deletions_disabled_without_floor():
    # unset / 0 / negative / garbage / None config → deletion is hard-disabled
    for cfg in ({}, None, {"free_space_limit": 0}, {"free_space_limit": -5},
                {"free_space_limit": None}, {"free_space_limit": "nope"}):
        assert deletions_enabled(cfg) is False, cfg

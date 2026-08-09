"""Tests for space.space_targets — the single source of truth for disk-space gating.

Covers the 25%-of-total-drive fallback that replaced the hardcoded GB constants
when free_space_limit is unset (the user directive: nothing should ever respect a
hardcoded GB floor; default to 25% of the total drive).
"""
from __future__ import annotations

import pytest

from scripts.managers.machine_learning.space.space_targets import (
    PRESSURE_FALLBACK_FRACTION,
    _CONSENT_ENV_VARS,
    deletions_consented,
    deletions_disabled_reason,
    deletions_enabled,
    space_targets,
)


def test_deletions_disabled_reason_is_accurate(monkeypatch):
    for v in _CONSENT_ENV_VARS:                                       # config-driven, ignore any ambient env
        monkeypatch.delenv(v, raising=False)
    # consent is checked first: a configured floor but NO consent reports the consent gate, not the floor
    assert "consent" in deletions_disabled_reason({"free_space_limit": 1500})
    # consent but no floor → the floor reason
    assert deletions_disabled_reason({"deletions_consent": True}) == "free_space_limit is not set"
    # both set → enabled, no reason
    assert deletions_disabled_reason({"deletions_consent": True, "free_space_limit": 1500}) == ""


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


def test_unset_limit_total_unknown_means_NO_floor():
    # No configured floor AND no readable total -> 0.0, i.e. NO floor at all, so `free < T`
    # is never true and nothing reclaims. The module-level PRESSURE_FALLBACK_GB (25.0) that
    # used to be returned here is GONE: four managers each declared their own last resort and
    # they disagreed (25 / 25 / 1000, plus 25 under the name PRESSURE_THRESHOLD_GB). A
    # wrongly-LOW floor does nothing; a wrongly-HIGH one declares pressure on a healthy disk
    # and starts reclaiming -- so with no basis for a floor, we assert none.
    for tg in (float("inf"), None, 0):
        T, U = space_targets({}, total_gb=tg)
        assert (T, U) == (0.0, 0.0), tg


def test_explicit_fallback_gb_honored_when_total_unknown():
    T, U = space_targets({}, fallback_gb=100.0, total_gb=None)
    assert (T, U) == (100.0, 100.0)


def test_25pct_of_total_takes_precedence_over_fallback_gb():
    # When total IS known, the fraction wins over the passed constant.
    T, _ = space_targets({}, fallback_gb=100.0, total_gb=8000.0)
    assert T == pytest.approx(2000.0)   # 25% of 8000, not 100


# ── deletions gate (consent AND floor — the media-deletion hard safety gate) ─────
def test_deletions_enabled_requires_consent_and_floor():
    assert deletions_enabled({"free_space_limit": 200, "deletions_consent": True}) is True
    assert deletions_enabled({"free_space_limit": 0.1, "deletions_consent": True}) is True


def test_deletions_disabled_without_consent():
    # A floor alone is no longer enough — explicit consent is now required.
    assert deletions_enabled({"free_space_limit": 500}) is False
    assert deletions_enabled({"free_space_limit": 500, "deletions_consent": False}) is False


def test_deletions_disabled_without_floor():
    # Consent given but no usable floor → still hard-disabled.
    for cfg in ({"deletions_consent": True}, {"free_space_limit": 0, "deletions_consent": True},
                {"free_space_limit": -5, "deletions_consent": True},
                {"free_space_limit": None, "deletions_consent": True},
                {"free_space_limit": "nope", "deletions_consent": True}):
        assert deletions_enabled(cfg) is False, cfg
    assert deletions_enabled(None) is False   # no consent, no floor


def test_deletions_consent_env_overrides_config(monkeypatch):
    for var in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(var, raising=False)
    assert deletions_consented({"deletions_consent": True}) is True       # from config
    assert deletions_consented({}) is False                               # default off
    monkeypatch.setenv("RECOMMENDARR_DELETIONS_CONSENT", "true")
    assert deletions_consented({"deletions_consent": False}) is True      # env opts in over config
    monkeypatch.setenv("RECOMMENDARR_DELETIONS_CONSENT", "false")
    assert deletions_consented({"deletions_consent": True}) is False      # env opts out over config

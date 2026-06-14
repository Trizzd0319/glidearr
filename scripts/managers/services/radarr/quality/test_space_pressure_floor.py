"""Floor-derivation tests for RadarrSpacePressureManager._space_targets(instance).

Part of the disk-space gating unification: the per-service space-pressure floor must
derive from free_space_limit, or 25% of the TOTAL drive (mount-deduped via
radarr_api.disk_total_gb) when that's unset — and only fall back to the hardcoded
PRESSURE_THRESHOLD_GB when the total is also unreadable. Drives the REAL helper via a
minimal stub manager (object.__new__ bypasses the heavy __init__/registry).
"""
from __future__ import annotations

import pytest

from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _FakeApi:
    def __init__(self, total_gb):
        self._t = total_gb

    def disk_total_gb(self, instance):
        return self._t


class _Logger:
    def log_warning(self, *a, **k): pass
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


def _mk(config, total_gb):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = config
    m.logger = _Logger()
    m.radarr_api = _FakeApi(total_gb)
    return m


def test_configured_limit_drives_band():
    T, U = _mk({"free_space_limit": 2500}, total_gb=8000.0)._space_targets("standard")
    assert T == 2500.0
    assert U == pytest.approx(2500.0 * 1.10)


def test_unset_limit_defaults_to_25pct_of_total():
    T, U = _mk({}, total_gb=8000.0)._space_targets("standard")
    assert T == pytest.approx(2000.0)   # 25% of 8000, not the 25 GB constant
    assert U == T                        # no headroom band on the fallback floor


def test_last_resort_constant_when_total_unreadable():
    # disk_total_gb returns inf on API error -> last-resort PRESSURE_THRESHOLD_GB.
    T, U = _mk({}, total_gb=float("inf"))._space_targets("standard")
    assert (T, U) == (RadarrSpacePressureManager.PRESSURE_THRESHOLD_GB,) * 2


def test_no_instance_skips_total_fetch():
    # instance=None -> total is never fetched -> last-resort constant.
    T, U = _mk({}, total_gb=8000.0)._space_targets(None)
    assert (T, U) == (RadarrSpacePressureManager.PRESSURE_THRESHOLD_GB,) * 2

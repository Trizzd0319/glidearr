"""Floor-derivation tests for SonarrSpacePressureManager._space_targets(instance).

Mirror of the Radarr space-pressure floor test: the TV downgrade floor must derive from
free_space_limit, or 25% of the total drive (via sonarr_api.disk_total_gb) when unset,
falling back to PRESSURE_FALLBACK_GB only when the total is also unreadable.
"""
from __future__ import annotations

import pytest

from scripts.managers.services.sonarr.series.space_pressure import SonarrSpacePressureManager


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
    m = object.__new__(SonarrSpacePressureManager)
    m.config = config
    m.logger = _Logger()
    m.sonarr_api = _FakeApi(total_gb)
    return m


def test_configured_limit_drives_band():
    T, U = _mk({"free_space_limit": 1000}, total_gb=8000.0)._space_targets("standard")
    assert T == 1000.0
    assert U == pytest.approx(1100.0)


def test_unset_limit_defaults_to_25pct_of_total():
    T, U = _mk({}, total_gb=8000.0)._space_targets("standard")
    assert T == pytest.approx(2000.0)
    assert U == T


def test_last_resort_constant_when_total_unreadable():
    T, U = _mk({}, total_gb=float("inf"))._space_targets("standard")
    assert (T, U) == (SonarrSpacePressureManager.PRESSURE_FALLBACK_GB,) * 2

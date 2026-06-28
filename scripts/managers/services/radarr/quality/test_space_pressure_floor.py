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


# ── run(): downgrade deferral to the coordinator (single shared-mount pass) ────
def _mk_run(coordinator_owns, *, free=100.0, T=1000.0, U=1100.0):
    """A manager whose run() pipeline is fully stubbed except the deferral branch.
    The default instance is 'standard' (what _resolve_instance(None) returns)."""
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {}
    m.logger = _Logger()
    m.calls = {"upgrades": 0, "downgrades": 0, "deletions": 0}
    m._resolve_instance = lambda inst: inst if inst is not None else "standard"
    m._get_free_space_gb = lambda inst: free
    m._space_targets = lambda inst: (T, U)
    m._coordinator_owns_deletion = lambda: coordinator_owns

    def _up(inst, fg): m.calls["upgrades"] += 1; return {}
    def _dn(inst, fg): m.calls["downgrades"] += 1; return {"downgraded": 1}
    def _del(inst, fg): m.calls["deletions"] += 1; return {}
    m.run_active_watcher_upgrades = _up
    m.run_downgrades = _dn
    m.run_deletions = _del
    return m


def test_run_defers_default_instance_downgrade_to_coordinator():
    # Coordinator owns AND this is the default instance -> downgrade NOT run here.
    m = _mk_run(coordinator_owns=True)
    out = m.run("standard")
    assert m.calls["downgrades"] == 0
    assert out["downgrades"] == {"deferred_to_coordinator": True}
    assert m.calls["deletions"] == 1   # run_deletions still invoked (it defers internally)


def test_run_defers_nondefault_instance_downgrade_to_coordinator():
    # Coordinator owns: EVERY instance (incl. non-default ultra/test) now defers its
    # downgrade to the coordinator, which downgrades all instances on the shared mount.
    m = _mk_run(coordinator_owns=True)
    out = m.run("ultra")
    assert m.calls["downgrades"] == 0
    assert out["downgrades"] == {"deferred_to_coordinator": True}


def test_run_downgrades_when_coordinator_disabled():
    # Coordinator not owning -> downgrade runs here regardless of instance.
    m = _mk_run(coordinator_owns=False)
    m.run("standard")
    assert m.calls["downgrades"] == 1

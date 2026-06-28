"""Deferral test for SonarrOrchestrationSeriesManager.run_space_pressure_downgrades.

Step-1 of the single-space-coordinator move: when the cross-service coordinator owns
reclamation, the per-service TV downgrade pass must DEFER (so the downgrade never runs
twice — once here and again in the coordinator's Stage 1). Drives the real method via a
minimal stub manager (object.__new__ bypasses the heavy __init__/registry).
"""
from __future__ import annotations

from scripts.managers.services.sonarr.orchestration.series import (
    SonarrOrchestrationSeriesManager,
)


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _InstMgr:
    def resolve_instance(self, inst): return inst if inst is not None else "standard"


class _Manager:
    def __init__(self): self.instance_manager = _InstMgr()


class _SP:
    def __init__(self): self.downgrade_calls = 0
    def get_free_space_gb(self, inst): return 100.0          # well below U
    def _space_targets(self, inst): return (1000.0, 1100.0)  # T, U
    def run_downgrades(self, inst, free):
        self.downgrade_calls += 1
        return {"downgraded": 1}


class _SeriesMgr:
    def __init__(self, sp): self.space_pressure = sp


def _mk(config, sp):
    m = object.__new__(SonarrOrchestrationSeriesManager)
    m.config = config
    m.logger = _Logger()
    m.manager = _Manager()
    m.series_manager = _SeriesMgr(sp)
    return m


def test_defers_to_coordinator_when_owned():
    sp = _SP()
    cfg = {"space_coordinator_enabled": True, "deletions_consent": True,
           "free_space_limit": 5500, "tv_downgrade_enabled": True}
    out = _mk(cfg, sp).run_space_pressure_downgrades(instance=None)
    assert out == {"action": "deferred_to_coordinator"}
    assert sp.downgrade_calls == 0


def test_runs_downgrades_when_coordinator_disabled():
    sp = _SP()
    cfg = {"space_coordinator_enabled": False, "tv_downgrade_enabled": True}
    _mk(cfg, sp).run_space_pressure_downgrades(instance=None)
    assert sp.downgrade_calls == 1

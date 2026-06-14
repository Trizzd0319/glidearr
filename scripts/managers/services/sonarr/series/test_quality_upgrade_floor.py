"""Regression test: the Sonarr active-watcher upgrade gate now derives its floor from
space_targets (free_space_limit, or 25% of the total drive when unset) instead of the
old hardcoded UPGRADE_MIN_FREE_GB=100.

Drives the REAL SonarrSeriesQualityManager.run_active_watcher_upgrades via a stub
manager (object.__new__ bypasses __init__). We assert on the "skipped" log line: the
gate either skips (free below the floor) or proceeds past it (then no-ops at the stub
registry). The pivotal case is free=150 GB with a 8000 GB drive: under the old 100 GB
floor it would have proceeded; under the 25%-of-total floor (2000 GB) it must skip.
"""
from __future__ import annotations


class _Logger:
    def __init__(self):
        self.infos = []

    def log_info(self, m, *a, **k):
        self.infos.append(m)

    def log_debug(self, *a, **k):
        pass

    def log_warning(self, *a, **k):
        pass


class _Api:
    def __init__(self, total_gb):
        self._t = total_gb

    def disk_total_gb(self, instance):
        return self._t


class _Reg:
    """Stub registry: returns no episode-files manager so the upgrade pass no-ops
    immediately *after* clearing the space gate (df is None -> early return)."""
    def get(self, *a, **k):
        return None


def _mk(config, free_gb, total_gb):
    from scripts.managers.services.sonarr.series.quality import SonarrSeriesQualityManager
    m = object.__new__(SonarrSeriesQualityManager)
    m.config = config
    m.logger = _Logger()
    m.instance_manager = None              # instance passes through unresolved
    m.sonarr_api = _Api(total_gb)
    m.registry = _Reg()
    m.get_free_space_gb = lambda instance: free_gb   # shadow the heavy method
    return m


def _skipped(m) -> bool:
    return any("upgrades skipped" in s.lower() for s in m.logger.infos)


def test_skips_below_25pct_of_total_when_limit_unset():
    # free 150 GB, total 8000 GB -> floor 2000 GB -> SKIP. (Old 100 GB floor would have
    # let this through — proving the hardcoded constant is gone.)
    m = _mk({}, free_gb=150.0, total_gb=8000.0)
    stats = m.run_active_watcher_upgrades("standard")
    assert _skipped(m), m.logger.infos
    assert stats["upgraded"] == 0


def test_proceeds_above_25pct_of_total():
    # free 2500 GB > floor 2000 GB -> pass the gate (then no-ops at the stub registry,
    # so no "skipped" line is logged).
    m = _mk({}, free_gb=2500.0, total_gb=8000.0)
    m.run_active_watcher_upgrades("standard")
    assert not _skipped(m), m.logger.infos


def test_configured_limit_overrides_total():
    # free_space_limit=3000 -> U=3300; free 3100 is below U -> SKIP regardless of total.
    m = _mk({"free_space_limit": 3000}, free_gb=3100.0, total_gb=99999.0)
    m.run_active_watcher_upgrades("standard")
    assert _skipped(m), m.logger.infos

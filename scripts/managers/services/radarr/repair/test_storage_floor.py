"""Tests: RadarrRepairStorageManager.check_free_space classifies root folders against the
single source of truth (space_targets) — critical below the floor T, warn in [T, U), ok
at/above U. T = free_space_limit, or 25% of the total drive (disk_total_gb) when unset;
the legacy DEFAULT_CRIT/WARN_GB survive only as the deepest last resort.
"""
from __future__ import annotations

from scripts.managers.services.radarr.repair.storage import RadarrRepairStorageManager

_GIB = 1024 ** 3


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _Inst:
    def __init__(self, total_gb):
        self._t = total_gb

    def disk_total_gb(self, instance):
        return self._t


class _Radarr:
    def __init__(self, free_gb, total_gb):
        self._free, self._total = free_gb, total_gb

    def _make_request(self, instance, endpoint, fallback=None):
        if endpoint == "rootfolder":
            # When the drive total is "unreadable" (inf), the per-folder API often
            # reports totalSpace=0 (the directive's totalSpace=0 CRITICAL case).
            finite = self._total == self._total and self._total != float("inf")
            tot = int(self._total * _GIB) if finite else 0
            return [{"path": "/movies",
                     "freeSpace": int(self._free * _GIB),
                     "totalSpace": tot}]
        return fallback


def _mk(config, free_gb, total_gb):
    m = object.__new__(RadarrRepairStorageManager)
    m.config = config
    m.logger = _Logger()
    m.crit_threshold_gb = 20.0     # DEFAULT_CRIT_GB
    m.warn_threshold_gb = 50.0     # DEFAULT_WARN_GB
    m.instance_manager = _Inst(total_gb)
    m.radarr_api = _Radarr(free_gb, total_gb)
    m._resolve_instance = lambda i: i
    return m


def _status(config, free_gb, total_gb):
    return _mk(config, free_gb, total_gb).check_free_space("standard")[0]["status"]


def test_critical_below_25pct_of_total_when_limit_unset():
    # total 8000 -> floor 2000. free 1500 < 2000 -> critical (old 20 GB floor would say ok).
    assert _status({}, free_gb=1500.0, total_gb=8000.0) == "critical"


def test_ok_above_25pct_of_total_when_limit_unset():
    assert _status({}, free_gb=3000.0, total_gb=8000.0) == "ok"


def test_warn_band_from_configured_limit():
    # free_space_limit 2500 -> T=2500, U=2750. free 2600 in [T,U) -> warn.
    cfg = {"free_space_limit": 2500}
    assert _status(cfg, free_gb=2600.0, total_gb=8000.0) == "warn"
    assert _status(cfg, free_gb=2400.0, total_gb=8000.0) == "critical"
    assert _status(cfg, free_gb=2800.0, total_gb=8000.0) == "ok"


def test_deepest_fallback_keeps_legacy_20_50_band():
    # No free_space_limit AND total unreadable (inf) -> crit 20, warn max(20,50)=50.
    assert _status({}, free_gb=10.0, total_gb=float("inf")) == "critical"
    assert _status({}, free_gb=35.0, total_gb=float("inf")) == "warn"
    assert _status({}, free_gb=80.0, total_gb=float("inf")) == "ok"


def test_zero_rootfolder_total_displays_real_mount_total_not_zero():
    # Radarr reports per-rootfolder totalSpace=0 for Docker mounts; the row must surface the
    # REAL mount total (disk_total_gb), never a misleading 0.0, and still classify by FREE.
    m = _mk({"free_space_limit": 2500}, free_gb=2271.0, total_gb=23301.0)
    m.radarr_api = _Radarr(2271.0, float("inf"))   # rootfolder totalSpace=0, disk_total_gb=23301
    row = m.check_free_space("standard")[0]
    assert row["status"] == "critical"             # 2271 < 2500 floor — verdict unaffected by total
    assert row["total_space_gb"] == 23301.0        # not 0.0


def test_run_scans_free_space_only_once():
    # run() must not log every root twice: it scans once and feeds the result into
    # recommend_deletions instead of letting it re-scan.
    m = _mk({"free_space_limit": 2500}, free_gb=2271.0, total_gb=8000.0)
    calls = {"n": 0}
    real = m.check_free_space

    def _counting(inst):
        calls["n"] += 1
        return real(inst)

    m.check_free_space = _counting
    m.find_large_movies = lambda inst: []          # isolate from the large-movie scan
    m.run("standard")
    assert calls["n"] == 1                          # one scan, not two

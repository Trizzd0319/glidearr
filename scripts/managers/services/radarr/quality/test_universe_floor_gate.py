"""Regression test for DEFECT 2: universe upgrades must respect the space-pressure floor.

Before the fix, RadarrQualityUniverseManager.evaluate_quality_actions marked
universe titles for UPGRADE whenever free > 50 GB (DEFAULT_UPGRADE_GB), against raw
free disk — ignoring the configured free_space_limit. Under genuine pressure
(free below the floor) it would plan +hundreds of GB of upgrades while
space-pressure was deleting/downgrading to free space (net plan consuming more
than it frees, and potentially re-inflating a bare-"universe" title that
space-pressure just downgraded as a last resort).

Fix: gate the upgrade branch on U (the top of the pressure band from
space_targets(config)). When free_space_limit is UNSET, U defaults to 25% of the
TOTAL drive (via instance_manager.disk_total_gb) — never the old hardcoded 50 GB.

Drives the REAL evaluate_quality_actions via a minimal stub manager (bypassing the
heavy __init__) so the actual decision + space_targets wiring is exercised.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.radarr.quality.universe import RadarrQualityUniverseManager


class _FakeMfm:
    def __init__(self, df):
        self._df = df
        self.saved = None

    def load(self, instance):
        return self._df.copy()

    def save(self, instance, df):
        self.saved = df


class _FakeLogger:
    def log_warning(self, *a, **k): pass
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _FakeInstanceMgr:
    """Supplies the mount-deduped total drive size used for the 25%-of-total fallback."""
    def __init__(self, total_gb):
        self._total = total_gb

    def disk_total_gb(self, instance):
        return self._total


def _mk_mgr(config, df, total_gb=float("inf")):
    m = object.__new__(RadarrQualityUniverseManager)   # skip __init__/registry/base
    m.config = config
    m.logger = _FakeLogger()
    m.instance_manager = _FakeInstanceMgr(total_gb)
    _mfm = _FakeMfm(df)
    m._get_movie_files_manager = lambda: _mfm           # type: ignore[attr-defined]
    m._resolve_instance = lambda i: i                   # type: ignore[attr-defined]
    return m


def _universe_df(n=3):
    return pd.DataFrame([
        dict(keep_policy="universe", title=f"Universe Movie {i}", quality_action=None,
             universe_name="mcu")
        for i in range(n)
    ])


def test_downgrade_while_under_floor():
    """free below the floor T -> NO upgrades; universe titles marked for DOWNGRADE.
    Under pressure universe quality is lowered to help reclaim (never deleted, so quality
    is the only lever) — the trigger is the floor T, not the old fixed 10 GB."""
    cfg = {"free_space_limit": 9000}          # T=9000, U=9900
    mgr = _mk_mgr(cfg, _universe_df())
    stats = mgr.evaluate_quality_actions("standard", free_space_gb=8854.0)
    assert stats["upgrade_marked"] == 0, stats
    assert stats["downgrade_marked"] == 3, stats   # 8854 < floor 9000 -> downgrade under pressure


def test_upgrade_when_above_band():
    """free comfortably above U (and the upgrade threshold) -> upgrades marked."""
    cfg = {"free_space_limit": 2500}          # U = 2750
    mgr = _mk_mgr(cfg, _universe_df(4))
    stats = mgr.evaluate_quality_actions("standard", free_space_gb=8883.0)
    assert stats["upgrade_marked"] == 4, stats


def test_just_inside_band_holds():
    """free between the floor T and the band top U -> HOLD (no up, no down)."""
    cfg = {"free_space_limit": 9000}          # T = 9000, U = 9900
    mgr = _mk_mgr(cfg, _universe_df())
    # 9500 is above the floor but inside the band [9000, 9900) -> hold (hysteresis):
    # neither upgrade nor downgrade.
    stats = mgr.evaluate_quality_actions("standard", free_space_gb=9500.0)
    assert stats["upgrade_marked"] == 0, stats
    assert stats["downgrade_marked"] == 0, stats


def test_fallback_is_25pct_of_total_when_limit_unset():
    """No free_space_limit -> the upgrade gate U defaults to 25% of the TOTAL drive,
    NOT the old hardcoded 50 GB. total=10000 GB -> floor 2500 GB."""
    # free 3000 GB > 2500 (25% of 10000) -> upgrade.
    mgr = _mk_mgr({}, _universe_df(2), total_gb=10000.0)
    assert mgr.evaluate_quality_actions("standard", free_space_gb=3000.0)["upgrade_marked"] == 2
    # free 2000 GB < 2500 (the floor) -> DOWNGRADE, not upgrade (the floor is 25%-of-total,
    # not the old 50 GB gate).
    mgr2 = _mk_mgr({}, _universe_df(2), total_gb=10000.0)
    s2 = mgr2.evaluate_quality_actions("standard", free_space_gb=2000.0)
    assert s2["upgrade_marked"] == 0, s2
    assert s2["downgrade_marked"] == 2, s2


def test_last_resort_constant_when_total_unknown():
    """No free_space_limit AND total unknown (inf) -> last-resort 25 GB constant
    (PRESSURE_FALLBACK_GB), never the old 50 GB."""
    mgr = _mk_mgr({}, _universe_df(2), total_gb=float("inf"))
    assert mgr.evaluate_quality_actions("standard", free_space_gb=100.0)["upgrade_marked"] == 2
    mgr2 = _mk_mgr({}, _universe_df(2), total_gb=float("inf"))
    # 30 GB is below the old 50 GB gate but ABOVE the 25 GB last-resort -> still upgrades,
    # proving 50 no longer floors anything.
    assert mgr2.evaluate_quality_actions("standard", free_space_gb=30.0)["upgrade_marked"] == 2
    mgr3 = _mk_mgr({}, _universe_df(2), total_gb=float("inf"))
    assert mgr3.evaluate_quality_actions("standard", free_space_gb=10.0)["upgrade_marked"] == 0


def test_universe_downgrade_steps_one_rank_not_to_floor():
    """Under pressure a universe title steps DOWN one profile rank per pass, not straight
    to the lowest (SD) — quality erodes gradually across the franchise pool."""
    mgr = _mk_mgr({}, _universe_df(1))
    ranked = [{"id": 10}, {"id": 11}, {"id": 12}, {"id": 13}]   # ranks 0..3 (SD..4K)
    # At 4K (rank 3) -> one rank down to rank 2 (id 12), NOT the rank-0 floor.
    assert mgr._get_target_profile(ranked, "downgrade", current_profile_id=13, min_rank=0)["id"] == 12
    # One rank above the floor -> steps to the floor.
    assert mgr._get_target_profile(ranked, "downgrade", current_profile_id=11, min_rank=0)["id"] == 10
    # Already at the floor -> nothing to step.
    assert mgr._get_target_profile(ranked, "downgrade", current_profile_id=10, min_rank=0) is None


def test_universe_downgrade_target_is_runtime_sized_best_quality_tier():
    """Universe downgrade now uses the shared resolution-tier logic (step_targets): the
    best-quality profile at the next-lower resolution whose runtime-sized estimate still
    reduces the current file — same as movies/TV."""
    _GIB = 1024 ** 3
    ranked = [
        {"id": 4, "items": [{"allowed": True, "quality": {"resolution": 480,  "name": "WEBDL-480p"}}]},
        {"id": 1, "items": [{"allowed": True, "quality": {"resolution": 720,  "name": "WEBDL-720p"}}]},
        {"id": 3, "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "Remux-1080p"}}]},  # ~235
        {"id": 2, "items": [{"allowed": True, "quality": {"resolution": 1080, "name": "WEBDL-1080p"}}]},  # ~56
    ]
    mgr = _mk_mgr({}, _universe_df(1))
    # Big 4K file (50 GiB, 100 min) -> best-quality 1080p reduction = Remux-1080p (id 3).
    big = {"resolution": 2160, "size_bytes": int(50 * _GIB), "runtime_minutes": 100.0}
    assert mgr._downgrade_target(big, ranked, current_profile_id=99)["id"] == 3
    # Small 4K file (15 GiB) -> Remux-1080p (~23 GiB) would be bigger -> WEBDL-1080p (id 2).
    small = {"resolution": 2160, "size_bytes": int(15 * _GIB), "runtime_minutes": 100.0}
    assert mgr._downgrade_target(small, ranked, current_profile_id=99)["id"] == 2
    # Unknown resolution -> legacy single-rank fallback (id 3 at index 2 -> index 1 -> id 1).
    unknown = {"resolution": None, "size_bytes": None, "runtime_minutes": None}
    assert mgr._downgrade_target(unknown, ranked, current_profile_id=3)["id"] == 1

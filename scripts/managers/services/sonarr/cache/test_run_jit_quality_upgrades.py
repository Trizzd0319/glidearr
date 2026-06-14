"""End-to-end grouping test for run_jit_quality_upgrades (deliverable B).

Covers the half the worker test can't: that a real mixed-target candidate frame actually gets
bucketed into the correct per-(series, tier) groups BEFORE reaching the worker. Drives the real
method (real jit_candidates / choose_jit_profile / target_tier_key / jit_step_down_pids /
_profile_max_quality / _estimate_grab_gb) with only I/O shadowed, and spies on
_spawn_jit_search_worker to capture the work dict.

Deterministic space: total=1000, free_space_limit=100 → reserve 110. Two next-up eps of ONE series
with free=137 → ep1 fits 2160 (137−19.53=117.47≥110); after decrement ep2 only fits 1080
(117.47−6.84=110.63≥110) → two tier groups {2160,1080}.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k): pass    # unified grab pass prints the breakdown via log_grid


def _prof(pid, res):
    return {"id": pid, "name": f"P{res}",
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_PROFILES = [_prof(13, 2160), _prof(12, 1080), _prof(11, 720)]
_MEASURED = {"q2160": 200.0, "q1080": 70.0, "q720": 30.0}   # est@100min: 19.53/6.84/2.93 GiB

# Force the likelihood cap to 2160 for any likelihood so the tier split is purely space-driven.
_WL = {
    "rewatch_floor": 90, "watched_floor": 50, "started_floor": 40, "abandoned_ceiling": 25,
    "untouched_mode": "absolute", "untouched_pct_floor": 0, "untouched_base": 12,
    "untouched_score_gain": 1.0, "affinity_cap": 75, "affinity_boost": 1.8,
    "uhd_cutoff": 0, "fhd_cutoff": 0, "hd_cutoff": 0,
    "uhd_res": 2160, "fhd_res": 1080, "hd_res": 720, "floor_res": 720,
}


def _row(sid, sn, en, fid):
    return {
        "series_id": sid, "season_number": sn, "episode_number": en, "series_title": "S",
        "episode_file_id": fid, "next_episode": True, "is_watched": False,
        "upgraded_for_watching": False, "keep_policy": None, "certification": "tv-ma",
        "runtime_seconds": 6000, "quality_name": "HDTV-720p", "quality_source": "tv",
        "resolution": 720, "video_codec": "x264", "size_bytes": 1_000_000_000,
    }


def test_mixed_target_series_buckets_into_two_tier_groups():
    df = pd.DataFrame([_row(10, 1, 1, 1001), _row(10, 1, 2, 1002)])
    captured = {}

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "qualityprofile":
                return list(_PROFILES)
            return fallback

    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = False
    m.global_cache = None
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 137.0
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: captured.update(work)

    stats = m.run_jit_quality_upgrades("inst")

    assert stats["upgraded"] == 2
    assert set(captured.keys()) == {10}                  # one series
    tiers = captured[10]
    assert set(tiers.keys()) == {2160, 1080}             # split into two tier groups (not one, not by id)
    # ep1 (S01E01, id 9001) is the 2160 group; ep2 (S01E02, id 9002) is the 1080 group.
    assert [e[0] for e in tiers[2160]["eps"]] == [9001]
    assert [e[0] for e in tiers[1080]["eps"]] == [9002]
    # each group's ladder tops out at its own tier (no rung above it) — the over-grab invariant.
    assert tiers[2160]["step_pids"] == [13, 12, 11]
    assert tiers[1080]["step_pids"] == [12, 11]


def test_single_target_series_is_one_group():
    # Ample space → both eps fit 2160 → ONE group (the all-same-target case, byte-identical path).
    df = pd.DataFrame([_row(10, 1, 1, 1001), _row(10, 1, 2, 1002)])
    captured = {}

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            return list(_PROFILES) if endpoint == "qualityprofile" else fallback

    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = False
    m.global_cache = None
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: captured.update(work)

    m.run_jit_quality_upgrades("inst")
    assert set(captured[10].keys()) == {2160}            # one tier group, both eps
    assert sorted(e[0] for e in captured[10][2160]["eps"]) == [9001, 9002]


def test_acquire_missing_episode_grabs_at_jit_tier():
    # A MISSING next-up episode (no file) is now an ACQUIRE: it routes through the SAME tier
    # calibration + step-down worker as a re-quality, but is counted 'acquired' (not 'upgraded'),
    # is NOT marked upgraded_for_watching (no prior file), and is NOT 'upgrade'-stamped in the
    # ledger (sync_from_tautulli owns the 'acquire' stamp → plan-summary oracle unchanged).
    df = pd.DataFrame([_row(10, 1, 1, None)])   # fid=None → missing → ACQUIRE
    captured = {}

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            return list(_PROFILES) if endpoint == "qualityprofile" else fallback

    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = False
    m.global_cache = None
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0        # ample space → 2160 tier
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: captured.update(work)

    stats = m.run_jit_quality_upgrades("inst")

    assert stats["acquired"] == 1 and stats["upgraded"] == 0
    # bucketed for the shared worker to search at the calibrated 2160 tier
    assert [e[0] for e in captured[10][2160]["eps"]] == [9001]
    # acquire does NOT mark the row bumped and does NOT stamp the 'upgrade' ledger
    assert bool(df.iloc[0]["upgraded_for_watching"]) is False
    assert pd.isna(df.iloc[0]["planned_action"])


def test_jit_plan_routes_into_run_summary():
    """D.3: with a run_summary collector present, the JIT next-up grab plan grid is routed
    into the consolidated end-of-run report (sonarr block, Instance column), not logged inline."""
    from scripts.support.utilities.logger.run_summary import RunSummaryManager

    df = pd.DataFrame([_row(10, 1, 1, 1001), _row(10, 1, 2, 1002)])

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            return list(_PROFILES) if endpoint == "qualityprofile" else fallback

    class _GC:
        def __init__(self, rs): self.run_summary = rs
        def set(self, *a, **k): pass
        def get(self, *a, **k): return None

    rs = RunSummaryManager()
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = False
    m.global_cache = _GC(rs)
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0           # ample → both eps, real grab rows
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: None

    m.run_jit_quality_upgrades("sonarr")

    class _Cap:
        def __init__(self): self.grids = []
        def log_grid(self, headers, rows, title="", cap=None):
            self.grids.append((title, list(headers), [list(r) for r in rows]))
        def log_info(self, *a, **k): pass

    cap = _Cap()
    rs.render(cap)
    match = [(h, r) for (t, h, r) in cap.grids if t == "JIT next-up grab plan"]
    assert match, [t for t, _, _ in cap.grids]
    headers, rows = match[0]
    assert headers[0] == "Instance"
    assert rows and all(r[0] == "sonarr" for r in rows)


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def test_jit_grabbed_persisted_in_dry_run():
    """REGRESSION (review): the playlist JIT signal sonarr/<i>/jit_grabbed must be POPULATED
    in dry_run — the default operating mode. It was derived from the live-only `eps` list
    (dry_run-gated), so it wrote [] in dry_run and the whole JIT-priority playlist feature
    was silently inert. Now sourced from planned_sids, collected unconditionally."""
    df = pd.DataFrame([_row(10, 1, 1, 1001), _row(10, 1, 2, 1002)])

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            return list(_PROFILES) if endpoint == "qualityprofile" else fallback

    cache = _Cache()
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = True                                       # the DEFAULT mode
    m.global_cache = cache
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0            # ample space → both eps planned
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: None

    m.run_jit_quality_upgrades("inst")
    assert cache.get("sonarr/inst/jit_grabbed") == [10]    # planned even though dry_run


def test_jit_grabbed_cleared_on_early_return():
    """REGRESSION (re-review): the early returns (here space-pressure) must STILL clear a
    stale jit_grabbed — a series the user stopped watching must stop being boosted even
    when the JIT pass short-circuits (otherwise the last-populated set lingers forever)."""
    df = pd.DataFrame([_row(10, 1, 1, 1001)])
    cache = _Cache()
    cache.set("sonarr/inst/jit_grabbed", [99])             # stale from a prior run

    class _Api:
        def _make_request(self, *a, **k): return []

    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.config = {"free_space_limit": 100, "watch_likelihood": _WL,
                "jit_per_episode_tiers": {"enabled": True}}
    m.dry_run = True
    m.global_cache = cache
    m.sonarr_api = _Api()
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 1.0               # << reserve → space-pressure return
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda inst, sid, sn, en: 9000 + en
    m._spawn_jit_search_worker = lambda inst, work: None

    m.run_jit_quality_upgrades("inst")
    assert cache.get("sonarr/inst/jit_grabbed") == []      # stale [99] cleared, not left behind

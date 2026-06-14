"""Tests for run_pilot_search — deliverable C: best-tier-first / space-divert pilot strategy.

Driven in dry_run via the object.__new__ pattern (heavy helpers shadowed), so no network. In
dry_run the chosen profile is stamped onto pilot_last_profile_id (via _mark_searched), so the test
reads that back to assert which tier the pilot targeted.

Reserve math (deterministic): total=1000, free_space_limit=100 → space_targets U=110,
jit_reserve_gb = max(110, 1000*0.05=50) = 110. Per-episode estimates @100min:
2160p≈19.53, 1080p≈6.84, 720p≈2.93 GiB.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _StubLogger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


def _prof(pid, res):
    return {"id": pid, "name": f"P{res}",
            "items": [{"allowed": True, "quality": {"resolution": res, "name": f"q{res}"}}]}


_PROFILES = [_prof(13, 2160), _prof(12, 1080), _prof(11, 720)]   # ranked → 11(floor)/12/13(widest)
_MEASURED = {"q2160": 200.0, "q1080": 70.0, "q720": 30.0}


class _FakeApi:
    def __init__(self, series_qp):
        self._series_qp = series_qp
        self.puts = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "qualityprofile":
            return list(_PROFILES)
        if endpoint == "series" and method == "GET":
            return [{"id": 1, "qualityProfileId": self._series_qp, "runtime": 100, "title": "S"}]
        if endpoint.startswith("series/") and method == "PUT":
            self.puts.append(payload.get("qualityProfileId"))
            return payload
        return fallback


def _stub_df(last_pid=None):
    return pd.DataFrame([{
        "series_id": 1, "series_title": "S", "is_pilot": True, "episode_file_id": None,
        "pilot_search_attempts": (1 if last_pid is not None else None),
        "pilot_last_searched_at": None,
        "pilot_last_profile_id": last_pid,
    }])


def _run(config, *, free_gb, series_qp=99, last_pid=None):
    df = _stub_df(last_pid=last_pid)
    api = _FakeApi(series_qp=series_qp)
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = config
    m.dry_run = True
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: free_gb
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda *a, **k: 999
    stats = m.run_pilot_search("inst")
    return df, stats, api


_ON = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": True}}
_ON_FORCE = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": True, "force_floor": True}}
_OFF = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": False}}


# ── best-tier-first ───────────────────────────────────────────────────────────────
def test_pilot_targets_highest_tier_when_space_ample():
    df, stats, _ = _run(_ON, free_gb=5000)
    assert int(df.at[0, "pilot_last_profile_id"]) == 13   # 2160p — the highest tier
    assert stats["searched"] == 1


def test_pilot_diverts_down_for_space_within_run():
    # free 120: 4K (−19.53) breaches the 110 reserve; 1080 (−6.84 → 113.2) fits → pid 12.
    df, _, _ = _run(_ON, free_gb=120)
    assert int(df.at[0, "pilot_last_profile_id"]) == 12   # diverted 2160 → 1080 for space
    # tighter (free 113): 1080 breaches (106.2<110); 720 fits (110.07) → pid 11.
    df2, _, _ = _run(_ON, free_gb=113)
    assert int(df2.at[0, "pilot_last_profile_id"]) == 11


def test_pilot_diverts_down_across_runs_for_availability():
    # Ample space (ceiling = 2160). Last run searched 2160 (current QP & last_pid both 13) and
    # found nothing → this run diverts DOWN one rung to 1080 (availability divert).
    df, _, _ = _run(_ON, free_gb=5000, series_qp=13, last_pid=13)
    assert int(df.at[0, "pilot_last_profile_id"]) == 12


def test_pilot_skipped_when_no_space_and_force_floor_off():
    # free 100: even 720 (−2.93 → 97.07) breaches the 110 reserve → None → skip (default).
    df, stats, api = _run(_ON, free_gb=100)
    assert stats["skipped_space"] == 1
    assert stats["searched"] == 0
    assert pd.isna(df.at[0, "pilot_last_profile_id"])     # never searched → never stamped
    # No profile change applied — the stub is left untouched for re-probe next run. (run_pilot_search
    # has no delete path at all; the actual never-delete-by-guard proof lives in the deletion-manager
    # tests — here we only prove the pilot is DEFERRED, not searched, when no tier fits.)
    assert api.puts == []


def test_pilot_forced_to_floor_when_no_space_and_force_floor_on():
    # Same no-space disk, but force_floor=True → always seed the pilot at the floor (720, pid 11).
    df, stats, _ = _run(_ON_FORCE, free_gb=100)
    assert int(df.at[0, "pilot_last_profile_id"]) == 11
    assert stats["searched"] == 1
    assert stats.get("skipped_space", 0) == 0


# ── no cumulative reservation: every due stub searches against the SAME free space ──
def test_multiple_pilots_all_search_no_cumulative_throttle():
    # Two stub pilots, free 130 GB vs reserve 110 GB. A 2160p grab (~19.5 GB @100min) fits ONCE but
    # not twice cumulatively. The OLD running-decrement would skip the 2nd pilot for "no space"; with
    # the static per-pilot gate both see the same 130 GB free and both target 2160p. (This is the
    # real-run regression: 9,010/9,482 stubs were deferred at 7 TB free under the running decrement.)
    df = pd.DataFrame([
        {"series_id": 1, "series_title": "A", "is_pilot": True, "episode_file_id": None,
         "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None},
        {"series_id": 2, "series_title": "B", "is_pilot": True, "episode_file_id": None,
         "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None},
    ])

    class _MultiApi:
        def __init__(self):
            self.puts = []

        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "qualityprofile":
                return list(_PROFILES)
            if endpoint == "series" and method == "GET":
                return [{"id": 1, "qualityProfileId": 99, "runtime": 100, "title": "A"},
                        {"id": 2, "qualityProfileId": 99, "runtime": 100, "title": "B"}]
            if endpoint.startswith("series/") and method == "PUT":
                self.puts.append(payload.get("qualityProfileId"))
                return payload
            return fallback

    api = _MultiApi()
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = _ON
    m.dry_run = True
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 130.0
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda *a, **k: 999

    stats = m.run_pilot_search("inst")
    assert stats["searched"] == 2                          # BOTH searched — not throttled cumulatively
    assert stats.get("skipped_space", 0) == 0
    assert int(df.at[0, "pilot_last_profile_id"]) == 13    # both at the highest tier (2160p)
    assert int(df.at[1, "pilot_last_profile_id"]) == 13


# ── flag OFF: legacy floor-first (the parity escape hatch) ────────────────────────
def test_flag_off_reproduces_legacy_floor_first():
    # With the flag OFF, attempt 1 targets the FLOOR (720, pid 11) — the OPPOSITE of best-tier-first
    # (which targets 2160). This is the byte-identical legacy behavior.
    df, stats, _ = _run(_OFF, free_gb=5000)
    assert int(df.at[0, "pilot_last_profile_id"]) == 11
    assert stats["searched"] == 1

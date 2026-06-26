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
    def log_table(self, *a, **k): pass
    def log_grid(self, *a, **k): pass


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


# NOTE: the within-run floor-first climb is now the DEFAULT pilot strategy; these tests pin the
# LEGACY escape-hatch strategies, so they explicitly disable the climb. The climb itself is covered
# by test_pilot_climb_worker.py and test_climb_default_collects_items_and_spawns_worker below.
_CLIMB_OFF = {"pilot_floor_climb": {"enabled": False}}
_ON = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": True}, **_CLIMB_OFF}
_ON_FORCE = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": True, "force_floor": True}, **_CLIMB_OFF}
_OFF = {"free_space_limit": 100, "pilot_best_tier_first": {"enabled": False}, **_CLIMB_OFF}


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


# ── live mode: decision off the bulk snapshot, fresh GET only for the changers ──
def test_live_reads_snapshot_and_fetches_fresh_only_for_changers():
    """Regression: the live loop must NOT do a GET series/{sid} per stub (that was the multi-hour
    first-run crawl). The tier decision is read from the ONE bulk snapshot; a fresh per-series GET
    happens ONLY for a stub that actually changes profile, right before its PUT."""
    df = pd.DataFrame([
        # sid 1 already at the best tier (pid 13), never searched → stays put → NO change, NO GET.
        {"series_id": 1, "series_title": "A", "is_pilot": True, "episode_file_id": None,
         "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None},
        # sid 2 at an off-ladder profile (99) → best-tier targets 13 → changes → fresh GET + PUT.
        {"series_id": 2, "series_title": "B", "is_pilot": True, "episode_file_id": None,
         "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None},
    ])

    class _CountingApi:
        def __init__(self):
            self.get_by_id = []   # sids fetched via GET series/{sid}
            self.puts = []        # (sid, qp) PUTs
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "qualityprofile":
                return list(_PROFILES)
            if endpoint == "series" and method == "GET":      # the ONE bulk snapshot
                return [{"id": 1, "qualityProfileId": 13, "runtime": 100, "title": "A"},
                        {"id": 2, "qualityProfileId": 99, "runtime": 100, "title": "B"}]
            if endpoint.startswith("series/") and method == "GET":
                sid = int(endpoint.split("/", 1)[1])
                self.get_by_id.append(sid)
                return {"id": sid, "qualityProfileId": 99, "runtime": 100, "title": "X"}
            if endpoint.startswith("series/") and method == "PUT":
                self.puts.append((int(endpoint.split("/", 1)[1]), payload.get("qualityProfileId")))
                return payload
            return fallback   # "command" POST etc.

    api = _CountingApi()
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None          # → snapshot falls back to the ONE bulk /series GET
    m.global_cache = None
    m.config = _ON
    m.dry_run = False              # LIVE
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0     # ample → best tier 2160 (pid 13)
    m._get_total_space_gb = lambda inst: 1000.0
    m._get_episode_id = lambda *a, **k: 999

    stats = m.run_pilot_search("inst")

    assert stats["searched"] == 2                  # both still queued for EpisodeSearch
    assert api.get_by_id == [2]                    # fresh GET ONLY for the changer — NOT one per stub
    assert api.puts == [(2, 13)]                   # only sid 2 re-profiled, to the best tier


def test_bulk_live_loop_resolves_ids_cache_only():
    """Regression: in the bulk (use_tqdm) LIVE path the episode cache is pre-warmed concurrently,
    so the serial loop must resolve S01E01 ids CACHE-ONLY (allow_live=False). Allowing the live
    fallback re-introduced a per-stub episode?seriesId= GET (~1 s each) — the crawl the warm exists
    to kill. Small batches (no warm) keep the live fallback."""
    rows = [{"series_id": i, "series_title": f"S{i}", "is_pilot": True, "episode_file_id": None,
             "pilot_search_attempts": None, "pilot_last_searched_at": None, "pilot_last_profile_id": None}
            for i in range(1, 16)]   # 15 stubs > PROGRESS_BAR_THRESHOLD (10) → use_tqdm path
    df = pd.DataFrame(rows)

    class _Api:
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "qualityprofile":
                return list(_PROFILES)
            if endpoint == "series" and method == "GET":   # bulk snapshot (all already at best tier)
                return [{"id": i, "qualityProfileId": 13, "runtime": 100, "title": f"S{i}"} for i in range(1, 16)]
            return fallback

    captured_allow_live = []
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = _Api()
    m.sonarr_cache = None
    m.global_cache = None
    m.config = _ON
    m.dry_run = False
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_free_space_gb = lambda inst: 5000.0
    m._get_total_space_gb = lambda inst: 1000.0
    m._prewarm_by_series_episode_cache = lambda *a, **k: 0   # warm stubbed (no real API in the loop)

    def _spy_get_ep(instance, sid, sn, en, **k):
        captured_allow_live.append(k.get("allow_live"))
        return 9000 + sid
    m._get_episode_id = _spy_get_ep

    m.run_pilot_search("inst")

    assert captured_allow_live, "loop never resolved an episode id"
    assert all(al is False for al in captured_allow_live), captured_allow_live   # cache-only, no live fallback


# ── flag OFF: legacy floor-first (the parity escape hatch) ────────────────────────
def test_flag_off_reproduces_legacy_floor_first():
    # With the flag OFF, attempt 1 targets the FLOOR (720, pid 11) — the OPPOSITE of best-tier-first
    # (which targets 2160). This is the byte-identical legacy behavior.
    df, stats, _ = _run(_OFF, free_gb=5000)
    assert int(df.at[0, "pilot_last_profile_id"]) == 11
    assert stats["searched"] == 1


# ── DEFAULT: interactive search (one manual search per stub) ──────────────────────
def test_interactive_default_spawns_interactive_worker():
    """By default (no pilot_interactive key → enabled), the main loop resolves S01E01 and hands it to
    the BACKGROUND interactive worker with the ascending ladder + per-series meta; it does NO profile
    flips itself (the worker owns the grab). The worker is stubbed to capture its arguments."""
    df = _stub_df()
    api = _FakeApi(series_qp=99)
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100}          # no pilot_interactive key → default ON
    m.dry_run = False                              # LIVE so the worker is actually spawned
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: 999

    spawned: dict = {}
    m._spawn_pilot_interactive_worker = lambda inst, items, ladder, meta, idx, floor, cd, **k: spawned.update(
        instance=inst, items=list(items), ladder=list(ladder), meta=dict(meta)
    )

    stats = m.run_pilot_search("inst")

    assert spawned["items"] == [(1, 999)]                       # (sid, s01e01 id) handed to worker
    assert [res for _pid, res in spawned["ladder"]] == [720, 1080, 2160]
    assert spawned["meta"][1]["title"] == "S"                   # title threaded for label + ledger
    assert api.puts == []                                       # main thread flips NO profiles
    assert stats["searched"] == 1


def test_removes_stub_with_committed_grab():
    """A pilot whose S01E01 is already in the download queue (grab committed) is REMOVED from the df:
    the one queue/details fetch surfaces episodeId 999, the stub row is dropped + the trimmed frame
    saved, and the interactive worker is never handed it. The pilot cache rebuild re-creates the stub
    if the download later fails (still file-less), so removal is safe."""
    df = _stub_df()

    class _DlApi(_FakeApi):
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "queue/details":
                return [{"episodeId": 999}]           # S01E01 (id 999) is mid-download
            return _FakeApi._make_request(self, instance, endpoint, method, payload, fallback)

    api = _DlApi(series_qp=99)
    saved: dict = {}
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100}              # default interactive
    m.dry_run = False
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: saved.update(df=d)
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: 999           # resolves to the downloading episode

    spawned: dict = {"n": 0}
    m._spawn_pilot_interactive_worker = lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)

    stats = m.run_pilot_search("inst")

    assert stats["removed_grabbing"] == 1             # surfaced from the queue
    assert spawned["n"] == 0                           # worker never spawned — nothing to search
    assert stats["searched"] == 0
    assert len(saved["df"]) == 0                       # the committed-grab stub was dropped + persisted


def test_dry_run_does_not_remove_committed_grab():
    """Dry-run counts the committed-grab stub but never drops the row or saves."""
    df = _stub_df()

    class _DlApi(_FakeApi):
        def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
            if endpoint == "queue/details":
                return [{"episodeId": 999}]
            return _FakeApi._make_request(self, instance, endpoint, method, payload, fallback)

    saved: list = []
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = _DlApi(series_qp=99)
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100}
    m.dry_run = True
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: saved.append(True)
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: 999

    stats = m.run_pilot_search("inst")

    assert stats["removed_grabbing"] == 1             # planned…
    assert saved == []                                 # …but never persisted in dry-run
    assert len(df) == 1                                # row left intact


def test_climb_default_collects_items_and_spawns_worker():
    """With interactive search OFF, the default falls back to the BACKGROUND climb worker: the main
    loop resolves S01E01 and hands it the ascending floor→widest ladder, doing NO profile flips
    itself (the worker owns those). The worker is stubbed to capture its arguments."""
    df = _stub_df()
    api = _FakeApi(series_qp=99)
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100, "pilot_interactive": {"enabled": False}}  # climb fallback
    m.dry_run = False                              # LIVE so the worker is actually spawned
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: 999

    spawned: dict = {}
    m._spawn_pilot_climb_worker = lambda inst, items, ladder: spawned.update(
        instance=inst, items=list(items), ladder=list(ladder)
    )

    stats = m.run_pilot_search("inst")

    assert spawned["items"] == [(1, 999)]                       # (sid, s01e01 id) handed to worker
    # Ascending floor→widest ladder, one rung per resolution tier (720 → 1080 → 2160).
    assert [pid for pid, _res in spawned["ladder"]] == [11, 12, 13]
    assert [res for _pid, res in spawned["ladder"]] == [720, 1080, 2160]
    assert api.puts == []                                       # main thread flips NO profiles
    assert stats["searched"] == 1
    # interval guard tracking stamped at the floor tier (so the 24 h guard works next run)
    assert int(df.at[0, "pilot_last_profile_id"]) == 11


def test_climb_unresolved_id_defers_instead_of_series_search():
    """When S01E01's id can't be resolved (cache AND live miss), the stub is DEFERRED — never a
    whole-series SeriesSearch (which would over-grab every monitored episode at the floor)."""
    df = _stub_df()
    api = _FakeApi(series_qp=99)
    posts: list = []
    _orig = api._make_request
    def _track(instance, endpoint, method="GET", payload=None, fallback=None):
        if endpoint == "command" and method == "POST":
            posts.append(payload)                     # a SeriesSearch here would be the over-grab
        return _orig(instance, endpoint, method, payload, fallback)
    api._make_request = _track

    spawned: list = []
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100, "pilot_floor_climb": {"enabled": True}}
    m.dry_run = False
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: None
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: None          # never resolves, even live
    m._spawn_pilot_climb_worker = lambda *a, **k: spawned.append(True)

    stats = m.run_pilot_search("inst")

    assert posts == []            # NO command POST at all — no whole-series SeriesSearch
    assert spawned == []          # nothing climbable
    assert stats["searched"] == 0


def test_climb_dry_run_does_not_spawn_or_write():
    """Dry-run climb plans but never spawns the worker, PUTs a profile, or saves the df."""
    df = _stub_df()
    api = _FakeApi(series_qp=99)
    saved: list = []
    spawned: list = []
    m = SonarrCacheEpisodeFilesManager.__new__(SonarrCacheEpisodeFilesManager)
    m.logger = _StubLogger()
    m.sonarr_api = api
    m.sonarr_cache = None
    m.global_cache = None
    m.config = {"free_space_limit": 100, "pilot_floor_climb": {"enabled": True}}
    m.dry_run = True
    m._resolve_instance = lambda inst: inst
    m.load = lambda inst: df
    m.save = lambda inst, d: saved.append(True)
    m._measured_mb_per_min = lambda d: dict(_MEASURED)
    m._get_episode_id = lambda *a, **k: 999
    m._spawn_pilot_climb_worker = lambda *a, **k: spawned.append(True)

    stats = m.run_pilot_search("inst")

    assert spawned == []          # no background worker in dry-run
    assert saved == []            # df not persisted in dry-run
    assert api.puts == []         # no profile writes
    assert stats["searched"] == 1

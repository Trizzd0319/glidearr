"""SonarrCacheEpisodeFilesManager.remediate_size_anomalies — RefreshSeries-rescans mis-graded /
undersized episodes and re-grabs bloated ones, gated by the remediate flag + dry_run + the backup
gate, and refusing to delete unmonitored OR whole-file-guarded (pilot/keep/recent-air) episodes."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.backup import GATE_KEY
from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_error(self, *a, **k): pass


class _API:
    def __init__(self, eps_by_series):
        self.eps_by_series = eps_by_series
        self.calls = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method, payload))
        if endpoint.startswith("episode?seriesId="):
            return self.eps_by_series.get(int(endpoint.split("=")[1]), [])
        return {}


class _GC:
    def __init__(self, gate=None): self.d = {GATE_KEY: gate} if gate is not None else {}
    def get(self, k, default=None): return self.d.get(k, default)


# series_id -> episodes (id/coord/monitored as Sonarr's /episode returns them)
_EPS = {
    2: [{"id": 200, "seasonNumber": 1, "episodeNumber": 2, "monitored": True}],
    3: [{"id": 300, "seasonNumber": 2, "episodeNumber": 5, "monitored": False}],
    4: [{"id": 400, "seasonNumber": 1, "episodeNumber": 1, "monitored": True}],
}

_ROWS = [
    {"action": "rescan", "series_id": 1, "series_title": "MisGraded",
     "season_number": 1, "episode_number": 1},
    {"action": "regrab", "series_id": 2, "episode_file_id": 20, "series_title": "Bloated",
     "season_number": 1, "episode_number": 2, "size_gb": 30.0, "quality_name": "Bluray-1080p",
     "reclaim_gb": 25.0},
    {"action": "regrab", "series_id": 3, "episode_file_id": 30, "series_title": "Unmonitored",
     "season_number": 2, "episode_number": 5},
    {"action": "regrab", "series_id": 4, "episode_file_id": 40, "series_title": "Guarded",
     "season_number": 1, "episode_number": 1},
]


def _mgr(remediate, dry_run, gate=None, protected=frozenset({40})):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = {"size_anomaly": {"remediate": remediate}}
    m.dry_run = dry_run
    m.logger = _Log()
    m.sonarr_api = _API(_EPS)
    m.global_cache = _GC(gate)
    m._resolve_instance = lambda i: i or "standard"
    # df only needs to be non-empty for the guard build; the guard set itself is stubbed.
    m.load = lambda inst: _NonEmptyDF()
    m._build_protected_file_ids = lambda df, now: protected
    return m


class _NonEmptyDF:
    empty = False


def _writes(api):
    return [c for c in api.calls if c[1] in ("POST", "DELETE")]


def test_real_armed_run_rescans_and_regrabs_monitored_unguarded_only():
    m = _mgr(remediate=True, dry_run=False)                       # gate unset → armed
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats == {"rescanned": 1, "regrabbed": 1, "skipped_unmonitored": 1,
                     "skipped_guard": 1, "failed": 0}
    calls = m.sonarr_api.calls
    assert ("command", "POST", {"name": "RefreshSeries", "seriesId": 1}) in calls     # rescan
    assert ("episodefile/20", "DELETE", None) in calls                                # delete bloated
    assert ("command", "POST", {"name": "EpisodeSearch", "episodeIds": [200]}) in calls  # re-search
    assert all("episodefile/30" not in c[0] for c in calls)      # unmonitored never deleted
    assert all("episodefile/40" not in c[0] for c in calls)      # guarded never deleted


def test_dry_run_makes_no_writes():
    m = _mgr(remediate=True, dry_run=True)
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.sonarr_api) == []                           # only the episode GET, no POST/DELETE


def test_disarmed_backup_gate_degrades_to_dry_run():
    # Real run, but the backup gate is DISARMED (a real backup failed) → no destructive writes.
    m = _mgr(remediate=True, dry_run=False, gate={"armed": False})
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.sonarr_api) == []


def test_guard_build_failure_skips_all_regrabs():
    m = _mgr(remediate=True, dry_run=False)
    def _boom(df, now): raise RuntimeError("guard build failed")
    m._build_protected_file_ids = _boom
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["regrabbed"] == 0 and stats["skipped_guard"] == 3   # all 3 regrab rows skipped
    assert all("episodefile/" not in c[0] for c in m.sonarr_api.calls)  # nothing deleted


def test_remediate_flag_off_is_noop():
    m = _mgr(remediate=False, dry_run=False)
    assert m.remediate_size_anomalies("standard", _ROWS) == {}
    assert m.sonarr_api.calls == []


def _omnibus_mgr(eps, df):
    m = object.__new__(SonarrCacheEpisodeFilesManager)
    m.config = {"size_anomaly": {"remediate": True}}
    m.dry_run = False
    m.logger = _Log()
    m.sonarr_api = _API(eps)
    m.global_cache = _GC()                                    # gate unset → armed
    m._resolve_instance = lambda i: i or "standard"
    m.load = lambda inst: df
    m._build_protected_file_ids = lambda d, now: frozenset()  # nothing pre-guarded
    return m


def test_multi_episode_file_with_unmonitored_sibling_is_not_deleted():
    # One physical file (fid 50) backs S01E02 (monitored, the anomaly row) AND S01E03 (UNMONITORED).
    # Deleting it would orphan E03 with nothing to re-grab it → the whole file must be SKIPPED.
    eps = {5: [{"id": 502, "seasonNumber": 1, "episodeNumber": 2, "monitored": True},
               {"id": 503, "seasonNumber": 1, "episodeNumber": 3, "monitored": False}]}
    df = pd.DataFrame([{"episode_file_id": 50, "season_number": 1, "episode_number": 2},
                       {"episode_file_id": 50, "season_number": 1, "episode_number": 3}])
    m = _omnibus_mgr(eps, df)
    rows = [{"action": "regrab", "series_id": 5, "episode_file_id": 50, "series_title": "Omnibus",
             "season_number": 1, "episode_number": 2, "size_gb": 30.0, "quality_name": "Bluray-1080p"}]
    stats = m.remediate_size_anomalies("standard", rows)
    assert stats["regrabbed"] == 0 and stats["skipped_unmonitored"] == 1
    assert all("episodefile/50" not in c[0] for c in m.sonarr_api.calls)   # file preserved, no orphaning


def test_multi_episode_file_all_monitored_searches_every_episode_once():
    # Same omnibus but BOTH episodes monitored → safe to replace; deleted ONCE (deduped), EpisodeSearch
    # covers BOTH episode ids so the whole multi-ep file is re-acquired.
    eps = {6: [{"id": 602, "seasonNumber": 1, "episodeNumber": 2, "monitored": True},
               {"id": 603, "seasonNumber": 1, "episodeNumber": 3, "monitored": True}]}
    df = pd.DataFrame([{"episode_file_id": 60, "season_number": 1, "episode_number": 2},
                       {"episode_file_id": 60, "season_number": 1, "episode_number": 3}])
    m = _omnibus_mgr(eps, df)
    rows = [{"action": "regrab", "series_id": 6, "episode_file_id": 60, "series_title": "Omnibus",
             "season_number": 1, "episode_number": n, "size_gb": 30.0, "quality_name": "Bluray-1080p"}
            for n in (2, 3)]                                  # two anomaly rows sharing fid 60
    stats = m.remediate_size_anomalies("standard", rows)
    assert stats["regrabbed"] == 1                            # ONE file, deleted once (deduped)
    assert ("episodefile/60", "DELETE", None) in m.sonarr_api.calls
    searches = [c for c in m.sonarr_api.calls
                if isinstance(c[2], dict) and c[2].get("name") == "EpisodeSearch"]
    assert len(searches) == 1 and sorted(searches[0][2]["episodeIds"]) == [602, 603]

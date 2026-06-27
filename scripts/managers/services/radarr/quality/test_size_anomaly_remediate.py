"""RadarrSpacePressureManager.remediate_size_anomalies — rescans mis-graded movies and re-grabs
bloated ones by ACQUIRING a same-resolution, right-sized release first (Radarr replaces the old file
on import — never delete-first), gated by the remediate flag + dry_run + the backup gate."""
from __future__ import annotations

from scripts.managers.services.backup import GATE_KEY
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager

_GB = 1024 ** 3


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _API:
    def __init__(self, movies, releases):
        self.movies = movies
        self.releases = releases
        self.calls = []

    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method, payload))
        if endpoint.startswith("movie/"):
            return self.movies.get(int(endpoint.split("/")[1]))
        if endpoint.startswith("release?movieId="):
            return self.releases.get(int(endpoint.split("=")[1]), [])
        return {}


class _GC:
    def __init__(self, gate=None): self.d = {GATE_KEY: gate} if gate is not None else {}
    def get(self, k, default=None): return self.d.get(k, default)


def _rel(guid, res, size_gb, seeders=10, rejections=None):
    return {"guid": guid, "indexerId": 1, "title": guid, "seeders": seeders,
            "size": int(size_gb * _GB), "quality": {"quality": {"resolution": res}},
            "rejections": rejections or []}


# Movie 2: bloated 2160p (expected ~15 GB). Candidate releases:
#   right  → 14 GB 2160p  (in-band, closest to expected) ← should win
#   bloat  → 48 GB 2160p  (48/15 = 3.2× ≥ over_ratio 3.0 → still oversized, excluded)
#   hd1080 → 8 GB 1080p   (wrong resolution, excluded)
#   tiny   → 1 GB 2160p   (1/15 < under_ratio band floor 4.5 GB → fake/undersized, excluded)
#   near   → 20 GB 2160p  (in-band but farther from 15 than 'right')
_RELEASES = {2: [
    _rel("right", 2160, 14.0, seeders=50),
    _rel("bloat", 2160, 48.0, seeders=99),
    _rel("hd1080", 1080, 8.0, seeders=80),
    _rel("tiny", 2160, 1.0, seeders=5),
    _rel("near", 2160, 20.0, seeders=70),
]}

_MOVIES = {
    2: {"monitored": True, "movieFile": {"quality": {"quality": {"resolution": 2160}}}},
    3: {"monitored": False},
}

_ROWS = [
    {"action": "rescan", "movie_id": 1, "title": "MisGraded"},
    {"action": "regrab", "movie_id": 2, "movie_file_id": 20, "title": "Bloated",
     "size_gb": 49.0, "expected_gb": 15.0, "quality_name": "Bluray-2160p", "reclaim_gb": 34.0},
    {"action": "regrab", "movie_id": 3, "movie_file_id": 30, "title": "Unmonitored",
     "size_gb": 49.0, "expected_gb": 15.0, "quality_name": "Bluray-2160p", "reclaim_gb": 34.0},
]


def _mgr(remediate, dry_run, movies=_MOVIES, releases=_RELEASES, gate=None):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {"size_anomaly": {"remediate": remediate}}
    m.dry_run = dry_run
    m.logger = _Log()
    m.radarr_api = _API(movies, releases)
    m.global_cache = _GC(gate)
    m._resolve_instance = lambda i: i or "standard"
    return m


def _writes(api):
    return [c for c in api.calls if c[1] in ("POST", "DELETE")]


def test_real_armed_run_rescans_and_regrabs_by_acquiring_right_sized_release():
    m = _mgr(remediate=True, dry_run=False)                       # gate unset → armed
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats == {"rescanned": 1, "regrabbed": 1, "skipped_unmonitored": 1,
                     "skipped_no_release": 0, "failed": 0}
    calls = m.radarr_api.calls
    assert ("command", "POST", {"name": "RefreshMovie", "movieIds": [1]}) in calls   # rescan
    assert ("release", "POST", {"guid": "right", "indexerId": 1}) in calls           # acquire the 14 GB one
    assert all(c[0] != "moviefile/20" for c in calls)            # NEVER delete-first
    assert all(c[1] != "DELETE" for c in calls)                  # nothing deleted by us
    assert all(p != {"name": "MoviesSearch", "movieIds": [2]}    # no blind re-acquire
               for _e, _m, p in calls if isinstance(p, dict))
    assert all("release?movieId=3" not in c[0] for c in calls)   # unmonitored never even searched


def test_picks_release_closest_to_expected_size():
    pick = RadarrSpacePressureManager._pick_replacement_release(
        _RELEASES[2], expected_gb=15.0, resolution=2160, under_ratio=0.3, over_ratio=3.0)
    assert pick["guid"] == "right"      # 14 GB beats 20 GB ('near'); 48/1/8 GB all excluded


def test_no_right_sized_release_keeps_bloated_file():
    # Only an oversized remux and a wrong-resolution release exist → keep the file, grab nothing.
    rel = {2: [_rel("bloat", 2160, 48.0), _rel("hd1080", 1080, 8.0)]}
    m = _mgr(remediate=True, dry_run=False, releases=rel)
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["regrabbed"] == 0 and stats["skipped_no_release"] == 1
    assert all(c[0] != "release" or c[1] != "POST" for c in m.radarr_api.calls)   # no grab


def test_dry_run_searches_but_makes_no_writes():
    m = _mgr(remediate=True, dry_run=True)
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.radarr_api) == []                           # GET movie + GET release only, no POST/DELETE


def test_disarmed_backup_gate_degrades_to_dry_run():
    m = _mgr(remediate=True, dry_run=False, gate={"armed": False})
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.radarr_api) == []


def test_remediate_flag_off_is_noop():
    m = _mgr(remediate=False, dry_run=False)
    assert m.remediate_size_anomalies("standard", _ROWS) == {}
    assert m.radarr_api.calls == []

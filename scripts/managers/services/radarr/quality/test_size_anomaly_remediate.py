"""RadarrSpacePressureManager.remediate_size_anomalies — rescans mis-graded movies and
re-grabs bloated ones, gated by the remediate flag + dry_run + the backup gate."""
from __future__ import annotations

from scripts.managers.services.backup import GATE_KEY
from scripts.managers.services.radarr.quality.space_pressure import RadarrSpacePressureManager


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _API:
    def __init__(self, movies): self.movies = movies; self.calls = []
    def _make_request(self, instance, endpoint, method="GET", payload=None, fallback=None):
        self.calls.append((endpoint, method, payload))
        if endpoint.startswith("movie/"):
            return self.movies.get(int(endpoint.split("/")[1]))
        return {}


class _GC:
    def __init__(self, gate=None): self.d = {GATE_KEY: gate} if gate is not None else {}
    def get(self, k, default=None): return self.d.get(k, default)


def _mgr(remediate, dry_run, movies, gate=None):
    m = object.__new__(RadarrSpacePressureManager)
    m.config = {"size_anomaly": {"remediate": remediate}}
    m.dry_run = dry_run
    m.logger = _Log()
    m.radarr_api = _API(movies)
    m.global_cache = _GC(gate)
    m._resolve_instance = lambda i: i or "standard"
    return m


_ROWS = [
    {"action": "rescan", "movie_id": 1, "title": "MisGraded"},
    {"action": "regrab", "movie_id": 2, "movie_file_id": 20, "title": "Bloated",
     "size_gb": 45.0, "quality_name": "Bluray-720p", "reclaim_gb": 38.0},
    {"action": "regrab", "movie_id": 3, "movie_file_id": 30, "title": "Unmonitored"},
]
_MOVIES = {2: {"monitored": True}, 3: {"monitored": False}}


def _writes(api):
    return [c for c in api.calls if c[1] in ("POST", "DELETE")]


def test_real_armed_run_rescans_and_regrabs_monitored_only():
    m = _mgr(remediate=True, dry_run=False, movies=_MOVIES)        # gate unset → armed
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats == {"rescanned": 1, "regrabbed": 1, "skipped_unmonitored": 1, "failed": 0}
    eps = m.radarr_api.calls
    assert ("command", "POST", {"name": "RefreshMovie", "movieIds": [1]}) in eps   # rescan
    assert ("moviefile/20", "DELETE", None) in eps                                 # delete bloated file
    assert ("command", "POST", {"name": "MoviesSearch", "movieIds": [2]}) in eps   # re-search
    assert all("moviefile/30" not in c[0] for c in eps)          # unmonitored never deleted


def test_dry_run_makes_no_writes():
    m = _mgr(remediate=True, dry_run=True, movies=_MOVIES)
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.radarr_api) == []                            # only read GETs, no POST/DELETE


def test_disarmed_backup_gate_degrades_to_dry_run():
    # Real run, but the backup gate is DISARMED (a real backup failed) → no destructive writes.
    m = _mgr(remediate=True, dry_run=False, movies=_MOVIES, gate={"armed": False})
    stats = m.remediate_size_anomalies("standard", _ROWS)
    assert stats["rescanned"] == 0 and stats["regrabbed"] == 0
    assert _writes(m.radarr_api) == []


def test_remediate_flag_off_is_noop():
    m = _mgr(remediate=False, dry_run=False, movies=_MOVIES)
    assert m.remediate_size_anomalies("standard", _ROWS) == {}
    assert m.radarr_api.calls == []

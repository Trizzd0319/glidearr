"""FORK-D integration tests — coordinator rehome-then-evict-then-readd flow through run().

Adversarial invariants:
  • NO 4K touch in the SAME run as the rehome (INV-1: no no-copy gap).
  • The 4K is evicted (file deleted + movie UNMONITORED, record kept) ONLY after the standard
    copy is confirmed imported (cross-run), and the entry moves to the space-evicted ledger.
  • Eviction is TEMPORARY: once free recovers above U*(1+margin) the 4K is re-added (re-monitor +
    search) → dual-version; below that margin it holds (anti-thrash).
  • Import-timeout drops the pending entry while KEEPING the 4K; queued_at is preserved on re-queue.
  • dry-run / disarmed-backup-gate writes nothing; gate-off is a complete no-op.
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.backup import GATE_KEY
from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager as C

_GB = 1024 ** 3
_PROFILES = [{"id": 3, "name": "HD-720p"}, {"id": 4, "name": "HD-1080p"},
             {"id": 8, "name": "Remux+WEB-1080p"}, {"id": 5, "name": "Ultra-HD"}]
_PKEY = "radarr/uhd/pending_4k_evicts"
_EKEY = "radarr/uhd/space_evicted_4k"


class _Log:
    def log_info(self, m): pass
    def log_warning(self, m): pass
    def log_debug(self, m): pass
    def log_error(self, m): pass
    def log_success(self, m): pass


class _Api:
    def __init__(self, free, total, instances):
        self._f, self._t, self._i = free, total, instances
        self.deletes: list = []          # (inst, endpoint) DELETE
        self.puts: list = []             # (inst, endpoint, payload) PUT
        self.commands: list = []         # (inst, endpoint, payload) POST
    def disk_free_gb(self, inst): return self._f
    def disk_total_gb(self, inst): return self._t
    def get_all_radarr_apis(self): return {n: None for n in self._i}
    def _make_request(self, inst, endpoint, method=None, fallback=None, payload=None, **k):
        if endpoint == "qualityprofile":
            return _PROFILES
        if method == "DELETE":
            self.deletes.append((inst, endpoint)); return {"ok": True}
        if method == "PUT":
            self.puts.append((inst, endpoint, payload)); return {"ok": True}
        if method == "POST":
            self.commands.append((inst, endpoint, payload)); return {"ok": True}
        return fallback


class _RadarrSP:
    def __init__(self, dfs, *, free, total, instances):
        self._dfs = dfs
        self._free = free
        self.radarr_api = _Api(free, total, instances)
    def _get_free_space_gb(self, inst): return self._free
    def _resolve_instance(self, x): return "standard"
    def run_downgrades(self, inst, free): return {}
    def load_movie_files(self, inst): return self._dfs.get(inst)
    def build_delete_candidates(self, inst, df, *, ignore_score_ceiling=False):
        out = []
        if df is None:
            return out
        for idx, row in df.iterrows():
            score = int(row["watchability_score"])
            if not ignore_score_ceiling and score >= 20:
                continue
            out.append({
                "service": "movie", "tier": 1, "score": score, "critic": None,
                "size_bytes": float(row["size_bytes"]), "size_gb": float(row["size_bytes"]) / _GB,
                "idx": idx, "fid": int(row["movie_file_id"]), "tmdb_id": int(row["tmdb_id"]),
                "resolution": (int(row["resolution"]) if "resolution" in df.columns and pd.notna(row["resolution"]) else None),
                "title": row.get("title", "x"),
            })
        return out
    def delete_selected_movie_files(self, inst, df, picks):
        return {"deleted": len(picks)}


class _Acq:
    def __init__(self, action="added"):
        self.action = action
        self.calls: list = []
    def rehome_to_standard(self, tmdb, *, std_inst, target_profile_id, **k):
        self.calls.append((tmdb, std_inst, target_profile_id))
        return {"action": self.action, "title": f"t{tmdb}", "standard_id": 555}


class _Restore:
    def restore_recovered_deletions(self, inst): return {}


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v): self.d[k] = v


class _Reg:
    def __init__(self, m): self._m = m
    def get(self, kind, key): return self._m.get(key)


def _coord(dfs, *, free=5400.0, dry_run=False, flag=True, acq_action="added", cache=None):
    c = C.__new__(C)
    c.config = {
        "space_coordinator_enabled": True, "deletions_consent": True, "free_space_limit": 5500,
        "radarr_instances": {"standard": {}, "uhd": {}, "default_instance": "standard"},
        "radarr_instances_categorized": {"4K": "uhd"},
        "routing": {"movies": {"rehome_4k_only": flag, "rehome_import_timeout_days": 7,
                               "rehome_readd_margin": 0.25}},
    }
    c.logger, c.dry_run = _Log(), dry_run
    c.global_cache = cache if cache is not None else _Cache()
    sp = _RadarrSP(dfs, free=free, total=30000.0, instances=["standard", "uhd"])
    acq = _Acq(action=acq_action)
    c.registry = _Reg({
        "RadarrSpacePressureManager": sp, "SonarrSpacePressureManager": None,
        "SonarrCacheEpisodeFilesManager": None, "RadarrRepairAnomalyManager": _Restore(),
        "AcquisitionManager": acq,
    })
    return c, sp, acq


def _std_df(with9=False):
    rows = [{"tmdb_id": 1, "movie_id": 100, "movie_file_id": 11, "watchability_score": 90,
             "size_bytes": 5 * _GB, "title": "Keep"}]
    if with9:                                   # the rehomed standard copy has imported (a real file)
        rows.append({"tmdb_id": 9, "movie_id": 109, "movie_file_id": 19, "watchability_score": 90,
                     "size_bytes": 4 * _GB, "title": "Rehomed"})
    return pd.DataFrame(rows)


def _uhd_df():
    # tmdb 9: a 2160p copy on the 4K instance with NO baseline on standard → a 4K-only film.
    return pd.DataFrame([{"tmdb_id": 9, "movie_id": 900, "movie_file_id": 99, "watchability_score": 5,
                          "resolution": 2160, "size_bytes": 70 * _GB, "title": "Orphan 4K"}])


def _uhd_puts(sp):  return [(e, pl) for (i, e, pl) in sp.radarr_api.puts if i == "uhd"]
def _uhd_cmds(sp):  return [(e, pl) for (i, e, pl) in sp.radarr_api.commands if i == "uhd"]


def test_run1_rehomes_and_does_not_touch_4k():
    cache = _Cache()
    c, sp, acq = _coord({"standard": _std_df(), "uhd": _uhd_df()}, cache=cache)
    c.run()
    assert acq.calls == [(9, "standard", 3)]              # cold 4K-only → standard @ 720p (sub-4K)
    assert sp.radarr_api.deletes == [] and _uhd_puts(sp) == []   # INV-1: 4K untouched in the rehome run
    led = cache.get(_PKEY)
    assert led and led["9"]["uhd_movie_id"] == 900 and led["9"]["uhd_file_id"] == 99


def test_4k_evicted_to_space_ledger_only_after_import():
    cache = _Cache()
    dfs = {"standard": _std_df(with9=False), "uhd": _uhd_df()}
    c1, sp1, _ = _coord(dfs, cache=cache)
    c1.run()
    assert sp1.radarr_api.deletes == []                   # run 1: rehome only, 4K untouched

    dfs["standard"] = _std_df(with9=True)                 # the standard copy imported
    c2, sp2, _ = _coord(dfs, cache=cache)                 # same cache → ledgers persist across runs
    c2.run()
    # 4K FILE deleted + movie UNMONITORED (record KEPT, not a whole-record delete)
    assert ("uhd", "moviefile/99") in sp2.radarr_api.deletes
    assert any(e == "movie/editor" and (pl or {}).get("monitored") is False for e, pl in _uhd_puts(sp2))
    assert not any("?deleteFiles=true" in e for _i, e in sp2.radarr_api.deletes)
    # entry moved pending → space-evicted (awaiting re-add)
    assert "9" not in (cache.get(_PKEY) or {})
    se = cache.get(_EKEY)
    assert se and se["9"]["uhd_movie_id"] == 900


def test_readd_4k_when_space_recovers():
    cache = _Cache()
    cache.set(_EKEY, {"9": {"std_inst": "standard", "uhd_movie_id": 900,
                            "size_bytes": 70 * _GB, "evicted_at": "2026-01-01T00:00:00+00:00"}})
    # free 9000 >= U*1.25 (6050*1.25 = 7562) → re-add the evicted 4K (re-monitor + search)
    c, sp, _ = _coord({"standard": _std_df(with9=True), "uhd": pd.DataFrame()}, free=9000.0, cache=cache)
    c.run()
    assert any(e == "movie/editor" and (pl or {}).get("monitored") is True for e, pl in _uhd_puts(sp))
    assert any(e == "command" and (pl or {}).get("name") == "MoviesSearch" for e, pl in _uhd_cmds(sp))
    assert "9" not in (cache.get(_EKEY) or {})            # ledger drained


def test_readd_holds_until_comfortable():
    cache = _Cache()
    cache.set(_EKEY, {"9": {"std_inst": "standard", "uhd_movie_id": 900,
                            "size_bytes": 70 * _GB, "evicted_at": "2026-01-01T00:00:00+00:00"}})
    # free 7000: above U (6050) but below the U*1.25 margin → must HOLD (anti-thrash)
    c, sp, _ = _coord({"standard": _std_df(), "uhd": pd.DataFrame()}, free=7000.0, cache=cache)
    c.run()
    assert _uhd_cmds(sp) == []                            # no re-add search
    assert "9" in (cache.get(_EKEY) or {})               # entry retained


def test_timeout_drops_entry_keeps_4k():
    cache = _Cache()
    cache.set(_PKEY, {"9": {"std_inst": "standard", "uhd_movie_id": 900, "uhd_file_id": 99,
                            "size_bytes": 70 * _GB, "queued_at": "2020-01-01T00:00:00+00:00"}})
    c, sp, _ = _coord({"standard": _std_df(with9=False), "uhd": _uhd_df()}, cache=cache)
    stats = c._execute_pending_uhd_evicts(sp, "standard", "uhd")
    assert stats["timed_out"] == 1 and stats["evicted"] == 0
    assert sp.radarr_api.deletes == []                    # 4K KEPT (never lose a copy on timeout)
    assert "9" not in cache.get(_PKEY)


def test_register_preserves_queued_at():
    # Re-queuing the same tmdb must NOT reset queued_at, or the timeout clock restarts forever.
    cache = _Cache()
    c, sp, _ = _coord({"standard": _std_df(), "uhd": _uhd_df()}, cache=cache)
    c._register_pending_uhd_evict("uhd", 9, 900, 99, "standard", 70 * _GB)
    first = cache.get(_PKEY)["9"]["queued_at"]
    c._register_pending_uhd_evict("uhd", 9, 900, 99, "standard", 70 * _GB)
    assert cache.get(_PKEY)["9"]["queued_at"] == first


def test_register_skips_when_backup_gate_disarmed():
    # A real run whose backup pre-flight failed degrades to dry-run → no ledger arm (INV-4).
    cache = _Cache()
    cache.set(GATE_KEY, {"armed": False})
    c, sp, _ = _coord({"standard": _std_df(), "uhd": _uhd_df()}, dry_run=False, cache=cache)
    c._register_pending_uhd_evict("uhd", 9, 900, 99, "standard", 70 * _GB)
    assert cache.get(_PKEY) in (None, {})


def test_dry_run_writes_nothing():
    cache = _Cache()
    c, sp, acq = _coord({"standard": _std_df(), "uhd": _uhd_df()},
                        dry_run=True, acq_action="would-add", cache=cache)
    c.run()
    assert cache.get(_PKEY) in (None, {})                 # no ledger armed
    assert sp.radarr_api.deletes == [] and _uhd_puts(sp) == []


def test_gate_off_is_noop():
    cache = _Cache()
    c, sp, acq = _coord({"standard": _std_df(), "uhd": _uhd_df()}, flag=False, cache=cache)
    out = c.run()
    assert acq.calls == [] and sp.radarr_api.deletes == [] and _uhd_puts(sp) == []
    assert cache.get(_PKEY) is None and cache.get(_EKEY) is None
    assert "pending_4k_evicts" not in out and "readd_4k" not in out

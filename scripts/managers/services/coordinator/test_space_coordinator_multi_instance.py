"""Multi-instance tests for SpaceCoordinatorManager.run().

Proves the Step-2..6 behaviour: the coordinator pools + deletes whole-title candidates
across EVERY Radarr instance on the shared mount (default + extra non-4K instances),
routes each delete back to its origin instance, EXCLUDES the config-labeled 4K instance
from whole-title deletion (rehome-then-evict is the pending 4K path), restores every
instance that deleted, and isolates a per-instance load failure. Drives the real run()
with fake managers (object.__new__ bypasses the heavy __init__/registry)."""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager as C

_GB = 1024 ** 3


class _Log:
    def log_info(self, m): pass
    def log_warning(self, m): pass
    def log_debug(self, m): pass
    def log_error(self, m): pass


class _Api:
    def __init__(self, free, total, instances): self._f, self._t, self._i = free, total, instances
    def disk_free_gb(self, inst): return self._f
    def disk_total_gb(self, inst): return self._t
    def get_all_radarr_apis(self): return {n: None for n in self._i}


class _RadarrSP:
    def __init__(self, dfs, *, free, total):
        self._dfs = dfs
        self._free = free
        self.radarr_api = _Api(free, total, list(dfs.keys()))
        self.deletes: list = []
        self.load_fail: set = set()

    def _get_free_space_gb(self, inst): return self._free
    def _resolve_instance(self, x): return "standard"
    def run_downgrades(self, inst, free): return {}
    def load_movie_files(self, inst):
        if inst in self.load_fail:
            raise RuntimeError(f"load failed for {inst}")
        return self._dfs.get(inst)

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
        self.deletes.append((inst, sorted(p["fid"] for p in picks)))
        return {"deleted": len(picks)}


class _Restore:
    def __init__(self): self.restored: list = []
    def restore_recovered_deletions(self, inst): self.restored.append(inst); return {}


class _Reg:
    def __init__(self, sp, restore):
        self._m = {"RadarrSpacePressureManager": sp, "RadarrRepairAnomalyManager": restore}
    def get(self, kind, key): return self._m.get(key)


def _coord(sp, restore):
    c = C.__new__(C)
    c.config = {
        "space_coordinator_enabled": True, "deletions_consent": True, "free_space_limit": 100.0,
        "radarr_instances": {"standard": {}, "ultra": {}, "test": {}, "default_instance": "standard"},
        "radarr_instances_categorized": {"4K": "ultra"},
        "routing": {"movies": {"evict_uhd_first": False}},
    }
    c.logger, c.dry_run, c.registry = _Log(), False, _Reg(sp, restore)
    return c


def _row(tmdb, fid, score, gb, res=None):
    r = {"tmdb_id": tmdb, "movie_file_id": fid, "watchability_score": score,
         "size_bytes": gb * _GB, "title": f"t{tmdb}"}
    if res is not None:
        r["resolution"] = res
    return r


def test_pools_and_deletes_extra_instance_excludes_4k():
    # standard + test each hold a 40GB low-score title; ultra (4K) holds a 70GB 4K-only
    # title. free 50 < T 100, need ~60 → both 40GB titles delete (routed per instance);
    # the 4K instance is never pooled/deleted, and restore covers exactly the two that deleted.
    dfs = {
        "standard": pd.DataFrame([_row(1, 11, 5, 40)]),
        "ultra":    pd.DataFrame([_row(9, 99, 5, 70, res=2160)]),
        "test":     pd.DataFrame([_row(3, 31, 5, 40)]),
    }
    sp = _RadarrSP(dfs, free=50.0, total=400.0)
    restore = _Restore()
    _coord(sp, restore).run()
    insts_deleted = {inst for inst, _ in sp.deletes}
    assert ("standard", [11]) in sp.deletes
    assert ("test", [31]) in sp.deletes
    assert "ultra" not in insts_deleted               # 4K-only title protected from coordinator delete
    assert "standard" in restore.restored and "test" in restore.restored
    assert "ultra" not in restore.restored


def test_extra_instance_load_failure_isolated():
    # test-instance df fails to load → it is skipped (never deleted), standard still deletes.
    dfs = {
        "standard": pd.DataFrame([_row(1, 11, 5, 80)]),
        "test":     pd.DataFrame([_row(3, 31, 5, 40)]),
    }
    sp = _RadarrSP(dfs, free=50.0, total=400.0)
    sp.load_fail.add("test")
    _coord(sp, _Restore()).run()
    insts_deleted = {inst for inst, _ in sp.deletes}
    assert "standard" in insts_deleted
    assert "test" not in insts_deleted


def test_each_instance_deleted_at_most_once():
    # Two low-score titles on the SAME extra instance → a single delete call for it
    # (one fid list), so the per-instance restore ledger is never double-counted.
    dfs = {
        "standard": pd.DataFrame([_row(1, 11, 5, 5)]),
        "test":     pd.DataFrame([_row(3, 31, 5, 40), _row(4, 41, 6, 40)]),
    }
    sp = _RadarrSP(dfs, free=50.0, total=400.0)
    _coord(sp, _Restore()).run()
    test_calls = [d for d in sp.deletes if d[0] == "test"]
    assert len(test_calls) == 1, f"expected one delete call for 'test', got {test_calls}"

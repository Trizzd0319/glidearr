"""Safety tests for the coordinator's evict-4K-first path (Stage B). Drives the real
SpaceCoordinatorManager.run() with fake managers and proves the invariants: under pressure a
baseline-backed 4K copy is evicted on the 4K INSTANCE before any whole title, the standard 1080p
baseline is never touched, restore covers the 4K instance, a 4K copy with NO surviving baseline is
never evicted as free, and with the gate off nothing 4K-related happens."""
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
    def __init__(self, free, total): self._f, self._t = free, total
    def disk_free_gb(self, inst): return self._f
    def disk_total_gb(self, inst): return self._t


class _RadarrSP:
    """Fake RadarrSpacePressureManager: serves movie_files dfs per instance, builds candidates
    (mimicking the real ceiling + resolution), and records deletes per instance."""
    def __init__(self, dfs, *, free, total):
        self._dfs = dfs
        self._free = free
        self.radarr_api = _Api(free, total)
        self.deletes: list = []

    def _get_free_space_gb(self, inst): return self._free      # coordinator _read_free reads this
    def _resolve_instance(self, x): return "standard"
    def run_downgrades(self, inst, free): return {}
    def _get_movie_files_manager(self): return None
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
        self.deletes.append((inst, sorted(p["fid"] for p in picks)))
        return {"deleted": len(picks)}


class _Restore:
    def __init__(self): self.restored: list = []
    def restore_recovered_deletions(self, inst): self.restored.append(inst); return {}


class _Reg:
    def __init__(self, sp, restore):
        self._m = {"RadarrSpacePressureManager": sp, "RadarrRepairAnomalyManager": restore}
    def get(self, kind, key): return self._m.get(key)


def _coord(sp, restore, config):
    c = C.__new__(C)
    c.config, c.logger, c.dry_run, c.registry = config, _Log(), False, _Reg(sp, restore)
    return c


def _cfg(*, evict=True):
    return {"space_coordinator_enabled": True, "deletions_consent": True, "free_space_limit": 100.0,
            "radarr_instances": {"standard": {"url": "s"}, "ultra": {"url": "u"}, "default_instance": "standard"},
            "radarr_instances_categorized": {"4K": "ultra"},
            "routing": {"movies": {"evict_uhd_first": evict, "4k_policy": "both"}}}


def _std_df():
    return pd.DataFrame([
        {"tmdb_id": 1, "movie_file_id": 11, "watchability_score": 5, "size_bytes": 5 * _GB, "title": "A"},
        {"tmdb_id": 2, "movie_file_id": 12, "watchability_score": 5, "size_bytes": 5 * _GB, "title": "B"},
    ])


def _no_env(monkeypatch):
    for v in ("RECOMMENDARR_DELETIONS_CONSENT", "GLIDEARR_DELETIONS_CONSENT"):
        monkeypatch.delenv(v, raising=False)


def test_evicts_4k_copy_first_on_4k_instance_baseline_untouched(monkeypatch):
    _no_env(monkeypatch)
    # tmdb 1 has a surviving 1080p baseline on standard AND a 70GB 4K copy on ultra. Need ~60GB.
    uhd_df = pd.DataFrame([{"tmdb_id": 1, "movie_file_id": 111, "watchability_score": 90,
                            "resolution": 2160, "size_bytes": 70 * _GB, "title": "A 4K"}])
    sp = _RadarrSP({"standard": _std_df(), "ultra": uhd_df}, free=50.0, total=400.0)
    restore = _Restore()
    _coord(sp, restore, _cfg()).run()
    assert ("ultra", [111]) in sp.deletes                      # the 4K copy evicted on the 4K instance
    assert all(inst != "standard" for inst, _ in sp.deletes)   # NO whole-title delete on standard
    assert "standard" in restore.restored and "ultra" in restore.restored   # restore covers both


def test_4k_only_title_without_baseline_is_never_evicted(monkeypatch):
    _no_env(monkeypatch)
    # tmdb 9 is a 4K copy on ultra with NO baseline on standard → must not be tagged/evicted.
    uhd_df = pd.DataFrame([{"tmdb_id": 9, "movie_file_id": 999, "watchability_score": 5,
                            "resolution": 2160, "size_bytes": 70 * _GB, "title": "Orphan 4K"}])
    sp = _RadarrSP({"standard": _std_df(), "ultra": uhd_df}, free=50.0, total=400.0)
    _coord(sp, _Restore(), _cfg()).run()
    assert all(inst != "ultra" for inst, _ in sp.deletes)      # the orphan 4K copy is never deleted


def test_gate_off_does_no_4k_eviction(monkeypatch):
    _no_env(monkeypatch)
    uhd_df = pd.DataFrame([{"tmdb_id": 1, "movie_file_id": 111, "watchability_score": 90,
                            "resolution": 2160, "size_bytes": 70 * _GB, "title": "A 4K"}])
    sp = _RadarrSP({"standard": _std_df(), "ultra": uhd_df}, free=50.0, total=400.0)
    restore = _Restore()
    _coord(sp, restore, _cfg(evict=False)).run()
    assert all(inst != "ultra" for inst, _ in sp.deletes)      # ultra never loaded/deleted
    assert "ultra" not in restore.restored                     # restore stays single-instance

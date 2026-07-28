"""Coordinator side of "deletion is the TRUE last resort" (``space_exhaustive_downgrade``).

Two coupled behaviours:

  1. **Downgrade credit.** In LEGACY mode the coordinator credits the ledger's projected
     downgrade reclaim against the deletion deficit, so a big enough projection suppresses
     deletion entirely (``action='downgrades_cover'``). In EXHAUSTIVE mode that credit is
     deliberately SKIPPED: the 720p-floor invariant already keeps every still-downgradable
     title OUT of the delete pool, so the two sets are disjoint and crediting would
     double-count — and, because an exhaustive plan projects a library-sized reclaim, it
     would zero the deficit every run and permanently disable the backstop.
  2. **Explaining an empty pool.** When the pool is empty because everything is still
     above the floor, the coordinator says so (and records ``still_downgradable``) instead
     of the misleading "no eligible delete candidates".
"""
from __future__ import annotations

import pandas as pd

from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager as C

_GIB = 1024 ** 3


class _Log:
    def __init__(self): self.msgs = []; self.tables = []
    def log_info(self, m): self.msgs.append(str(m))
    def log_warning(self, m): self.msgs.append(str(m))
    def log_error(self, m): self.msgs.append(str(m))
    def log_debug(self, *a, **k): pass
    def log_table(self, headers, rows, **k): self.tables.append((headers, rows))


def _coord(*, exhaustive, pool, free=5400.0, fsl=5500.0, projected_downgrade_gb=5000.0):
    """Coordinator below the floor (free < T) with stub services. The movie_files frame
    carries a ``downgrade`` ledger stamp worth ``projected_downgrade_gb`` — the input the
    credit block reads. ``pool`` is what build_delete_candidates yields."""
    deleted: list = []

    class _RadarrSP:
        radarr_api = None
        last_skipped_downgradable = 7

        def _resolve_instance(self, _): return "standard"
        def run_downgrades(self, inst, fr): return {"downgraded": 0}

        def load_movie_files(self, inst):
            return pd.DataFrame([{
                "tmdb_id": 1, "movie_file_id": 10, "title": "X", "resolution": 720,
                "planned_action": "downgrade", "plan_reclaim_gb": projected_downgrade_gb,
            }])

        def build_delete_candidates(self, inst, df, **k):
            return [dict(c) for c in pool]

        def delete_selected_movie_files(self, inst, df, picks):
            deleted.extend(picks)
            return {"deleted": len(picks), "failed": 0, "bytes_freed": 0.0}

    class _SonarrSP:
        sonarr_api = None
        def run_downgrades(self, inst, fr): return {"downgraded": 0}

    class _SonarrEF:
        last_skipped_downgradable = 3
        def _resolve_instance(self, _): return "standard"
        def load(self, inst): return None
        def restore_recovered_episode_deletions(self, inst): return {"restored": 0}

    class _RadarrRestore:
        def restore_recovered_deletions(self, inst): return {"restored": 0}

    c = object.__new__(C)
    c.config = {"space_coordinator_enabled": True, "deletions_consent": True,
                "free_space_limit": fsl, "space_exhaustive_downgrade": exhaustive}
    c.logger = _Log()
    c.dry_run = True
    c.global_cache = None
    _mgrs = {"RadarrSpacePressureManager": _RadarrSP(),
             "SonarrSpacePressureManager": _SonarrSP(),
             "SonarrCacheEpisodeFilesManager": _SonarrEF(),
             "RadarrRepairAnomalyManager": _RadarrRestore()}
    c._mgr = lambda key: _mgrs.get(key)          # type: ignore[assignment]
    c._read_total = lambda *a, **k: 10000.0      # type: ignore[assignment]
    c._read_free = lambda *a, **k: free          # type: ignore[assignment]
    return c, deleted


_AT_FLOOR = [{"service": "movie", "tier": 0, "score": 1, "critic": None, "idx": 0,
              "fid": 10, "tmdb_id": 1, "size_bytes": 900 * _GIB, "size_gb": 900.0,
              "resolution": 720, "title": "AtFloor"}]


def test_legacy_credit_can_suppress_deletion_entirely():
    c, deleted = _coord(exhaustive=False, pool=_AT_FLOOR)
    out = c.run()
    assert out["action"] == "downgrades_cover"
    assert out["downgrade_credit_gb"] > 0
    assert deleted == []                       # the projected downgrades "paid" for everything


def test_exhaustive_skips_the_credit_so_the_backstop_still_fires():
    # Same 5000 GB projection — but with the floor invariant the delete pool can only hold
    # at-floor items, which the credit can never have paid for. Deletion therefore runs.
    c, deleted = _coord(exhaustive=True, pool=_AT_FLOOR)
    out = c.run()
    assert out["downgrade_credit_gb"] == 0.0
    assert out["action"] == "deleted" and len(deleted) == 1
    assert any("no credit applied" in m for m in c.logger.msgs)


def test_exhaustive_empty_pool_is_explained_as_still_downgradable():
    c, deleted = _coord(exhaustive=True, pool=[])
    out = c.run()
    assert out["action"] == "no_candidates" and deleted == []
    assert out["still_downgradable"] == 7            # the movie service's excluded count
    assert any("still ABOVE the 720p floor" in m for m in c.logger.msgs)
    assert not any("no eligible delete candidates" in m for m in c.logger.msgs)


def test_delete_pool_table_shows_the_still_downgradable_reason():
    c, _ = _coord(exhaustive=True, pool=[])
    c.run()
    rows = dict(next(t[1] for t in c.logger.tables if t[0][0] == "Pool"))
    assert rows["still downgradable (movies)"] == 7
    assert rows["movie candidates"] == 0 and rows["episode candidates"] == 0
    assert rows["credit applied GB"] == 0.0          # never credited in exhaustive mode


def test_empty_pool_without_the_floor_reason_keeps_the_old_message():
    c, _ = _coord(exhaustive=True, pool=[])
    c._mgr("RadarrSpacePressureManager").last_skipped_downgradable = 0
    c._mgr("SonarrCacheEpisodeFilesManager").last_skipped_downgradable = 0
    c.run()
    assert any("no eligible delete candidates" in m for m in c.logger.msgs)

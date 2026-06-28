"""
Unit tests for the cross-service space coordinator (Phase 4).

Self-contained (no network / no registry / no Sonarr / no Radarr). Exercises:

  * ``SpaceCoordinatorManager._select_for_target`` — the unified ranking +
    greedy accumulation that decides which movies/episodes get deleted.
  * ``SpaceCoordinatorManager._critic_sort`` — None-critic neutrality.
  * ``SonarrCacheEpisodeFilesManager.build_delete_candidates`` — episode pool
    construction respects the whole-file guards and dedups per file id.
  * ``SonarrCacheEpisodeFilesManager.delete_selected_episode_files`` (dry_run) —
    refuses guarded ids, coalesces multi-ep files, counts bytes.

Run directly:  python -m scripts.managers.services.coordinator.test_space_coordinator
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.services.coordinator.space_coordinator import SpaceCoordinatorManager as C
from scripts.managers.services.sonarr.cache.episode_files import (
    SonarrCacheEpisodeFilesManager as M,
)

_NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


# ── coordinator selection ────────────────────────────────────────────────────
def _cand(service, score, size_gb, critic=None, **extra):
    d = {"service": service, "score": score, "size_gb": size_gb, "critic": critic}
    d.update(extra)
    return d


def test_select_for_target():
    print("test_select_for_target:")

    # Lowest score deleted first; stop once need is met.
    pool = [
        _cand("movie", 80, 10.0, fid=1),    # high watchability — keep
        _cand("episode", 5, 4.0, fid=2),     # low — delete first
        _cand("movie", 10, 3.0, critic=2.0, fid=3),
        _cand("episode", 5, 6.0, fid=4),     # tie score 5, bigger -> before fid=2
    ]
    sel, proj = C._select_for_target(pool, need_gb=9.0)
    ids = [c["fid"] for c in sel]
    # score 5 items first (bigger=fid4 then fid2), that's 6+4=10 >= 9 -> stop.
    _check("picks lowest-score first", ids[:2] == [4, 2], f"ids={ids}")
    _check("stops at need", proj >= 9.0 and len(sel) == 2, f"proj={proj} n={len(sel)}")
    _check("never selects high-watchability", 1 not in ids and 3 not in ids, f"ids={ids}")

    # Critic breaks ties when score is equal.
    pool2 = [
        _cand("movie", 10, 2.0, critic=8.0, fid=10),
        _cand("movie", 10, 2.0, critic=1.0, fid=11),   # lower critic -> delete first
    ]
    sel2, _ = C._select_for_target(pool2, need_gb=1.0)
    _check("low critic deleted before high critic", sel2[0]["fid"] == 11, f"sel={[c['fid'] for c in sel2]}")

    # None critic is neutral (5.0), not treated as 0.
    pool3 = [
        _cand("episode", 10, 2.0, critic=None, fid=20),
        _cand("movie", 10, 2.0, critic=1.0, fid=21),   # critic 1 < neutral 5 -> first
    ]
    sel3, _ = C._select_for_target(pool3, need_gb=1.0)
    _check("None critic sorts neutral", sel3[0]["fid"] == 21, f"sel={[c['fid'] for c in sel3]}")

    # Pool exhaustion: selects everything, projected < need (caller warns).
    pool4 = [_cand("movie", 5, 1.0, fid=30), _cand("episode", 5, 1.0, fid=31)]
    sel4, proj4 = C._select_for_target(pool4, need_gb=100.0)
    _check("exhausts pool when need unreachable", len(sel4) == 2 and proj4 == 2.0, f"proj={proj4}")

    # Empty pool -> empty selection.
    sel5, proj5 = C._select_for_target([], need_gb=5.0)
    _check("empty pool -> empty", sel5 == [] and proj5 == 0.0)


# ── playlist-pick delete shield ───────────────────────────────────────────────
class _Cache:
    def __init__(self, d): self.d = d
    def get(self, key, default=None): return self.d.get(key, default)


def test_shield_protected_picks():
    print("test_shield_protected_picks:")
    cache = _Cache({
        "plex/playlists/protected_movie_tmdbs/movie": {"tmdbs": [12, 34]},
        "plex/playlists/protected_movie_tmdbs/combined": {"tmdbs": [34, 56]},
    })
    pool = [
        _cand("movie", 5, 2.0, tmdb_id=12, fid=1),                       # recommended -> shielded
        _cand("movie", 5, 2.0, tmdb_id=99, fid=2),                       # not recommended -> kept
        _cand("movie", 5, 2.0, tmdb_id=56, is_uhd_copy=True, fid=3),     # reclaimable 4K copy -> kept
        _cand("episode", 5, 2.0, tmdb_id=34, fid=4),                     # episode -> not movie-shielded
    ]
    mgr = object.__new__(C)
    mgr.config = {}
    mgr.global_cache = cache
    kept, shielded = mgr._shield_protected_picks(pool)
    kept_ids = [c["fid"] for c in kept]
    _check("shields recommended whole movie", 1 not in kept_ids and shielded == 1, f"kept={kept_ids}")
    _check("keeps non-recommended movie", 2 in kept_ids)
    _check("keeps reclaimable 4K copy even when recommended", 3 in kept_ids)
    _check("keeps episode candidate (movie shield only)", 4 in kept_ids)

    mgr.config = {"space_protect_playlist_picks": False}            # opt-out disables the shield
    kept2, shielded2 = mgr._shield_protected_picks(pool)
    _check("flag off disables shield", len(kept2) == 4 and shielded2 == 0)

    mgr.config = {}
    mgr.global_cache = _Cache({})                                  # no published plans -> no-op
    kept3, shielded3 = mgr._shield_protected_picks(pool)
    _check("no plans -> no-op", len(kept3) == 4 and shielded3 == 0)


def test_critic_sort():
    print("test_critic_sort:")
    _check("None -> 5.0", C._critic_sort(None) == 5.0)
    _check("float passthrough", C._critic_sort(7.5) == 7.5)
    _check("bad value -> 5.0", C._critic_sort("nan-ish") == 5.0)


# ── Sonarr episode candidates / delete ───────────────────────────────────────
class _StubLogger:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []
    def log_warning(self, m): self.warnings.append(str(m))
    def log_info(self, m):    self.infos.append(str(m))
    def log_debug(self, m):   pass
    def log_error(self, m):   pass


def _sonarr_mgr() -> M:
    mgr = M.__new__(M)
    mgr.logger = _StubLogger()
    mgr.dry_run = True
    mgr.global_cache = None   # dry_run: restore-set write is skipped
    # Arm the deletion hard gate (deletions_enabled = explicit consent AND floor):
    # this stub exercises the coordinator delete APPLY primitive, so it must run.
    mgr.config = {"free_space_limit": 100.0, "deletions_consent": True}
    mgr._saved = []           # capture save() calls (no real parquet in tests)
    mgr.save = lambda inst, df: mgr._saved.append(df)  # type: ignore
    return mgr


def _ep_row(**kw):
    base = {
        "series_id": 1, "series_title": "Test Show", "season_number": 1,
        "episode_number": 1, "episode_file_id": 100, "size_bytes": 2 * 1024**3,
        "watchability_score": 5, "marked_for_deletion": False, "is_watched": True,
        "last_watched_at": _iso(60), "air_date_utc": _iso(400),
        "keep_policy": None, "is_pilot": False, "monitored": True,
    }
    base.update(kw)
    return base


def test_episode_candidates_and_delete():
    print("test_episode_candidates_and_delete:")

    # A real pilot (ep1) + two marked NON-pilot episodes on distinct files + one
    # unmarked -> 2 candidates. The pilot occupies the de-facto-pilot slot so the
    # marked episodes aren't auto-protected as the series' earliest-watched file.
    df = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=99, is_pilot=True, marked_for_deletion=False),
        _ep_row(episode_number=5, episode_file_id=100, marked_for_deletion=True, size_bytes=2 * 1024**3, watchability_score=4),
        _ep_row(episode_number=6, episode_file_id=101, marked_for_deletion=True, size_bytes=3 * 1024**3, watchability_score=8),
        _ep_row(episode_number=7, episode_file_id=102, marked_for_deletion=False),
    ])
    mgr = _sonarr_mgr()
    cands = mgr.build_delete_candidates("inst", df)
    fids = sorted(c["fid"] for c in cands)
    _check("only marked non-pilot rows become candidates", fids == [100, 101], f"fids={fids}")
    _check("carries watchability_score", {c["fid"]: c["score"] for c in cands}[100] == 4)
    _check("service tag is episode", all(c["service"] == "episode" for c in cands))
    _check("critic is None for episodes", all(c["critic"] is None for c in cands))
    _check("size_gb computed", abs([c for c in cands if c["fid"] == 101][0]["size_gb"] - 3.0) < 0.01)

    # Multi-ep file: two marked rows share one file id -> one candidate (deduped).
    # ep1 pilot occupies the de-facto-pilot slot so fid200 isn't auto-protected.
    df2 = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=199, is_pilot=True, marked_for_deletion=False),
        _ep_row(episode_number=5, episode_file_id=200, marked_for_deletion=True),
        _ep_row(episode_number=6, episode_file_id=200, marked_for_deletion=True),
    ])
    mgr = _sonarr_mgr()
    cands2 = mgr.build_delete_candidates("inst", df2)
    _check("multi-ep file dedups to one candidate", len(cands2) == 1, f"n={len(cands2)}")
    _check("deduped candidate is fid200", cands2[0]["fid"] == 200, f"cands={cands2}")

    # Whole-file guard: a marked row sharing a file with a pilot sibling -> excluded.
    df3 = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=300, marked_for_deletion=True, is_pilot=True),
        _ep_row(episode_number=2, episode_file_id=300, marked_for_deletion=True),
    ])
    mgr = _sonarr_mgr()
    cands3 = mgr.build_delete_candidates("inst", df3)
    _check("pilot-guarded file excluded from candidates", cands3 == [], f"cands={cands3}")

    # delete_selected_episode_files (dry_run): deletes requested unguarded fid,
    # refuses guarded, coalesces multi-ep.
    df4 = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=399, is_pilot=True, marked_for_deletion=False),   # series-1 pilot
        _ep_row(episode_number=5, episode_file_id=400, marked_for_deletion=True, size_bytes=5 * 1024**3),
        _ep_row(episode_number=6, episode_file_id=400, marked_for_deletion=True),  # same file
        _ep_row(episode_number=1, episode_file_id=401, marked_for_deletion=True, is_pilot=True, series_id=2),  # series-2 pilot (guarded)
    ])
    mgr = _sonarr_mgr()
    # load() is bypassed: patch it to return df4 directly.
    mgr.load = lambda inst: df4  # type: ignore
    stats = mgr.delete_selected_episode_files("inst", [400, 401])
    _check("deletes the one unguarded file once", stats["deleted"] == 1, f"stats={stats}")
    _check("guarded file skipped", stats["skipped_guard"] >= 1, f"stats={stats}")
    _check("bytes_freed counts the deleted file", abs(stats["bytes_freed"] - 5 * 1024**3) < 1, f"stats={stats}")
    # E (ledger): the deleted fid's rows are stamped 'delete'; reclaim counted once;
    # the guarded fid is NOT stamped; the df is persisted.
    _del = df4[df4["episode_file_id"] == 400]
    _check("E: deleted-fid rows stamped 'delete'", (_del["planned_action"] == "delete").all(), f"{list(_del['planned_action'])}")
    _check("E: reclaim counted once per file id", _del["plan_reclaim_gb"].notna().sum() == 1, f"{list(_del['plan_reclaim_gb'])}")
    _check("E: guarded fid not stamped", (df4[df4["episode_file_id"] == 401]["planned_action"] != "delete").all())
    _check("E: df persisted once", len(mgr._saved) == 1, f"saved={len(mgr._saved)}")


def test_candidate_failsafes():
    """Data-loss guards added in review (H4, H5): never emit candidates / never
    delete when the protected-set build fails or scores never populated."""
    print("test_candidate_failsafes:")

    def _boom(*a, **k):
        raise RuntimeError("guard build broke")

    # H5: watchability_score column entirely empty -> NO candidates (refuse to
    # delete on fallback scores).
    df = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=99, is_pilot=True, marked_for_deletion=False, watchability_score=None),
        _ep_row(episode_number=5, episode_file_id=100, marked_for_deletion=True, watchability_score=None),
    ])
    mgr = _sonarr_mgr()
    _check("H5: empty scores -> no candidates", mgr.build_delete_candidates("inst", df) == [])

    # H4: protected-set build failure -> fail-safe yields NO candidates (a guarded
    # multi-ep sibling must never leak into the pool because the guard crashed).
    #
    # NB: M (via BaseManager.__new__) is a process-wide singleton keyed on
    # (cls, singleton_key), so M.__new__(M) hands back the SAME instance every
    # time. Patching _build_protected_file_ids onto it shadows the class method
    # for the whole process — restore it in finally or the boom stub leaks into
    # sibling test files (e.g. test_episode_files_guards.py).
    df2 = pd.DataFrame([
        _ep_row(episode_number=1, episode_file_id=199, is_pilot=True, marked_for_deletion=False),
        _ep_row(episode_number=5, episode_file_id=200, marked_for_deletion=True),
    ])
    mgr = _sonarr_mgr()
    mgr._build_protected_file_ids = _boom  # type: ignore
    try:
        _check("H4: guard-build failure -> no candidates", mgr.build_delete_candidates("inst", df2) == [])
    finally:
        del mgr._build_protected_file_ids  # restore class-method resolution

    # H4: protected-set build failure on the DELETE path -> refuse to delete anything.
    df3 = pd.DataFrame([
        _ep_row(episode_number=5, episode_file_id=300, marked_for_deletion=True, size_bytes=4 * 1024**3),
    ])
    mgr = _sonarr_mgr()
    mgr.load = lambda inst: df3  # type: ignore
    mgr._build_protected_file_ids = _boom  # type: ignore
    try:
        stats = mgr.delete_selected_episode_files("inst", [300])
        _check("H4: guard-build failure -> delete refuses", stats["deleted"] == 0, f"stats={stats}")
    finally:
        del mgr._build_protected_file_ids  # restore class-method resolution


# ── space-targets floor derivation (total-aware) ──────────────────────────────
def _api(total_gb):
    return type("_Api", (), {"disk_total_gb": lambda self, i: total_gb})()


def test_read_total_and_space_targets():
    print("test_read_total_and_space_targets:")
    c = object.__new__(C)
    c.config = {}

    # _read_total takes the MIN across services (same shared mount, conservative).
    radarr_sp = type("R", (), {"radarr_api": _api(8000.0)})()
    sonarr_sp = type("S", (), {"sonarr_api": _api(10000.0)})()
    total = c._read_total(radarr_sp, "r", sonarr_sp, "s")
    _check("min total across services", total == 8000.0, f"total={total}")

    # Unset free_space_limit -> floor is 25% of that total (not the 25 GB constant).
    T, U = c._space_targets(total_gb=total)
    _check("floor is 25% of total", T == 2000.0 and U == T, f"(T,U)=({T},{U})")

    # No readable total -> None -> last-resort PRESSURE_FALLBACK_GB.
    none_total = c._read_total(None, None, None, None)
    _check("no total -> None", none_total is None)
    Tf, Uf = c._space_targets(total_gb=none_total)
    _check("last-resort constant", (Tf, Uf) == (C.PRESSURE_FALLBACK_GB,) * 2, f"({Tf},{Uf})")

    # Configured free_space_limit wins over total.
    c.config = {"free_space_limit": 2500}
    Tc, Uc = c._space_targets(total_gb=total)
    _check("free_space_limit drives band", Tc == 2500.0 and abs(Uc - 2750.0) < 1e-6, f"({Tc},{Uc})")


# ── run(): deletion is floor-gated for hysteresis ─────────────────────────────
class _Rec:
    """Tally which stages run() actually reaches."""
    def __init__(self):
        self.radarr_downgrades = 0
        self.sonarr_downgrades = 0
        self.delete_candidates_built = 0
        self.downgrade_insts = []


def _coord(free, rec, *, fsl=5500.0, radarr_instances=None):
    """A coordinator wired to stub managers, with free space pinned to ``free``.
    T=fsl, U=fsl*1.1. Stubs record downgrade + delete-pool calls; deletion would
    fire iff run() reaches build_delete_candidates. ``radarr_instances`` (a list)
    gives radarr_sp.radarr_api.get_all_radarr_apis() so the multi-instance downgrade
    loop can be exercised; when None the api lacks the method (single-instance)."""
    class _Api:
        def get_all_radarr_apis(self): return {n: None for n in radarr_instances}
    class _RadarrSP:
        radarr_api = _Api() if radarr_instances else None
        def _resolve_instance(self, _): return "standard"
        def run_downgrades(self, inst, fr):
            rec.radarr_downgrades += 1; rec.downgrade_insts.append(inst); return {"downgraded": 0}
        def load_movie_files(self, inst):
            return pd.DataFrame([{"tmdb_id": 1, "movie_file_id": 10, "title": "X"}])
        def build_delete_candidates(self, inst, df, **k):
            rec.delete_candidates_built += 1
            return []
    class _SonarrSP:
        sonarr_api = None
        def run_downgrades(self, inst, fr): rec.sonarr_downgrades += 1; return {"downgraded": 0}
    class _SonarrEF:
        def _resolve_instance(self, _): return "standard"
        def load(self, inst): return None
        def restore_recovered_episode_deletions(self, inst): return {"restored": 0}
    class _RadarrRestore:
        def restore_recovered_deletions(self, inst): return {"restored": 0}

    c = object.__new__(C)
    c.config = {"space_coordinator_enabled": True, "deletions_consent": True,
                "free_space_limit": fsl}
    c.logger = _StubLogger()
    c.dry_run = True
    c.global_cache = None
    _mgrs = {"RadarrSpacePressureManager": _RadarrSP(),
             "SonarrSpacePressureManager": _SonarrSP(),
             "SonarrCacheEpisodeFilesManager": _SonarrEF(),
             "RadarrRepairAnomalyManager": _RadarrRestore()}
    c._mgr = lambda key: _mgrs.get(key)        # type: ignore
    c._read_total = lambda *a, **k: 10000.0     # type: ignore
    c._read_free = lambda *a, **k: free         # type: ignore
    return c


def test_run_deletion_is_floor_gated():
    print("test_run_deletion_is_floor_gated:")
    # T=5500, U=6050.

    # Comfortable (free >= U): bail before any reclamation.
    rec = _Rec(); out = _coord(6200.0, rec).run()
    _check("free >= U: action none", out.get("action") == "none", f"out={out}")
    _check("free >= U: no downgrades", rec.radarr_downgrades == 0 and rec.sonarr_downgrades == 0)
    _check("free >= U: no delete pool", rec.delete_candidates_built == 0)

    # In the band (T <= free < U): non-destructive downgrades run, but the delete
    # pool HOLDS — this is the floor-gated hysteresis (the bug this test guards).
    rec = _Rec(); out = _coord(5557.0, rec).run()
    _check("band: downgrades ran", rec.radarr_downgrades == 1 and rec.sonarr_downgrades == 1, f"rec={rec.__dict__}")
    _check("band: action band_hold", out.get("action") == "band_hold", f"out={out}")
    _check("band: NO delete pool built", rec.delete_candidates_built == 0, f"built={rec.delete_candidates_built}")

    # Below floor (free < T): downgrades run AND the deletion path is reached.
    rec = _Rec(); out = _coord(5400.0, rec).run()
    _check("below floor: downgrades ran", rec.radarr_downgrades == 1 and rec.sonarr_downgrades == 1)
    _check("below floor: delete pool built", rec.delete_candidates_built == 1, f"built={rec.delete_candidates_built}")
    _check("below floor: not band_hold", out.get("action") != "band_hold", f"out={out}")


def test_run_downgrades_all_radarr_instances():
    print("test_run_downgrades_all_radarr_instances:")
    insts = ["standard", "ultra", "test"]

    # Below floor: coordinator downgrades EVERY instance once, default first.
    rec = _Rec(); out = _coord(5400.0, rec, radarr_instances=insts).run()
    _check("downgraded all 3 instances", sorted(rec.downgrade_insts) == ["standard", "test", "ultra"], f"insts={rec.downgrade_insts}")
    _check("default instance first", rec.downgrade_insts[0] == "standard", f"insts={rec.downgrade_insts}")
    _check("each downgraded exactly once", rec.radarr_downgrades == 3, f"n={rec.radarr_downgrades}")

    # Band (T<=free<U): still downgrades all instances, but deletion HOLDS.
    rec = _Rec(); out = _coord(5557.0, rec, radarr_instances=insts).run()
    _check("band: all instances downgraded", rec.radarr_downgrades == 3, f"n={rec.radarr_downgrades}")
    _check("band: action band_hold", out.get("action") == "band_hold", f"out={out}")
    _check("band: no delete pool", rec.delete_candidates_built == 0, f"built={rec.delete_candidates_built}")

    # Single-instance setup (api lacks get_all_radarr_apis) -> only the default.
    rec = _Rec(); _coord(5400.0, rec).run()
    _check("single-instance: only default downgraded", rec.downgrade_insts == ["standard"], f"insts={rec.downgrade_insts}")


if __name__ == "__main__":
    test_select_for_target()
    test_critic_sort()
    test_episode_candidates_and_delete()
    test_candidate_failsafes()
    test_read_total_and_space_targets()
    test_run_deletion_is_floor_gated()
    test_run_downgrades_all_radarr_instances()
    print("\nAll coordinator tests passed")

"""
SpaceCoordinatorManager — cross-service space reclamation capstone (Phase 4).
================================================================================
The four space-management phases culminate here. Radarr and Sonarr each own their
*upgrade* and *downgrade* stages, but **deletion is unified**: when the shared
media mount drops below the pressure band, movies and TV episodes compete in **one
ranked pool** sorted by watchability so the least-valuable bytes go first —
regardless of which service owns them.

Pipeline (all stages dry_run-safe; the leaf managers log-only under dry_run):

  Gate   — opt-in via ``space_coordinator_enabled`` AND ``free_space_limit`` > 0
           (``coordinator_owns_deletion``). Bail immediately if free ≥ U so a
           healthy library is never touched. While disabled, each service keeps
           its own per-service delete loop (this manager is simply never invoked).
  Stage 1 — run BOTH downgrade passes (Radarr → HD-720p, Sonarr → HD-720p). These
           reclaim projected space cheaply (re-grab smaller files) before any
           deletion. Re-read free; return if we've recovered to U.
  Stage 2 — build the COMBINED delete pool: Radarr movie candidates +
           Sonarr episode candidates, each carrying its persisted
           ``watchability_score``. Sort ascending by score, then by critic
           rating, then by size descending; accumulate from the bottom until the
           projected free space reaches U. Split the selection back to each
           service and delete (movies via moviefile/{id}, episodes via
           episodefile/{id} with the whole-file guards).
  Stage 3 — restore: re-acquire anything previously coordinator-deleted whose
           score has since recovered (Radarr restore_recovered_deletions +
           Sonarr restore_recovered_episode_deletions).

Deletion is the *true* backstop — Stage-1 downgrades only project reclaim; Stage 2
realizes it. Restorable: every deletion is tracked so a later recovery in
watchability re-grabs it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.space.coordinator_ranker import (
    critic_sort,
    select_for_target,
)
from scripts.managers.machine_learning.space.routing_targets import evict_uhd_first
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_targets import coordinator_owns_deletion, space_targets


class SpaceCoordinatorManager(BaseManager, ComponentManagerMixin):
    parent_name = "SpaceCoordinatorManager"

    # Last-resort pressure floor — only when free_space_limit is unset AND the shared
    # mount's total size is unreadable (otherwise the floor is 25% of the total drive).
    PRESSURE_FALLBACK_GB = 1000.0

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "SpaceCoordinatorManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        parent = kwargs.get("manager")

        # dry_run resolution (kwargs → parent → Main); never silently default.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        self.dry_run = bool(_dry_run) if _dry_run is not None else False

        self.sonarr = kwargs.get("sonarr")
        self.radarr = kwargs.get("radarr")

    def prepare(self) -> None:
        pass

    # ── Manager lookups ──────────────────────────────────────────────────────────
    def _mgr(self, key: str):
        try:
            return self.registry.get("manager", key)
        except Exception:
            return None

    def _space_targets(self, total_gb: "float | None" = None) -> tuple[float, float]:
        """(T, U) for the shared media mount. When ``free_space_limit`` is unset the
        floor defaults to 25% of ``total_gb`` (the shared mount's deduped total);
        PRESSURE_FALLBACK_GB is the last resort only when the total is also unknown."""
        return space_targets(self.config, fallback_gb=self.PRESSURE_FALLBACK_GB, total_gb=total_gb)

    @staticmethod
    def _critic_sort(critic) -> float:
        """Critic-rating sort key — delegates to the brain
        (space.coordinator_ranker.critic_sort); None -> neutral 5.0."""
        return critic_sort(critic)

    @classmethod
    def _select_for_target(cls, pool: list[dict], need_gb: float, *,
                           recency_ramp: "dict | None" = None, now=None,
                           tier_size: "float | None" = None,
                           uhd_first: bool = False) -> tuple[list[dict], float]:
        """Rank the combined movie+episode delete pool to the free-space target —
        delegates to the brain (space.coordinator_ranker.select_for_target). Returns
        ``(selected, projected_gb)``. ``recency_ramp``/``now`` sink a recently-watched
        file to the bottom of the order; ``tier_size`` buckets the score so the biggest
        file in the lowest tier goes first; ``uhd_first`` puts baseline-backed 4K bonus
        copies ahead of every whole title. All default to the byte-identical ranking."""
        return select_for_target(pool, need_gb, recency_ramp=recency_ramp, now=now,
                                 tier_size=tier_size, uhd_first=uhd_first)

    # ── Entry point ──────────────────────────────────────────────────────────────
    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self) -> dict:
        # ── Gate ────────────────────────────────────────────────────────────────
        if not coordinator_owns_deletion(self.config):
            cfg = self.config or {}
            try:
                _fsl = float(cfg.get("free_space_limit", 0) or 0)
            except (TypeError, ValueError):
                _fsl = 0.0
            if cfg.get("space_coordinator_enabled") and _fsl <= 0:
                # The flag is on but the floor is unset, so coordinator_owns_deletion is
                # False — the coordinator is INERT, and (deletions_enabled hard gate)
                # ALL deletion everywhere is disabled until free_space_limit is set.
                self.logger.log_warning(
                    "[SpaceCoordinator] space_coordinator_enabled=true but free_space_limit<=0 — "
                    "coordinator is INERT and ALL media deletions are DISABLED until "
                    "free_space_limit>0 is set in config.json."
                )
            else:
                self.logger.log_debug(
                    "[SpaceCoordinator] disabled — set space_coordinator_enabled=true and "
                    "free_space_limit>0 to enable the unified movie+TV delete pool."
                )
            return {"enabled": False, "action": "disabled"}

        radarr_sp = self._mgr("RadarrSpacePressureManager")
        sonarr_sp = self._mgr("SonarrSpacePressureManager")
        sonarr_ef = self._mgr("SonarrCacheEpisodeFilesManager")
        radarr_restore = self._mgr("RadarrRepairAnomalyManager")
        if radarr_sp is None and sonarr_sp is None:
            self.logger.log_warning(
                "[SpaceCoordinator] no space-pressure managers available — skipping."
            )
            return {"enabled": True, "action": "no_managers"}

        radarr_inst = radarr_sp._resolve_instance(None) if radarr_sp else None
        sonarr_inst = sonarr_ef._resolve_instance(None) if sonarr_ef else None

        total = self._read_total(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst)
        T, U = self._space_targets(total_gb=total)
        free = self._read_free(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst)
        self.logger.log_info(
            f"[SpaceCoordinator] shared pool: {free:.0f} GB free "
            f"(floor {T:.0f} GB, target band top {U:.0f} GB, dry_run={self.dry_run})."
        )
        if free >= U:
            self.logger.log_info(
                f"[SpaceCoordinator] {free:.0f} GB ≥ {U:.0f} GB — no space pressure, skipping."
            )
            return {"enabled": True, "action": "none", "free_space_gb": round(free, 1)}

        stats: dict = {"enabled": True, "free_before_gb": round(free, 1),
                       "downgrades": {}, "deletions": {}, "restores": {}}
        # Bound up-front (None) so the Stage-1 "downgrades_only" and the "no candidates" early
        # returns can pass uhd_inst to _run_restores before the Stage-2 4K block (re)assigns it.
        uhd_inst, uhd_df = None, None

        # ── Stage 1: downgrades (both services) ─────────────────────────────────
        if radarr_sp and radarr_inst:
            try:
                stats["downgrades"]["radarr"] = radarr_sp.run_downgrades(radarr_inst, free)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Radarr downgrades failed: {e}")
        if sonarr_sp and sonarr_inst:
            try:
                stats["downgrades"]["sonarr"] = sonarr_sp.run_downgrades(sonarr_inst, free)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Sonarr downgrades failed: {e}")

        free = self._read_free(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst)
        stats["free_after_downgrades_gb"] = round(free, 1)
        if free >= U:
            self.logger.log_info(
                f"[SpaceCoordinator] downgrades recovered to {free:.0f} GB ≥ {U:.0f} GB — "
                f"no deletion needed."
            )
            stats["action"] = "downgrades_only"
            stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, uhd_inst=uhd_inst)
            return stats

        # ── Stage 2: combined ranked delete pool ────────────────────────────────
        # Defensive: drop the Radarr movie_files run cache so this post-run load reads
        # fresh from disk, decoupled from the orchestration run that populated it.
        try:
            if radarr_sp and hasattr(radarr_sp, "_get_movie_files_manager"):
                _mfm = radarr_sp._get_movie_files_manager()
                if _mfm is not None and hasattr(_mfm, "reset_run_cache"):
                    _mfm.reset_run_cache()
        except Exception:
            pass
        radarr_df = radarr_sp.load_movie_files(radarr_inst) if (radarr_sp and radarr_inst) else None
        sonarr_df = sonarr_ef.load(sonarr_inst) if (sonarr_ef and sonarr_inst) else None

        pool: list[dict] = []
        if radarr_sp and radarr_df is not None and not radarr_df.empty:
            try:
                pool += radarr_sp.build_delete_candidates(radarr_inst, radarr_df)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] movie candidate build failed: {e}")
        if sonarr_ef and sonarr_df is not None and not sonarr_df.empty:
            try:
                pool += sonarr_ef.build_delete_candidates(sonarr_inst, sonarr_df)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] episode candidate build failed: {e}")

        # ── Evict-4K-first (default-off): add the dual-version 4K BONUS copies to the pool ──
        # Each is a 2160p copy whose 1080p baseline SURVIVES on the standard instance, so
        # reclaiming it loses NO title (pure reclaim). The ranker (uhd_first) then puts them
        # ahead of every whole title, lowest-watchability first, so a whole title is never
        # deleted while a reclaimable 4K copy remains.
        if radarr_sp and radarr_inst and evict_uhd_first(self.config):
            uhd_inst = self._uhd_instance(radarr_inst)
            if uhd_inst and uhd_inst != radarr_inst:
                survivors = self._baseline_survivors(radarr_df)     # standard tmdbIds WITH a file
                try:
                    uhd_df = radarr_sp.load_movie_files(uhd_inst)
                except Exception as e:
                    self.logger.log_warning(f"[SpaceCoordinator] 4K movie_files load failed for "
                                            f"'{uhd_inst}': {e}")
                    uhd_df = None
                if uhd_df is not None and not uhd_df.empty:
                    try:
                        uhd_cands = radarr_sp.build_delete_candidates(
                            uhd_inst, uhd_df, ignore_score_ceiling=True)
                    except Exception as e:
                        self.logger.log_warning(f"[SpaceCoordinator] 4K candidate build failed: {e}")
                        uhd_cands = []
                    added = 0
                    for c in uhd_cands:
                        res = c.get("resolution")
                        if res and int(res) > 1080 and c.get("tmdb_id") in survivors:
                            c["is_uhd_copy"] = True            # ranker evicts these first
                            c["instance"] = uhd_inst           # deleted against the 4K instance
                            pool.append(c)
                            added += 1
                    if added:
                        self.logger.log_info(
                            f"[SpaceCoordinator] {added} reclaimable 4K copy(ies) on '{uhd_inst}' "
                            f"(1080p baseline survives → reclaimed before any whole title).")

        # Shield titles the per-user playlists are actively recommending (esp. a kid's top
        # picks) from the delete pool. The household-blended watchability score can dilute a
        # child's clear favourite below the delete ceiling, and deleting what we just put in
        # someone's Up Next is self-defeating. Whole titles only — reclaimable 4K bonus copies
        # still go (their 1080p baseline survives). Opt out via space_protect_playlist_picks=false.
        pool, _shielded = self._shield_protected_picks(pool)
        if _shielded:
            self.logger.log_info(
                f"[SpaceCoordinator] shielded {_shielded} recommended title(s) from the delete "
                f"pool (currently in a user's Up Next playlist)."
            )

        if not pool:
            self.logger.log_info("[SpaceCoordinator] no eligible delete candidates — nothing to do.")
            stats["action"] = "no_candidates"
            stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, uhd_inst=uhd_inst)
            return stats

        # Rank lowest watchability first, then lowest critic, then biggest file
        # first, and accumulate from the bottom until we'd reach U.
        need = U - free   # GB we must reclaim
        # Optional recency weighting: a file watched in the last few days sinks to the
        # bottom of the delete order so the sweep takes cold titles first. Default-off
        # (the ramp must be enabled) → the bare watchability ranking, byte-identical.
        _recency_ramp = (self.config or {}).get("delete_recency_ramp", {}) or {}
        _now = datetime.now(timezone.utc) if _recency_ramp.get("enabled") else None
        # Optional likelihood-tier bucketing: within a watchability tier, take the
        # biggest file first (fewer deletions to hit the target). Default-off (unset /
        # <=0) → no bucketing, byte-identical.
        try:
            _tier_size = float((self.config or {}).get("delete_tier_size", 0) or 0) or None
        except (TypeError, ValueError):
            _tier_size = None
        selected, projected = self._select_for_target(
            pool, need, recency_ramp=_recency_ramp, now=_now, tier_size=_tier_size,
            uhd_first=bool(uhd_inst),
        )

        movie_picks = [c for c in selected if c.get("service") == "movie"]
        episode_picks = [c for c in selected if c.get("service") == "episode"]
        # Split the movie picks by instance: the standard 1080p-tier titles delete against the
        # standard instance/df; the dual-version 4K copies delete against the 4K instance/df.
        std_movie_picks = [c for c in movie_picks if not c.get("is_uhd_copy")]
        uhd_movie_picks = [c for c in movie_picks if c.get("is_uhd_copy")]
        for c in movie_picks:
            c["reason"] = f"coordinator pool (score {c.get('score')})"
        episode_fids = [c["fid"] for c in episode_picks if c.get("fid") is not None]

        self.logger.log_info(
            f"[SpaceCoordinator] need {need:.0f} GB → selected {len(selected)} item(s) "
            f"({len(movie_picks)} movie, {len(episode_picks)} episode) projecting "
            f"~{projected:.0f} GB reclaim."
        )
        if projected < need:
            self.logger.log_warning(
                f"[SpaceCoordinator] pool exhausted — only ~{projected:.0f} GB of {need:.0f} GB "
                f"reclaimable from {len(pool)} candidate(s); deleting all eligible."
            )

        if std_movie_picks and radarr_sp and radarr_df is not None:
            try:
                stats["deletions"]["radarr"] = radarr_sp.delete_selected_movie_files(radarr_inst, radarr_df, std_movie_picks)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] movie deletion failed: {e}")
        if uhd_movie_picks and radarr_sp and uhd_df is not None:
            try:
                # Deletes ONLY the 2160p file on the 4K instance (separate DB/record); the standard
                # instance's 1080p baseline is untouched. Recorded under the 4K instance's restore ledger.
                stats["deletions"]["radarr_uhd"] = radarr_sp.delete_selected_movie_files(uhd_inst, uhd_df, uhd_movie_picks)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] 4K copy deletion failed: {e}")
        if episode_fids and sonarr_ef:
            try:
                stats["deletions"]["sonarr"] = sonarr_ef.delete_selected_episode_files(sonarr_inst, episode_fids)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] episode deletion failed: {e}")

        stats["free_after_deletions_gb"] = round(
            self._read_free(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst), 1
        )
        stats["action"] = "deleted"

        # ── Stage 3: restore recovered ──────────────────────────────────────────
        stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, uhd_inst=uhd_inst)
        return stats

    # ── playlist-pick delete shield ──────────────────────────────────────────────
    @staticmethod
    def _as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _protected_playlist_tmdbs(self) -> set:
        """Union of movie tmdbIds the movie + combined playlist builders published this run as
        currently-recommended (per-user Up Next plans). Empty when the builders did not run /
        published nothing — in which case the shield is a no-op (fail-open is fine: it only ever
        REMOVES candidates from deletion, never adds)."""
        out: set = set()
        cache = getattr(self, "global_cache", None)
        if not cache:
            return out
        for key in ("plex/playlists/protected_movie_tmdbs/movie",
                    "plex/playlists/protected_movie_tmdbs/combined"):
            try:
                blob = cache.get(key) or {}
            except Exception:
                blob = {}
            for v in (blob.get("tmdbs") or []):
                iv = self._as_int(v)
                if iv is not None:
                    out.add(iv)
        return out

    def _shield_protected_picks(self, pool: "list[dict]") -> "tuple[list[dict], int]":
        """Drop whole-title movie candidates whose tmdb_id is in the current per-user playlist
        plans from the delete pool. 4K bonus copies (``is_uhd_copy``) are NOT shielded — their
        1080p baseline survives, so reclaiming them loses no recommended title. No-op when the
        ``space_protect_playlist_picks`` flag is off (default ON) or no plans were published."""
        if not (self.config or {}).get("space_protect_playlist_picks", True):
            return pool, 0
        protected = self._protected_playlist_tmdbs()
        if not protected:
            return pool, 0
        kept, shielded = [], 0
        for c in pool:
            if (c.get("service") == "movie" and not c.get("is_uhd_copy")
                    and self._as_int(c.get("tmdb_id")) in protected):
                shielded += 1
                continue
            kept.append(c)
        return kept, shielded

    # ── Helpers ──────────────────────────────────────────────────────────────────
    def _read_free(self, radarr_sp, radarr_inst, sonarr_sp, sonarr_inst) -> float:
        """Free GB on the shared media mount. Radarr and Sonarr report the same
        underlying mount, so take the MIN of whatever's available (conservative)."""
        vals = []
        if radarr_sp and radarr_inst:
            try:
                vals.append(float(radarr_sp._get_free_space_gb(radarr_inst)))
            except Exception:
                pass
        if sonarr_sp and sonarr_inst:
            try:
                vals.append(float(sonarr_sp.get_free_space_gb(sonarr_inst)))
            except Exception:
                pass
        vals = [v for v in vals if v == v and v != float("inf")]
        if not vals:
            return float("inf")
        return min(vals)

    # ── evict-4K-first helpers ────────────────────────────────────────────────────
    _UHD_LABELS = ("4k", "uhd", "2160p", "2160")

    def _uhd_instance(self, default_inst):
        """The distinct 4K Radarr instance from the categorized map, or None. Config-only (mirrors
        the reconcile's alias logic): a "4K"/"4k"/"uhd"/"2160p" categorized label that maps to a real
        instance OTHER than the default. None → no separate 4K instance → eviction stays single-pool."""
        cat = (self.config or {}).get("radarr_instances_categorized", {}) or {}
        insts = (self.config or {}).get("radarr_instances", {}) or {}
        lower = {str(k).lower(): v for k, v in cat.items() if k}
        for label in self._UHD_LABELS:
            v = lower.get(label)
            if v and str(v) in insts and str(v) != str(default_inst):
                return str(v)
        return None

    @staticmethod
    def _baseline_survivors(df) -> frozenset:
        """tmdbIds on the standard instance that own an actual file — deleting a 4K copy of any of
        these loses NO title. Read from the already-loaded standard df (no extra HTTP); a not-yet-
        cached baseline simply isn't a survivor, which is fail-safe (we then don't evict its 4K)."""
        if df is None or getattr(df, "empty", True) or "tmdb_id" not in getattr(df, "columns", []):
            return frozenset()
        if "movie_file_id" in df.columns:
            has = df["movie_file_id"].notna()
        elif "has_file" in df.columns:
            has = df["has_file"].fillna(False).astype(bool)
        else:
            return frozenset()
        return frozenset(int(t) for t, h in zip(df["tmdb_id"], has) if h and pd.notna(t))

    def _read_total(self, radarr_sp, radarr_inst, sonarr_sp, sonarr_inst) -> "float | None":
        """Total capacity (GB) of the shared media mount, mount-deduped. Radarr and
        Sonarr report the same underlying mount, so take the MIN of whatever's
        available (conservative — a smaller total yields a smaller 25%-of-total floor).
        Returns None when neither service can report it, so space_targets falls back to
        the last-resort constant rather than scaling off a bogus total."""
        vals = []
        if radarr_sp and radarr_inst and getattr(radarr_sp, "radarr_api", None):
            try:
                vals.append(float(radarr_sp.radarr_api.disk_total_gb(radarr_inst)))
            except Exception:
                pass
        if sonarr_sp and sonarr_inst and getattr(sonarr_sp, "sonarr_api", None):
            try:
                vals.append(float(sonarr_sp.sonarr_api.disk_total_gb(sonarr_inst)))
            except Exception:
                pass
        vals = [v for v in vals if v == v and v > 0 and v != float("inf")]
        return min(vals) if vals else None

    def _run_restores(self, radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, *, uhd_inst=None) -> dict:
        out: dict = {}
        if radarr_restore and radarr_inst and hasattr(radarr_restore, "restore_recovered_deletions"):
            try:
                out["radarr"] = radarr_restore.restore_recovered_deletions(radarr_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Radarr restore failed: {e}")
        # Evicted 4K copies are recorded under the 4K instance's own restore ledger, so restore
        # must run for it too — otherwise a 4K copy whose watchability recovers is never re-grabbed.
        if radarr_restore and uhd_inst and uhd_inst != radarr_inst \
                and hasattr(radarr_restore, "restore_recovered_deletions"):
            try:
                out["radarr_uhd"] = radarr_restore.restore_recovered_deletions(uhd_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] 4K restore failed: {e}")
        if sonarr_ef and sonarr_inst and hasattr(sonarr_ef, "restore_recovered_episode_deletions"):
            try:
                out["sonarr"] = sonarr_ef.restore_recovered_episode_deletions(sonarr_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Sonarr restore failed: {e}")
        return out

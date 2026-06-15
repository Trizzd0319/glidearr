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

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.space.coordinator_ranker import (
    critic_sort,
    select_for_target,
)
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
                           tier_size: "float | None" = None) -> tuple[list[dict], float]:
        """Rank the combined movie+episode delete pool to the free-space target —
        delegates to the brain (space.coordinator_ranker.select_for_target). Returns
        ``(selected, projected_gb)``. ``recency_ramp``/``now`` sink a recently-watched
        file to the bottom of the order; ``tier_size`` buckets the score so the biggest
        file in the lowest tier goes first. All default to the byte-identical ranking."""
        return select_for_target(pool, need_gb, recency_ramp=recency_ramp, now=now,
                                 tier_size=tier_size)

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
            stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst)
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

        if not pool:
            self.logger.log_info("[SpaceCoordinator] no eligible delete candidates — nothing to do.")
            stats["action"] = "no_candidates"
            stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst)
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
            pool, need, recency_ramp=_recency_ramp, now=_now, tier_size=_tier_size
        )

        movie_picks = [c for c in selected if c.get("service") == "movie"]
        episode_picks = [c for c in selected if c.get("service") == "episode"]
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

        if movie_picks and radarr_sp and radarr_df is not None:
            try:
                stats["deletions"]["radarr"] = radarr_sp.delete_selected_movie_files(radarr_inst, radarr_df, movie_picks)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] movie deletion failed: {e}")
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
        stats["restores"] = self._run_restores(radarr_restore, radarr_inst, sonarr_ef, sonarr_inst)
        return stats

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

    def _run_restores(self, radarr_restore, radarr_inst, sonarr_ef, sonarr_inst) -> dict:
        out: dict = {}
        if radarr_restore and radarr_inst and hasattr(radarr_restore, "restore_recovered_deletions"):
            try:
                out["radarr"] = radarr_restore.restore_recovered_deletions(radarr_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Radarr restore failed: {e}")
        if sonarr_ef and sonarr_inst and hasattr(sonarr_ef, "restore_recovered_episode_deletions"):
            try:
                out["sonarr"] = sonarr_ef.restore_recovered_episode_deletions(sonarr_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Sonarr restore failed: {e}")
        return out

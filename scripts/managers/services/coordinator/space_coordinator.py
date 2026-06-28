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
           deletion. Downgrades are non-destructive (restorable re-grabs), so they run
           throughout the band. Re-read free; if free ≥ the floor T — whether recovered
           all the way to U or merely holding in the band (T ≤ free < U) — STOP here: the
           destructive delete pool is floor-gated for hysteresis.
  Stage 2 — (only when free < the floor T) build the COMBINED delete pool: Radarr movie candidates +
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
from scripts.managers.machine_learning.space.routing_targets import (
    evict_uhd_first,
    rehome_4k_only_enabled,
)
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_targets import coordinator_owns_deletion, space_targets


class SpaceCoordinatorManager(BaseManager, ComponentManagerMixin):
    parent_name = "SpaceCoordinatorManager"

    # Last-resort pressure floor — only when free_space_limit is unset AND the shared
    # mount's total size is unreadable (otherwise the floor is 25% of the total drive).
    PRESSURE_FALLBACK_GB = 1000.0

    # FORK-D: global_cache key for the cross-run "rehomed 4K copy awaiting eviction" ledger,
    # per 4K instance. {str(tmdb): {std_inst, uhd_movie_id, uhd_file_id, size_bytes, queued_at}}.
    _PENDING_EVICT_KEY = "radarr/{inst}/pending_4k_evicts"
    # FORK-D: 4K copies already evicted (file gone, record unmonitored) awaiting SPACE recovery to
    # be re-added as a dual-version bonus. {str(tmdb): {std_inst, uhd_movie_id, size_bytes, evicted_at}}.
    _SPACE_EVICTED_KEY = "radarr/{inst}/space_evicted_4k"

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

        # FORK-D RE-ADD (runs on EVERY path, incl. the healthy one below): when free has
        # recovered to a comfortable margin above the band top, re-acquire the 4K copies we
        # evicted under pressure so the title returns to a proper dual-version. Gated well above
        # U so it can never thrash against the evict (which only fires below the floor T).
        readd_stats = None
        if rehome_4k_only_enabled(self.config) and radarr_inst:
            _ruhd_readd = self._uhd_instance(radarr_inst)
            if _ruhd_readd:
                readd_stats = self._readd_space_evicted_4k(radarr_sp, _ruhd_readd, free, U)

        if free >= U:
            self.logger.log_info(
                f"[SpaceCoordinator] {free:.0f} GB ≥ {U:.0f} GB — no space pressure, skipping."
            )
            out = {"enabled": True, "action": "none", "free_space_gb": round(free, 1)}
            if readd_stats is not None:
                out["readd_4k"] = readd_stats
            return out

        stats: dict = {"enabled": True, "free_before_gb": round(free, 1),
                       "downgrades": {}, "deletions": {}, "restores": {}}
        # Bound up-front (None) so the "no candidates" early return can reference uhd_inst
        # for its restore set before the Stage-2 4K block (re)assigns it.
        uhd_inst, uhd_df = None, None

        # ── Stage 1: downgrades (both services, ALL Radarr instances) ───────────
        # All Radarr instances share the one media mount, so the coordinator owns the
        # downgrade pass for EVERY instance (not just the default) — this is what makes
        # it the single point of contact. Per-instance failures are isolated so one bad
        # instance never aborts the others. Downgrades are non-destructive (profile flip
        # + re-grab on recovery), so looping here before the floor gate is safe.
        if radarr_sp:
            stats["downgrades"]["radarr"] = {}
            for inst in self._all_radarr_instances(radarr_sp, radarr_inst):
                try:
                    stats["downgrades"]["radarr"][inst] = radarr_sp.run_downgrades(inst, free)
                except Exception as e:
                    self.logger.log_warning(
                        f"[SpaceCoordinator] Radarr downgrades failed for '{inst}': {e}"
                    )
        if sonarr_sp and sonarr_inst:
            try:
                stats["downgrades"]["sonarr"] = sonarr_sp.run_downgrades(sonarr_inst, free)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Sonarr downgrades failed: {e}")

        free = self._read_free(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst)
        stats["free_after_downgrades_gb"] = round(free, 1)
        # Deletion is floor-gated for hysteresis: it triggers only once free actually
        # breaches the floor T, NOT merely the band top U. While T <= free < U we are in
        # the pressure band — the Stage-1 downgrades above (non-destructive, restorable)
        # have run, but the destructive delete pool HOLDS. This mirrors the per-service
        # run_deletions (``if free >= T: return``) and space_targets.py's documented rule
        # ("T <= free < U → hold steady, deletion loop stops here = hysteresis"). Once
        # free < T, Stage 2 below reclaims all the way back up to U (the high-watermark).
        if free >= T:
            if free >= U:
                self.logger.log_info(
                    f"[SpaceCoordinator] downgrades recovered to {free:.0f} GB ≥ {U:.0f} GB — "
                    f"no deletion needed."
                )
                stats["action"] = "downgrades_only"
            else:
                self.logger.log_info(
                    f"[SpaceCoordinator] {free:.0f} GB ≥ floor {T:.0f} GB (pressure band "
                    f"{T:.0f}–{U:.0f} GB) — downgrades ran; deletion holds until free "
                    f"breaches the floor (hysteresis)."
                )
                stats["action"] = "band_hold"
            # No deletion happened (downgrades only / band hold) → restore the default
            # instance + Sonarr (uhd_inst is still None here, before the Stage-2 4K pass).
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

        # instance_dfs maps each Radarr instance name → its loaded movie_files df so the
        # delete branch can route every pick back to the instance/df it came from. The
        # default instance is keyed first; the 4K pass and Pass-3 add the rest.
        instance_dfs: dict = {}
        if radarr_inst is not None:
            instance_dfs[radarr_inst] = radarr_df

        pool: list[dict] = []
        if radarr_sp and radarr_df is not None and not radarr_df.empty:
            try:
                _std = radarr_sp.build_delete_candidates(radarr_inst, radarr_df)
                for c in _std:
                    c.setdefault("instance", radarr_inst)   # route default-instance deletes correctly
                pool += _std
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
                instance_dfs[uhd_inst] = uhd_df   # route 4K-copy deletes to the 4K instance df
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

        # ── Pass 2b (FORK-D, default-off): queue cold 4K-ONLY films for rehome-then-evict ──
        # A 4K-only film is a 2160p copy on the 4K instance with NO 1080p baseline on standard
        # (the exact inverse of the evict pass's survivors test). Rather than protect it forever
        # (Pass 3 excludes it), queue it: later this run we add a watchability-matched <=1080p
        # copy on standard, and a LATER run evicts the 4K once that copy imports (the drain). Only
        # the coldest titles, up to the reclaim target — never churn-add the whole 4K library.
        rehome_queue: list = []
        if rehome_4k_only_enabled(self.config) and radarr_sp and radarr_inst:
            _ruhd = self._uhd_instance(radarr_inst)
            if _ruhd and _ruhd != radarr_inst:
                uhd_inst = _ruhd                       # make restore/drain aware of the 4K instance
                survivors = self._baseline_survivors(radarr_df)
                _rdf = instance_dfs.get(_ruhd)
                if _rdf is None:
                    try:
                        _rdf = radarr_sp.load_movie_files(_ruhd)
                    except Exception as e:
                        self.logger.log_warning(f"[SpaceCoordinator] FORK-D 4K load failed for '{_ruhd}': {e}")
                        _rdf = None
                    instance_dfs[_ruhd] = _rdf
                # Detect 4K-only films DIRECTLY from the 4K instance df — NOT via
                # build_delete_candidates, whose keep/universe DELETION guards exclude them. A
                # rehome PRESERVES the title (adds a <=1080p baseline BEFORE the 4K is evicted), so
                # keep_universe — "never lose the final copy without first replacing it with a
                # lower-quality one" — is satisfied by construction. Spare only hard manual pins
                # (keep_forever / keep_movie).
                _spared = {"keep_forever", "keep_movie"}
                only4k: list = []
                if _rdf is not None and not _rdf.empty:
                    for _idx in _rdf.index:
                        _row = _rdf.loc[_idx]
                        _fid = _row.get("movie_file_id")
                        _res = pd.to_numeric(_row.get("resolution"), errors="coerce")
                        _tmdb = _row.get("tmdb_id")
                        if (_fid is None or not pd.notna(_fid)               # no 4K file on disk
                                or not (pd.notna(_res) and _res > 1080)      # not a 2160p copy
                                or pd.isna(_tmdb) or int(_tmdb) in survivors  # has a standard baseline
                                or str(_row.get("keep_policy") or "").strip().lower() in _spared):
                            continue
                        only4k.append((_idx, _row))
                    # coldest first (lowest watchability), then biggest file, up to the reclaim target.
                    only4k.sort(key=lambda t: ((float(t[1].get("watchability_score"))
                                                if pd.notna(t[1].get("watchability_score")) else 0.0),
                                               -float(t[1].get("size_bytes") or 0.0)))
                    _budget_gb = max(0.0, U - free)   # GB to reclaim (sizes below are BYTES)
                    _acc_gb = 0.0
                    for _idx, _row in only4k:
                        if _acc_gb >= _budget_gb:
                            break
                        try:
                            _mid = int(_row.get("movie_id"))
                            _fidv = int(_row.get("movie_file_id"))
                        except Exception:
                            continue                  # can't identify the 4K record/file → skip (never guess)
                        _sz = float(_row.get("size_bytes") or 0.0)
                        rehome_queue.append({
                            "tmdb_id": int(_row.get("tmdb_id")), "uhd_inst": _ruhd,
                            "uhd_movie_id": _mid, "uhd_file_id": _fidv,
                            "size_bytes": _sz, "title": _row.get("title"),
                            "row": _row.to_dict(),
                        })
                        _acc_gb += _sz / (1024 ** 3)
                # Always log the count when FORK-D is active, so an empty result is visible (not silent).
                _msg = (f"{len(rehome_queue)} cold 4K-only film(s) on '{_ruhd}' to rehome → standard "
                        f"(evicted once the copy imports)." if rehome_queue
                        else f"no 4K-only films on '{_ruhd}' to rehome.")
                self.logger.log_info(f"[SpaceCoordinator] FORK-D: {_msg}")

        # ── Pass 3: pool the REMAINING Radarr instances (not the default, not the
        # config-labeled 4K instance) as whole-title candidates. They share the one mount,
        # so their low-watchability titles compete in the same ranked pool under all the
        # same keep/score/recent guards. The 4K instance is deliberately EXCLUDED here —
        # its only coordinator-eligible candidates are the baseline-backed bonus copies
        # added in Pass 2; whole-title reclamation of a 4K-only film is the pending
        # rehome-then-evict path, so the coordinator never deletes a 4K-only title today.
        if radarr_sp and radarr_inst:
            protected_uhd = self._uhd_instance(radarr_inst)   # config 4K instance (independent of the evict flag)
            for other_inst in self._all_radarr_instances(radarr_sp, radarr_inst):
                if other_inst == radarr_inst or other_inst == protected_uhd:
                    continue
                try:
                    other_df = radarr_sp.load_movie_files(other_inst)
                except Exception as e:
                    self.logger.log_warning(
                        f"[SpaceCoordinator] movie_files load failed for '{other_inst}': {e}"
                    )
                    other_df = None
                instance_dfs[other_inst] = other_df
                if other_df is not None and not other_df.empty:
                    try:
                        _oc = radarr_sp.build_delete_candidates(other_inst, other_df)
                        for c in _oc:
                            c.setdefault("instance", other_inst)
                        pool += _oc
                    except Exception as e:
                        self.logger.log_warning(
                            f"[SpaceCoordinator] movie candidate build failed for '{other_inst}': {e}"
                        )

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
            deleted_instances: set = set()
        else:
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
            for c in movie_picks:
                c.setdefault("instance", radarr_inst)
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

            # Route each movie pick back to its origin instance's df and delete there. At most
            # one delete call per instance, so the per-instance restore ledger and reclaim
            # accounting are never double-counted. A pick whose instance df failed to load is
            # SKIPPED (never guess the target df → never delete the wrong file).
            deleted_instances = set()
            if radarr_sp and movie_picks:
                picks_by_inst: dict = {}
                for c in movie_picks:
                    picks_by_inst.setdefault(c.get("instance") or radarr_inst, []).append(c)
                stats["deletions"]["radarr"] = {}
                for inst, picks in picks_by_inst.items():
                    idf = instance_dfs.get(inst)
                    if idf is None:
                        self.logger.log_warning(
                            f"[SpaceCoordinator] skipping {len(picks)} delete pick(s) for '{inst}' — "
                            f"no movie_files df loaded (never guess the target df)."
                        )
                        continue
                    try:
                        res = radarr_sp.delete_selected_movie_files(inst, idf, picks)
                        stats["deletions"]["radarr"][inst] = res
                        if res and res.get("deleted"):
                            deleted_instances.add(inst)
                    except Exception as e:
                        self.logger.log_warning(f"[SpaceCoordinator] movie deletion failed for '{inst}': {e}")
            if episode_fids and sonarr_ef:
                try:
                    stats["deletions"]["sonarr"] = sonarr_ef.delete_selected_episode_files(sonarr_inst, episode_fids)
                except Exception as e:
                    self.logger.log_warning(f"[SpaceCoordinator] episode deletion failed: {e}")

            stats["free_after_deletions_gb"] = round(
                self._read_free(radarr_sp, radarr_inst, sonarr_sp, sonarr_inst), 1
            )
            stats["action"] = "deleted"

        # ── FORK-D: execute queued rehomes — add the watchability-matched <=1080p copy on
        # standard + register the deferred 4K eviction. Runs on BOTH the deleted and the
        # no-candidates paths (a cold 4K-only film is exactly when the standard pool is empty).
        # No-op when nothing was queued (flag off → byte-identical).
        if rehome_queue:
            stats["rehomes"] = self._execute_rehomes(rehome_queue, radarr_sp, radarr_inst, uhd_inst)

        # ── Stage 3: restore recovered ──────────────────────────────────────────
        # Always restore the default instance; additionally restore the 4K instance (its
        # ledger may hold evicted copies) and every other instance that deleted this run,
        # so a recovered title on any instance is re-grabbed.
        restore_extra = set(deleted_instances)
        if uhd_inst:
            restore_extra.add(uhd_inst)
        stats["restores"] = self._run_restores(
            radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, extra_insts=restore_extra
        )

        # ── FORK-D: deferred-evict DRAIN — the ONLY place a rehomed 4K copy is deleted, and
        # only once its standard replacement is confirmed imported (see _execute_pending_uhd_evicts).
        if rehome_4k_only_enabled(self.config) and uhd_inst and radarr_inst:
            stats["pending_4k_evicts"] = self._execute_pending_uhd_evicts(radarr_sp, radarr_inst, uhd_inst)
        if readd_stats is not None:
            stats["readd_4k"] = readd_stats
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
    def _all_radarr_instances(self, radarr_sp, default_inst) -> list:
        """Every Radarr instance name on the shared mount, with ``default_inst`` FIRST
        (the 4K baseline-survivors pass must read the standard df before the 4K pass).
        Mirrors RadarrOrchestrationManager._all_instances(); falls back to just the
        default when the api can't enumerate (single-instance setups), so behaviour is
        unchanged there."""
        api = getattr(radarr_sp, "radarr_api", None) if radarr_sp else None
        names: list = []
        if api is not None and hasattr(api, "get_all_radarr_apis"):
            try:
                names = [str(k) for k in api.get_all_radarr_apis().keys()]
            except Exception:
                names = []
        if default_inst:
            names = [default_inst] + [n for n in names if n != default_inst]
        return names

    # ── FORK-D: rehome target-profile resolution ──────────────────────────────────
    def _instance_profiles(self, radarr_sp, inst) -> dict:
        """``{profile_id: name}`` actually configured on a Radarr instance (live
        ``GET qualityprofile``). Empty dict on any failure → caller treats it as
        'cannot resolve a target' and skips the rehome (fail-safe: the 4K copy stays)."""
        api = getattr(radarr_sp, "radarr_api", None) if radarr_sp else None
        if api is None or not hasattr(api, "_make_request"):
            return {}
        try:
            rows = api._make_request(inst, "qualityprofile", fallback=[]) or []
        except Exception:
            return {}
        out: dict = {}
        for r in rows:
            try:
                out[int(r.get("id"))] = r.get("name")
            except (TypeError, ValueError):
                continue
        return out

    def _rehome_target_profile(self, row, radarr_sp, std_inst) -> "int | None":
        """The watchability-matched Radarr quality profile id to grab a rehomed 4K-only film
        at on the standard instance. HARD-CAPPED below the 4K cutoff (a rehome can NEVER
        re-grab 2160p — INV-5), then validated to actually EXIST on ``std_inst``: walk DOWN
        to the best present profile at/below the earned ladder rank, never below the hard
        floor (``routing.movies.rehome_floor_profile``, default 'HD-720p'). Returns None when
        no valid sub-4K profile exists on the instance → the caller skips the rehome and the
        4K copy stays protected (fail-safe to today's behaviour)."""
        try:
            from scripts.managers.machine_learning.likelihood.watch_likelihood import (
                ladder_rank, profile_id_for_likelihood, watch_likelihood,
            )
        except Exception:
            return None
        cfg = self.config or {}
        # Cap the likelihood just below the 4K cutoff so the earned profile is never a 4K tier.
        try:
            uhd_cutoff = float((cfg.get("watch_likelihood", {}) or {}).get("uhd_cutoff", 75.0))
        except (TypeError, ValueError):
            uhd_cutoff = 75.0
        try:
            L = min(float(watch_likelihood(row, config=cfg)), uhd_cutoff - 1.0)
        except Exception:
            return None
        earned_rank = ladder_rank(profile_id_for_likelihood(L, config=cfg), config=cfg)
        # The lowest 4K ladder rank — the UNCONDITIONAL ceiling: a rehome may NEVER return a
        # profile at or above this rank (INV-5), including via the floor fallback below.
        cap_rank = ladder_rank(profile_id_for_likelihood(uhd_cutoff, config=cfg), config=cfg)
        present = self._instance_profiles(radarr_sp, std_inst)
        if not present:
            return None
        floor_name = str(((cfg.get("routing", {}) or {}).get("movies", {}) or {})
                         .get("rehome_floor_profile", "HD-720p")).strip().lower()
        floor_id = next((pid for pid, nm in present.items()
                         if str(nm or "").strip().lower() == floor_name), None)
        floor_rank = ladder_rank(floor_id, config=cfg) if floor_id is not None else -1
        # A misconfigured 4K-tier floor is treated as "no valid floor" so it can never leak a
        # 2160p grab through the fallback — the fail-safe (None → keep the 4K copy) holds instead.
        if cap_rank >= 0 and floor_rank >= cap_rank:
            floor_id, floor_rank = None, -1
        best_id, best_rank = None, -1
        for pid in present:
            r = ladder_rank(pid, config=cfg)
            if r < 0 or r > earned_rank:           # off-ladder or above the capped tier
                continue
            if cap_rank >= 0 and r >= cap_rank:    # 4K tier — never (belt-and-braces vs earned_rank)
                continue
            if floor_rank >= 0 and r < floor_rank:  # below the hard floor
                continue
            if r > best_rank:
                best_id, best_rank = pid, r
        if best_id is not None:
            return int(best_id)
        return int(floor_id) if floor_id is not None else None

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

    def _run_restores(self, radarr_restore, radarr_inst, sonarr_ef, sonarr_inst, *, extra_insts=None) -> dict:
        out: dict = {}
        # Restore the default instance always, plus every other Radarr instance that deleted
        # this run (the 4K-evict instance and any Pass-3 instance). Each instance's restore
        # reads its OWN per-instance ledger and re-grabs titles whose watchability recovered;
        # deduped so a single instance is never restored twice.
        radarr_insts: list = [radarr_inst] if radarr_inst else []
        for inst in (extra_insts or ()):
            if inst and inst != radarr_inst and inst not in radarr_insts:
                radarr_insts.append(inst)
        if radarr_restore and hasattr(radarr_restore, "restore_recovered_deletions"):
            for inst in radarr_insts:
                # Keep the default instance's key as plain "radarr" (byte-identical telemetry for
                # the common single-instance case); extra instances use a per-instance key.
                k = "radarr" if inst == radarr_inst else f"radarr:{inst}"
                try:
                    out[k] = radarr_restore.restore_recovered_deletions(inst)
                except Exception as e:
                    self.logger.log_warning(f"[SpaceCoordinator] Radarr restore failed for '{inst}': {e}")
        if sonarr_ef and sonarr_inst and hasattr(sonarr_ef, "restore_recovered_episode_deletions"):
            try:
                out["sonarr"] = sonarr_ef.restore_recovered_episode_deletions(sonarr_inst)
            except Exception as e:
                self.logger.log_warning(f"[SpaceCoordinator] Sonarr restore failed: {e}")
        return out

    # ── FORK-D: rehome execution + deferred 4K eviction ───────────────────────────
    def _execute_rehomes(self, rehome_queue, radarr_sp, std_inst, uhd_inst) -> dict:
        """For each queued cold 4K-only film, add a watchability-matched <=1080p copy on the
        standard instance (via AcquisitionManager.rehome_to_standard) and, on success, REGISTER
        a pending 4K eviction. NEVER deletes the 4K here — eviction is the drain's job, and only
        once the standard copy has imported."""
        out = {"queued": len(rehome_queue), "rehomed": 0, "skipped": 0, "failed": 0}
        acq = self._mgr("AcquisitionManager")
        if acq is None or not hasattr(acq, "rehome_to_standard"):
            self.logger.log_warning(
                "[SpaceCoordinator] FORK-D: AcquisitionManager unavailable — keeping 4K copies.")
            out["skipped"] = len(rehome_queue)
            return out
        for q in rehome_queue:
            tmdb = q.get("tmdb_id")
            profile_id = self._rehome_target_profile(q.get("row") or {}, radarr_sp, std_inst)
            if profile_id is None:
                self.logger.log_info(
                    f"[SpaceCoordinator] FORK-D: no valid <=1080p profile for '{q.get('title')}' "
                    f"on '{std_inst}' — keeping the 4K copy.")
                out["skipped"] += 1
                continue
            try:
                res = acq.rehome_to_standard(tmdb, std_inst=std_inst, target_profile_id=profile_id)
            except Exception as e:
                self.logger.log_warning(
                    f"[SpaceCoordinator] FORK-D rehome failed for '{q.get('title')}': {e}")
                out["failed"] += 1
                continue
            action = (res or {}).get("action")
            # On any successful add/search (or an already-present standard copy), queue the 4K
            # eviction. would-add/would-search are dry-run actions → _register no-ops in dry_run.
            if action in ("added", "would-add", "searched", "would-search", "already-owned"):
                self._register_pending_uhd_evict(
                    uhd_inst, tmdb, q.get("uhd_movie_id"), q.get("uhd_file_id"),
                    std_inst, q.get("size_bytes"))
                out["rehomed"] += 1
            else:
                self.logger.log_info(
                    f"[SpaceCoordinator] FORK-D rehome '{q.get('title')}' → {action} "
                    f"(no eviction queued).")
                out["skipped"] += 1
        return out

    def _register_pending_uhd_evict(self, uhd_inst, tmdb, uhd_movie_id, uhd_file_id,
                                    std_inst, size_bytes) -> None:
        """Record a 4K copy to evict LATER, once its standard rehome copy imports. Keyed by tmdb;
        re-queuing UPDATES the fields but PRESERVES the original ``queued_at`` so the import-timeout
        clock measures from first queue (a persistent stall really does age out — without this the
        7-day clock would restart every run and never fire). No write under effective_dry_run — a
        preview or backup-disarmed run must never arm a real eviction (INV-4)."""
        if tmdb is None or not uhd_inst:
            return
        if effective_dry_run(self.dry_run, self.global_cache) or not self.global_cache:
            self.logger.log_debug(
                f"[SpaceCoordinator] FORK-D [dry_run] would queue 4K eviction for tmdb {tmdb}.")
            return
        key = self._PENDING_EVICT_KEY.format(inst=uhd_inst)
        try:
            led = self.global_cache.get(key)
            led = led if isinstance(led, dict) else {}
            prev = led.get(str(tmdb)) or {}
            led[str(tmdb)] = {
                "std_inst": std_inst, "uhd_movie_id": uhd_movie_id, "uhd_file_id": uhd_file_id,
                "size_bytes": float(size_bytes or 0.0),
                "queued_at": prev.get("queued_at") or datetime.now(timezone.utc).isoformat(),
            }
            self.global_cache.set(key, led)
        except Exception as e:
            self.logger.log_warning(
                f"[SpaceCoordinator] FORK-D: failed to persist pending evict for {tmdb}: {e}")

    def _execute_pending_uhd_evicts(self, radarr_sp, std_inst, uhd_inst) -> dict:
        """FORK-D DRAIN — evict the rehomed 4K copy TEMPORARILY (restored when space recovers, see
        _readd_space_evicted_4k), never permanently. ONLY when a real standard file for that tmdb is
        confirmed present in a FRESHLY-reloaded standard df (INV-1: no no-copy gap): delete the 4K
        FILE and UNMONITOR the 4K movie (the cheap record is KEPT so re-acquisition is just
        re-monitor+search, and unmonitoring stops Radarr re-grabbing it while still under pressure),
        then move the entry to the space-evicted ledger. A not-yet-imported entry is kept; one older
        than the import timeout is dropped while KEEPING the 4K intact. Re-checks consent; all writes
        guarded by effective_dry_run."""
        out = {"checked": 0, "evicted": 0, "still_waiting": 0, "timed_out": 0, "failed": 0}
        if not uhd_inst or not self.global_cache:
            return out
        if not coordinator_owns_deletion(self.config):     # consent can be revoked between runs
            return out
        key = self._PENDING_EVICT_KEY.format(inst=uhd_inst)
        led = self.global_cache.get(key)
        if not isinstance(led, dict) or not led:
            return out
        # Reload the STANDARD movie_files FRESH so an import that landed since this run started is seen.
        try:
            if radarr_sp and hasattr(radarr_sp, "_get_movie_files_manager"):
                _mfm = radarr_sp._get_movie_files_manager()
                if _mfm is not None and hasattr(_mfm, "reset_run_cache"):
                    _mfm.reset_run_cache()
            std_df = radarr_sp.load_movie_files(std_inst) if radarr_sp else None
        except Exception as e:
            self.logger.log_warning(
                f"[SpaceCoordinator] FORK-D drain: standard reload failed: {e} — keeping all pending evicts.")
            return out
        if std_df is None:
            return out                                     # never guess — keep all entries
        present = self._baseline_survivors(std_df)          # tmdbs with a real file on standard
        api = getattr(radarr_sp, "radarr_api", None) if radarr_sp else None
        try:
            timeout_days = float(((self.config or {}).get("routing", {}) or {}).get("movies", {})
                                 .get("rehome_import_timeout_days", 7) or 7)
        except (TypeError, ValueError):
            timeout_days = 7.0
        now = datetime.now(timezone.utc)
        dry = effective_dry_run(self.dry_run, self.global_cache)
        changed = False
        evicted_led = None
        for tmdb_str, ent in list(led.items()):
            out["checked"] += 1
            try:
                tmdb = int(tmdb_str)
            except (TypeError, ValueError):
                led.pop(tmdb_str, None); changed = True
                continue
            if tmdb in present:                             # standard imported → evict the 4K (temporarily)
                mid, fid = ent.get("uhd_movie_id"), ent.get("uhd_file_id")
                if mid is None or api is None:
                    out["failed"] += 1
                    continue                                # can't act safely → keep the entry
                if dry:
                    self.logger.log_info(
                        f"[SpaceCoordinator] FORK-D [dry_run] would evict 4K copy (movie {mid}, "
                        f"file {fid}) on '{uhd_inst}' — standard rehome imported.")
                    out["evicted"] += 1
                    continue                                # read-only: never mutate the ledger
                try:
                    if fid is not None:
                        api._make_request(uhd_inst, f"moviefile/{fid}", method="DELETE")
                    api._make_request(uhd_inst, "movie/editor", method="PUT",
                                      payload={"movieIds": [mid], "monitored": False})
                except Exception as e:
                    out["failed"] += 1
                    self.logger.log_warning(
                        f"[SpaceCoordinator] FORK-D 4K eviction failed for movie {mid} on "
                        f"'{uhd_inst}': {e}")
                    continue                                # keep the entry; retry next run
                self.logger.log_info(
                    f"[SpaceCoordinator] FORK-D evicted 4K copy (movie {mid}) on '{uhd_inst}' — "
                    f"standard rehome imported; will re-add when space recovers.")
                out["evicted"] += 1
                # Move pending → space-evicted (awaiting space-recovery re-add to dual-version).
                if evicted_led is None:
                    _e = self.global_cache.get(self._SPACE_EVICTED_KEY.format(inst=uhd_inst))
                    evicted_led = _e if isinstance(_e, dict) else {}
                evicted_led[tmdb_str] = {
                    "std_inst": ent.get("std_inst"), "uhd_movie_id": mid,
                    "size_bytes": float(ent.get("size_bytes") or 0.0), "evicted_at": now.isoformat(),
                }
                led.pop(tmdb_str, None); changed = True
            else:                                           # not imported yet — keep unless aged out
                aged = False
                try:
                    aged = (now - datetime.fromisoformat(ent.get("queued_at"))).total_seconds() \
                        > timeout_days * 86400
                except Exception:
                    aged = False
                if aged:
                    self.logger.log_warning(
                        f"[SpaceCoordinator] FORK-D: rehome of tmdb {tmdb} never imported in "
                        f"{timeout_days:.0f}d — abandoning the eviction, KEEPING the 4K copy.")
                    out["timed_out"] += 1
                    if not dry:
                        led.pop(tmdb_str, None); changed = True
                else:
                    out["still_waiting"] += 1
        if changed and not dry:
            try:
                self.global_cache.set(key, led)
                if evicted_led is not None:
                    self.global_cache.set(self._SPACE_EVICTED_KEY.format(inst=uhd_inst), evicted_led)
            except Exception as e:
                self.logger.log_warning(
                    f"[SpaceCoordinator] FORK-D: failed to persist drained ledger: {e}")
        return out

    def _readd_space_evicted_4k(self, radarr_sp, uhd_inst, free, U) -> dict:
        """FORK-D RE-ADD — once free space recovers to a comfortable margin above the band top U
        (``routing.movies.rehome_readd_margin``, default 0.25 → free >= U*1.25), re-acquire the 4K
        copies evicted under pressure: re-monitor + 4K-search each on the 4K instance (the record
        was kept, only unmonitored), so the title becomes a proper dual-version (standard baseline +
        4K bonus). Budgeted so the re-grabs can't drop free back into the band. All writes guarded
        by effective_dry_run. The wide evict(<T) → re-add(>=U*1.25) gap is the anti-thrash hysteresis."""
        out = {"pending": 0, "readded": 0, "skipped": 0, "failed": 0}
        if not uhd_inst or not self.global_cache:
            return out
        ekey = self._SPACE_EVICTED_KEY.format(inst=uhd_inst)
        led = self.global_cache.get(ekey)
        if not isinstance(led, dict) or not led:
            return out
        out["pending"] = len(led)
        try:
            margin = float(((self.config or {}).get("routing", {}) or {}).get("movies", {})
                           .get("rehome_readd_margin", 0.25))
        except (TypeError, ValueError):
            margin = 0.25
        threshold = U * (1.0 + max(0.0, margin))
        if free < threshold:
            return out                                     # not comfortable enough — hold (anti-thrash)
        api = getattr(radarr_sp, "radarr_api", None) if radarr_sp else None
        if api is None:
            return out
        dry = effective_dry_run(self.dry_run, self.global_cache)
        budget_gb = max(0.0, free - U)                     # keep free above the band even if all re-grab
        acc_gb = 0.0
        changed = False
        # Cheapest first → fit the most titles back under the budget (sizes are BYTES, budget is GB).
        for tmdb_str, ent in sorted(led.items(), key=lambda kv: float(kv[1].get("size_bytes") or 0.0)):
            sz_gb = float(ent.get("size_bytes") or 0.0) / (1024 ** 3)
            if acc_gb + sz_gb > budget_gb:
                break
            mid = ent.get("uhd_movie_id")
            if mid is None:
                led.pop(tmdb_str, None); changed = True
                continue
            if dry:
                self.logger.log_info(
                    f"[SpaceCoordinator] FORK-D [dry_run] would re-add 4K (movie {mid}) on "
                    f"'{uhd_inst}' — space recovered.")
                out["readded"] += 1
                acc_gb += sz_gb
                continue
            try:
                api._make_request(uhd_inst, "movie/editor", method="PUT",
                                  payload={"movieIds": [mid], "monitored": True})
                api._make_request(uhd_inst, "command", method="POST",
                                  payload={"name": "MoviesSearch", "movieIds": [mid]})
            except Exception as e:
                out["failed"] += 1
                self.logger.log_warning(
                    f"[SpaceCoordinator] FORK-D 4K re-add failed for movie {mid} on '{uhd_inst}': {e}")
                continue
            self.logger.log_info(
                f"[SpaceCoordinator] FORK-D re-added 4K (movie {mid}) on '{uhd_inst}' — space "
                f"recovered to {free:.0f} GB (>= {threshold:.0f} GB).")
            out["readded"] += 1
            acc_gb += sz_gb
            led.pop(tmdb_str, None); changed = True
        if changed and not dry:
            try:
                self.global_cache.set(ekey, led)
            except Exception as e:
                self.logger.log_warning(
                    f"[SpaceCoordinator] FORK-D: failed to persist space-evicted ledger: {e}")
        return out

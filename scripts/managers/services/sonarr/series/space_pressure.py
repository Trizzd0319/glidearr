"""
SonarrSpacePressureManager — Stage-1 TV downgrade under space pressure
=====================================================================
The Sonarr twin of ``RadarrSpacePressureManager.run_downgrades`` (Phase 3 of the
cross-service space plan). When free space is in the pressure band (free < U),
downgrade the lowest-watchability SERIES to HD-720p and trigger a re-grab, freeing
space BEFORE anything is deleted. Non-destructive and reversible (you keep the
show, just at a lower quality).

Differences from the Radarr movie template:
  * SERIES-level — episode_files.parquet is per-episode, but watchability_score is
    per-series (broadcast onto every row by refresh_scores). We group by series_id,
    score once per series, change the SERIES qualityProfileId (PUT series/{id}),
    and stamp the plan on every episode row of that series.
  * Reads the already-broadcast ``watchability_score`` column (Phase 2) — it does
    NOT recompute scores.
  * Adds a recently-AIRED guard (no Radarr analog): never downgrade a series with an
    episode aired within RECENT_AIR_DAYS.
  * U-target loop — downgrades just enough (lowest score first) to project free ≥ U,
    rather than downgrading the whole low-value catalog at once (avoids a re-grab
    storm on a large TV library). Falls back to "all candidates" if U is unreachable.

Gating lives in the orchestration wrapper (run_space_pressure_downgrades): it only
calls run_downgrades when free < U and ``tv_downgrade_enabled`` is set. dry_run only
changes whether the PUT/search actually fire — the plan is always stamped + persisted
so it is previewable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.machine_learning.ledger.decision_ledger import stamp
from scripts.managers.machine_learning.space.downgrade_planner import plan_series_downgrades
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import space_targets


class SonarrSpacePressureManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    HD_720P_PROFILE_NAME = "HD-720p"
    PRESSURE_FALLBACK_GB = 25.0  # last-resort floor only (free_space_limit unset AND total drive unreadable)
    RECENT_WATCH_DAYS    = 7      # don't downgrade a series watched within this window
    RECENT_AIR_DAYS      = 30     # don't downgrade a series with a very recently aired ep
    DEFAULT_SCORE_CEILING = 20    # tv_space_pressure_score_ceiling default (0-100 scale)
    DEFAULT_RUNTIME_MIN  = 45.0   # fallback per-episode runtime when unknown
    KEEP_TAGS = frozenset({"keep_series", "keep_season", "keep_universe", "keep_forever"})

    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)

        # Resolve dry_run robustly — this manager PUTs to Sonarr, so never default
        # to False silently (the dry_run-propagation footgun). Walk kwargs → parent
        # → SonarrManager → Main; raise if unresolvable.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(manager, "dry_run", None)
        for _root_name in ("SonarrManager", "Main"):
            if _dry_run is not None:
                break
            if self.registry:
                try:
                    _root = self.registry.get("manager", _root_name)
                    _dry_run = getattr(_root, "dry_run", None) if _root else None
                except Exception:
                    _dry_run = None
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run from kwargs, "
                f"SonarrManager, or Main. Refusing to initialize without an explicit "
                f"value to prevent accidental live profile changes."
            )
        self.dry_run = bool(_dry_run)

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    def prepare(self):
        pass

    def run(self):
        # No-op for the SonarrSeriesManager component-iteration; the downgrade pass
        # is driven by the orchestration (run_space_pressure_downgrades) AFTER
        # refresh_scores so it operates on fresh watchability scores.
        return {}

    # ── Helpers ───────────────────────────────────────────────────────────────────

    def get_free_space_gb(self, instance: str) -> float:
        """Free space (GiB) across this instance's disks, mount-deduped."""
        if self.sonarr_api is None:
            return float("inf")
        return self.sonarr_api.disk_free_gb(instance)

    def _space_targets(self, instance: str | None = None) -> tuple[float, float]:
        """(T, U) from the shared helper. When ``free_space_limit`` is unset the floor
        defaults to 25% of the total drive (mount-deduped via ``disk_total_gb``);
        PRESSURE_FALLBACK_GB is the last resort only when the total is also unreadable."""
        total_gb = None
        if instance is not None and self.sonarr_api is not None:
            try:
                total_gb = self.sonarr_api.disk_total_gb(instance)
            except Exception:
                total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Sonarr", instance, total_gb)
        return space_targets(self.config, fallback_gb=self.PRESSURE_FALLBACK_GB, total_gb=total_gb)

    def _score_ceiling(self) -> float:
        try:
            return float((self.config or {}).get("tv_space_pressure_score_ceiling", self.DEFAULT_SCORE_CEILING))
        except (TypeError, ValueError):
            return float(self.DEFAULT_SCORE_CEILING)

    def _get_episode_files_manager(self):
        try:
            return self.registry.get("manager", "SonarrCacheEpisodeFilesManager")
        except Exception:
            return None

    @timeit("_fetch_hd720p_profile")
    def _fetch_hd720p_profile(self, instance: str) -> dict | None:
        """Fetch the HD-720p quality profile from Sonarr by exact name (Sonarr's
        endpoint is camelCase ``qualityProfile``; Radarr's is lowercase)."""
        if self.sonarr_api is None:
            return None
        profiles = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        for p in profiles:
            if (p.get("name") or "").strip().lower() == self.HD_720P_PROFILE_NAME.lower():
                return p
        self.logger.log_warning(
            f"⚠️ [SpacePressure-TV] quality profile '{self.HD_720P_PROFILE_NAME}' not found in "
            f"'{instance}'. Available: {[p.get('name') for p in profiles]}"
        )
        return None

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        """Max allowed resolution of a Sonarr quality profile (0 if none)."""
        from scripts.managers.machine_learning.sizing.size_model import profile_max_quality
        res, _ = profile_max_quality(profile) if profile else (-1, None)
        return res if isinstance(res, (int, float)) and res > 0 else 0

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """All Sonarr quality profiles sorted ascending by max allowed resolution — the
        ladder the step-down downgrade walks one resolution tier at a time."""
        if self.sonarr_api is None:
            return []
        raw = self.sonarr_api._make_request(instance, "qualityProfile", fallback=[]) or []
        return sorted(raw, key=self._profile_max_resolution)

    @staticmethod
    def _ensure_plan_cols(df) -> None:
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

    def _stamp_plan(self, df, idx, action: str, reason: str, reclaim_gb) -> None:
        # Delegates the ledger write to the brain (ledger.decision_ledger.stamp).
        stamp(df, idx, action, reason, reclaim_gb)

    # NOTE: the _max_ts helper moved to the brain (space.downgrade_planner) in ML Step 7c.

    # ── Stage 1: downgrade to HD-720p ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_tv_downgrades")
    def run_downgrades(self, instance: str, free_space_gb: float) -> dict:
        """Downgrade the lowest-watchability series to HD-720p until projected free
        space reaches U. Series-level; stamps the plan on every episode row.
        ``free_space_gb`` is the current free space (the orchestration already
        verified free < U before calling)."""
        stats = {
            "candidates":        0,
            "downgraded":        0,
            "est_reclaim_gb":    0.0,
            "skipped_protected": 0,
            "skipped_high_score": 0,
            "skipped_recent":    0,
            "skipped_already":   0,
            "failed":            0,
        }

        ef = self._get_episode_files_manager()
        if ef is None:
            self.logger.log_warning("[SpacePressure-TV] episode_files manager unavailable — skipping downgrades")
            return stats
        df = ef.load(instance)
        if df.empty or "series_id" not in df.columns:
            return stats
        if "watchability_score" not in df.columns:
            self.logger.log_warning("[SpacePressure-TV] no watchability_score column — run refresh_scores first")
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning("[SpacePressure-TV] Could not fetch quality profiles — skipping downgrades")
            return stats
        # Series floor at the HD-720p resolution: they step DOWN toward it (4K → 1080p →
        # 720p) but never below.
        hd720p = self._fetch_hd720p_profile(instance)
        floor_resolution = (self._profile_max_resolution(hd720p) or 720) if hd720p is not None else 720

        self._ensure_plan_cols(df)
        # Clear any stale downgrade plan from a prior run so the ledger reflects THIS
        # run's decision (leave delete/acquire/upgrade plans untouched).
        _stale = df["planned_action"] == "downgrade"
        if _stale.any():
            df.loc[_stale, ["planned_action", "plan_reason", "plan_reclaim_gb"]] = None

        _, U = self._space_targets(instance)
        need_gb = max(0.0, U - float(free_space_gb))
        ceiling = self._score_ceiling()
        now = datetime.now(tz=timezone.utc)
        watch_cutoff = now - timedelta(days=self.RECENT_WATCH_DAYS)
        air_cutoff   = now - timedelta(days=self.RECENT_AIR_DAYS)

        # DECISION: the brain (space.downgrade_planner.plan_series_downgrades) steps the
        # lowest-watchability series DOWN the resolution ladder one tier at a time, spread
        # across the pool, until ~need_gb is reclaimed (no series crushed straight to 720p).
        # The service APPLIES each per-series target (PUT + SeriesSearch + stamp) below.
        candidates, _pstats = plan_series_downgrades(
            df, ranked_profiles,
            need_gb=need_gb,
            ceiling=ceiling,
            watch_cutoff=watch_cutoff,
            air_cutoff=air_cutoff,
            keep_tags=self.KEEP_TAGS,
            default_runtime_min=self.DEFAULT_RUNTIME_MIN,
            floor_resolution=floor_resolution,
        )
        stats.update(_pstats)
        if not candidates:
            self.logger.log_info(
                f"[SpacePressure-TV] '{instance}': {free_space_gb:.0f}GB free (<{U:.0f}GB) but no "
                f"downgrade candidates (score<{ceiling:.0f}, not keep/recent/at-floor)."
            )
            return stats

        # Apply each per-series step-down target (the planner already spread to ~need_gb).
        ids_to_search: list[int] = []
        plan_changed = changed = False
        reclaimed = 0.0

        for c in candidates:
            reason = f"{c['reason']} → {c['target_name']}"
            # Stamp the series-level downgrade on ONE representative episode row with
            # the WHOLE-series reclaim. The plan ledger (plan_summary.py) counts rows
            # and sums plan_reclaim_gb per planned_action, so stamping every episode
            # row of the series would inflate BOTH the count (~n_eps) and the GB freed.
            self._stamp_plan(df, c["indices"][0], "downgrade", reason, c["reclaim"])
            plan_changed = True
            reclaimed += c["reclaim"]
            stats["est_reclaim_gb"] = round(reclaimed, 1)

            if self.dry_run:
                self.logger.log_info(
                    f"  📉 [dry_run] Would step down '{c['title']}' ({c['n_eps']} ep, "
                    f"{c['cur_gib']:.1f}GB → {c['target_name']}, ~{c['reclaim']:.1f}GB reclaim) — {c['reason']}"
                )
                stats["downgraded"] += 1
                continue

            try:
                payload = self.sonarr_api._make_request(instance, f"series/{c['sid']}", fallback=None)
                if not payload or not isinstance(payload, dict):
                    self.logger.log_warning(f"  ⚠️ Could not fetch series payload for '{c['title']}' (id={c['sid']})")
                    stats["failed"] += 1
                    continue
                payload["qualityProfileId"] = c["target_id"]
                self.sonarr_api._make_request(instance, f"series/{c['sid']}", method="PUT", payload=payload)
                ids_to_search.append(c["sid"])
                changed = True
                stats["downgraded"] += 1
                self.logger.log_info(
                    f"  📉 Stepped down '{c['title']}' ({c['n_eps']} ep, {c['cur_gib']:.1f}GB → "
                    f"{c['target_name']}, ~{c['reclaim']:.1f}GB) — {c['reason']}"
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Downgrade failed for '{c['title']}' (id={c['sid']}): {e}")
                stats["failed"] += 1

        # ── trigger re-grab at the new (lower) profile, one SeriesSearch per series ──
        for sid in ids_to_search:
            try:
                self.sonarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "SeriesSearch", "seriesId": sid},
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ SeriesSearch trigger failed for series {sid}: {e}")
        if ids_to_search:
            self.logger.log_info(f"  🔍 SeriesSearch triggered for {len(ids_to_search)} series")

        if plan_changed or changed:
            ef.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_info(
            f"[SpacePressure-TV] {prefix}'{instance}': {free_space_gb:.0f}GB free → target {U:.0f}GB "
            f"(need ~{need_gb:.0f}GB) | {stats['downgraded']} stepped down (~{stats['est_reclaim_gb']:.0f}GB, "
            f"target {'met' if _pstats.get('target_met') else 'NOT met'}) | "
            f"{stats['candidates']} candidate(s) | {stats['skipped_high_score']} score≥{ceiling:.0f} | "
            f"{stats['skipped_protected']} keep | {stats['skipped_recent']} recent | "
            f"{stats['skipped_already']} at/below floor | {stats['failed']} failed"
        )
        return stats

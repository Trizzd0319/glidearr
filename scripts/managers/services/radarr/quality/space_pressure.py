"""
RadarrSpacePressureManager
==========================
Space-pressure quality manager for Radarr.

When free space falls below the pressure band (free < U, where the floor
T = ``free_space_limit`` or 25% of the total drive when that's unset — see
space.space_targets; PRESSURE_THRESHOLD_GB is only the last-resort floor when the
drive's total size is also unreadable) this manager runs a two-stage pipeline:

STAGE 1 — DOWNGRADE TO HD-720P
    Identifies low-priority movies and sets their Radarr quality profile to
    HD-720p, then triggers a MovieSearch so Radarr's cutoff-unmet logic
    fetches the 720p file and replaces the existing one.

    Low-priority candidates (all scored via the same affinity matrix used
    for Trakt auto-rating):
      a. Score below WATCHABILITY_PROTECT_THRESHOLD — genre/actor/director
         affinity + completion + collection bonus say the household is
         unlikely to watch this again.
      b. Unwatched movies (not keep-forever/keep-movie) — never seen at all.
      c. Movies in a collection where any member was watched in the last 30d
         (likely-to-be-watched-soon — keep them, but at 720p for now).
      d. Universe-tagged movies (quality-change only, never deleted).

    Excluded from downgrades:
      * keep_forever / keep_movie tagged
      * Already at or below HD-720p
      * Watched within the last 7 days
      * Score >= WATCHABILITY_PROTECT_THRESHOLD (household likely to re-watch)

STAGE 2 — DELETE (LAST RESORT)
    Only runs if still below threshold after downgrades are queued.
    Only targets watched + grace-expired + already-at-720p movies.
    Prioritises lowest-score movies first.

    NEVER deletes: universe, franchise entries, keep-forever, keep-movie,
    or anything watched within the last 30 days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.managers.machine_learning.ledger.decision_ledger import stamp
from scripts.managers.machine_learning.scoring.critic import critic_avg
from scripts.managers.machine_learning.space.delete_planner import (
    bare_universe_protected,
    build_movie_delete_candidates,
)
from scripts.managers.machine_learning.space.downgrade_planner import (
    plan_movie_downgrades,
    UNIVERSE_PROTECT_MIN,
)
from scripts.managers.machine_learning.space.upgrade_planner import (
    plan_movie_upgrades,
)
from scripts.managers.machine_learning.likelihood.watch_likelihood import (
    movie_universe_credits,
)
from scripts.managers.machine_learning.playlists.models import PLACEHOLDER_AFFINITY
from scripts.managers.machine_learning.sizing import anomaly as size_anomaly
from scripts.support.utilities.backup_gate import effective_dry_run
from scripts.support.utilities.watch_likelihood import (
    affinity_boost as _affinity_boost,
)
from scripts.support.utilities.space_floor_alert import alert_unconfigured_floor
from scripts.support.utilities.space_targets import (
    coordinator_owns_deletion, deletions_disabled_reason, deletions_enabled, space_targets,
)


class RadarrSpacePressureManager(BaseManager, ComponentManagerMixin):

    PRESSURE_THRESHOLD_GB          = 25.0   # last-resort floor only (free_space_limit unset AND total drive unreadable)
    HD_720P_PROFILE_NAME           = "HD-720p"
    RECENT_WATCH_DAYS              = 7
    COLLECTION_WINDOW_DAYS         = 30
    WATCHABILITY_PROTECT_THRESHOLD = 6   # score >= this → protect from downgrade

    parent_name = "RadarrQualityManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)

        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(parent, "dry_run", None) if parent else None
        if _dry_run is None and self.registry:
            try:
                _root = self.registry.get("manager", "RadarrManager")
                _dry_run = getattr(_root, "dry_run", None) if _root else None
            except Exception:
                pass
        if _dry_run is None and self.registry:
            try:
                _main = self.registry.get("manager", "Main")
                _dry_run = getattr(_main, "dry_run", None) if _main else None
            except Exception:
                pass
        if _dry_run is None:
            raise ValueError(
                f"❌ {self.__class__.__name__} could not resolve dry_run. "
                f"Refusing to initialize without an explicit value from config.json."
            )
        self.dry_run = bool(_dry_run)
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (dry_run={self.dry_run})")

    # ── Helpers ──────────────────────────────────────────────────────────────────

    def _resolve_instance(self, instance: str | None) -> str:
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_movie_files_manager(self):
        try:
            return self.registry.get("manager", "RadarrCacheMovieFilesManager")
        except Exception:
            return None

    @timeit("_get_free_space_gb")
    def _get_free_space_gb(self, instance: str) -> float:
        if self.radarr_api is None:
            return float("inf")
        # Mount-deduped — root folders sharing a disk must not be summed twice.
        return self.radarr_api.disk_free_gb(instance)

    def _space_targets(self, instance: str | None = None) -> tuple[float, float]:
        """(T, U) from the shared helper — T = ``free_space_limit`` floor, U = top of
        the pressure band. When ``free_space_limit`` is unset the floor defaults to
        25% of the total drive (mount-deduped via ``disk_total_gb``); PRESSURE_THRESHOLD_GB
        is only the last resort when the total drive size is also unreadable."""
        total_gb = None
        if instance is not None and self.radarr_api is not None:
            try:
                total_gb = self.radarr_api.disk_total_gb(instance)
            except Exception:
                total_gb = None
        alert_unconfigured_floor(self.config, self.logger, "Radarr", instance, total_gb)
        return space_targets(self.config, fallback_gb=self.PRESSURE_THRESHOLD_GB, total_gb=total_gb)

    def _coordinator_owns_deletion(self) -> bool:
        """When the cross-service space coordinator owns deletion, this manager keeps
        its upgrade + downgrade stages but skips its own delete loop (the coordinator
        deletes movies + TV together on one ranked pool)."""
        return coordinator_owns_deletion(self.config)

    def _universe_delete_age_days(self) -> "int | None":
        """On-disk dwell (days) a bare 'universe' title must reach before it becomes
        delete-eligible. None when unset / <= 0 → no ageing guard (byte-identical)."""
        try:
            v = int((self.config or {}).get("universe_delete_age_days", 0) or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    @staticmethod
    def _fmt_bytes(n: "int | float | None") -> str:
        if n is None or n != n:
            return "0 B"
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(n) < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ── Decision-ledger helpers ──────────────────────────────────────────────────
    @staticmethod
    def _ensure_plan_cols(df) -> None:
        for _c in ("planned_action", "plan_reason", "plan_reclaim_gb"):
            if _c not in df.columns:
                df[_c] = None
        # Reloaded all-null Parquet columns come back as float64; force the
        # string-plan columns to object so _stamp_plan's str assignments don't
        # trip pandas' incompatible-dtype FutureWarning.
        for _c in ("planned_action", "plan_reason"):
            if df[_c].dtype != object:
                df[_c] = df[_c].astype(object)

    def _stamp_plan(self, df, idx, action: str, reason: str, reclaim_gb) -> None:
        """Record a planned action on a row (preview-safe; persisted in dry_run).
        ``reclaim_gb`` is +GiB freed (delete/downgrade) or -GiB consumed (upgrade).
        Ensures the ledger columns exist, then delegates the write to the brain
        (ledger.decision_ledger.stamp)."""
        self._ensure_plan_cols(df)
        stamp(df, idx, action, reason, reclaim_gb)

    @timeit("_fetch_hd720p_profile")
    def _fetch_hd720p_profile(self, instance: str) -> dict | None:
        """Fetch the HD-720p quality profile from Radarr by exact name."""
        if self.radarr_api is None:
            return None
        profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        for p in profiles:
            if (p.get("name") or "").strip().lower() == self.HD_720P_PROFILE_NAME.lower():
                return p
        self.logger.log_warning(
            f"⚠️ Quality profile '{self.HD_720P_PROFILE_NAME}' not found in '{instance}'. "
            f"Available: {[p.get('name') for p in profiles]}"
        )
        return None

    def _downgrade_protect_threshold(self) -> int:
        """Watchability floor BELOW which movies are stepped down under space pressure.

        Default = WATCHABILITY_PROTECT_THRESHOLD (6): only the near-unwatched step down.
        When space_pressure_downgrade_before_delete is on, widen it to MATCH the delete
        ceiling (space_pressure_score_ceiling, default 20) so any title the coordinator
        could delete is shrunk to 720p FIRST — deletion becomes the last resort."""
        if self.config and self.config.get("space_pressure_downgrade_before_delete", False):
            try:
                return int(self.config.get("space_pressure_score_ceiling", 20))
            except (TypeError, ValueError):
                return 20
        return self.WATCHABILITY_PROTECT_THRESHOLD

    @staticmethod
    def _profile_max_resolution(profile: dict) -> int:
        """Highest resolution among a profile's *allowed* quality items (incl. nested
        group items); 0 if none. Used as the downgrade-floor resolution so titles already
        at/below it are never 'downgraded' upward."""
        best = 0
        for item in (profile.get("items") or []):
            if not item.get("allowed"):
                continue
            res = (item.get("quality") or {}).get("resolution", 0)
            if isinstance(res, (int, float)):
                best = max(best, int(res))
            for sub in (item.get("items") or []):
                if sub.get("allowed"):
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
        return best

    def _fetch_ranked_profiles(self, instance: str) -> list[dict]:
        """All Radarr quality profiles sorted ascending by max allowed resolution — the
        ladder the step-down downgrade walks one rank at a time."""
        if self.radarr_api is None:
            return []
        raw = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        return sorted(raw, key=self._profile_max_resolution)

    @timeit("_build_active_collection_set")
    def _build_active_collection_set(self, df: pd.DataFrame) -> set[str]:
        """
        Return collection_names where any member was watched within
        COLLECTION_WINDOW_DAYS.
        """
        if "collection_name" not in df.columns or "last_watched_at" not in df.columns:
            return set()

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        active: set[str] = set()

        watched_mask = df["is_watched"].infer_objects(copy=False).fillna(False).astype(bool)
        for _, row in df[watched_mask].iterrows():
            coll = row.get("collection_name")
            if not coll or pd.isna(coll):
                continue
            lw = row.get("last_watched_at")
            if not lw:
                continue
            try:
                if pd.to_datetime(lw, utc=True) >= cutoff:
                    active.add(str(coll))
            except Exception:
                pass

        return active

    # ── Affinity scoring ──────────────────────────────────────────────────────────

    @timeit("_build_affinity_inputs")
    def _build_affinity_inputs(self, instance: str) -> tuple[dict, set[int], dict[int, set[int]]]:
        """
        Pull the same affinity inputs used by run_movie_ratings() from global_cache.

        Returns (genre_affinity, watched_tmdb_ids, collection_members).
        """
        genre_affinity: dict = {}
        watched_tmdb_ids: set[int] = set()
        collection_members: dict[int, set[int]] = {}

        if not self.global_cache:
            return genre_affinity, watched_tmdb_ids, collection_members

        genre_affinity = self.global_cache.get("tautulli/affinity") or {}

        trakt_history = self.global_cache.get("trakt/history/movies") or []
        for entry in trakt_history:
            tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
            if tmdb_id:
                watched_tmdb_ids.add(int(tmdb_id))

        rating_groups_cfg = (self.config.get("rating_groups", {}) if self.config else {})
        for group_name in (rating_groups_cfg or {"household": {}}):
            raw = self.global_cache.get(f"tautulli/group/{group_name}/tmdb_completions") or {}
            for tmdb_str in raw:
                try:
                    watched_tmdb_ids.add(int(tmdb_str))
                except (ValueError, TypeError):
                    pass

        movies = self.global_cache.get(f"radarr.movies.{instance}.full") or []
        for m in movies:
            coll    = m.get("collection") or {}
            coll_id = coll.get("tmdbId")
            mid     = m.get("tmdbId")
            if coll_id and mid:
                collection_members.setdefault(int(coll_id), set()).add(int(mid))

        return genre_affinity, watched_tmdb_ids, collection_members

    @timeit("_score_row")
    def _score_row(self, row: "pd.Series | dict", genre_affinity: dict,
                   watched_tmdb_ids: set[int], collection_members: dict[int, set[int]],
                   people_manager=None,
                   platform_usage: dict | None = None,
                   transcode_stats: dict | None = None,
                   per_user_affinity: dict | None = None,
                   kids_users: list[str] | None = None,
                   adult_users: list[str] | None = None,
                   related_enabled: bool = False,
                   related_graph_cap: float = 4.0,
                   person_weights: dict | None = None,
                   person_affinity_cap: float = 0.0,
                   language_consumability: bool = False,
                   return_breakdown: bool = False) -> "int | tuple[int, dict]":
        """
        Score a single movie_files Parquet row using score_movie(). ``row`` is a
        pandas Series OR a plain dict (from df.to_dict("records")) — build_movie_feature_row
        reads it through row.get()+pd.notna() coercion, so both yield an identical score.
        Returns [0, 100], defaults to 30 on error. When ``return_breakdown`` is
        True, returns ``(score, breakdown)`` instead — the breakdown is the flat
        per-signal-group contribution dict the pure scorer already produces, so
        the returned score is byte-identical either way.
        """
        # ML Step 3c: the row->MovieFeatureRow marshalling + the score_movie call now
        # live in the brain boundary adapter (machine_learning.features.movie_features).
        # The service keeps only the I/O (credits + related set) and the config view.
        from scripts.managers.machine_learning.features.movie_features import (
            build_movie_feature_row, score_movie_features,
        )

        try:
            tmdb_id = row.get("tmdb_id")

            credits: dict = {}
            if people_manager and pd.notna(tmdb_id):
                try:
                    credits = people_manager.get_people(int(tmdb_id)) or {}
                except Exception:
                    pass

            # GROUP C3 — collaborative related-graph affinity (daemon-cached neighbours).
            related_tmdb_ids = (
                self._load_related_tmdb_ids(int(tmdb_id))
                if (related_enabled and pd.notna(tmdb_id)) else None
            )

            fr = build_movie_feature_row(row, credits=credits, related_tmdb_ids=related_tmdb_ids)
            return score_movie_features(
                fr,
                genre_affinity=genre_affinity,
                watched_tmdb_ids=watched_tmdb_ids,
                collection_members=collection_members,
                platform_usage=platform_usage,
                transcode_stats=transcode_stats,
                per_user_affinity=per_user_affinity,
                kids_users=kids_users,
                adult_users=adult_users,
                completion_threshold=0.9,
                affinity_boost=_affinity_boost(self.config),
                related_graph_cap=related_graph_cap,
                person_weights=person_weights,
                person_affinity_cap=person_affinity_cap,
                language_consumability=language_consumability,
                return_breakdown=return_breakdown,
            )
        except Exception:
            return (30, {}) if return_breakdown else 30

    @timeit("_build_score_map")
    def _build_score_map(self, df: pd.DataFrame, instance: str,
                         with_breakdown: bool = False) -> dict:
        """Return ``{df_index: watchability_score}`` for every row using affinity
        cache data. When ``with_breakdown`` is True, the value is instead
        ``(score, breakdown)`` — the score is identical, only the explanation dict
        is added (the persistence path uses this; decision paths don't)."""
        genre_affinity, watched_tmdb_ids, collection_members = self._build_affinity_inputs(instance)

        # Pull device/transcode/per-user context from global_cache
        platform_usage:   dict | None = None
        transcode_stats:  dict | None = None
        per_user_affinity: dict | None = None
        kids_users: list[str] = []
        adult_users: list[str] = []

        if self.global_cache:
            platform_usage  = self.global_cache.get("tautulli/platforms") or None
            transcode_stats = self.global_cache.get("tautulli/transcode") or None
            per_user_affinity = {}
            # Load per-user affinity from each user's cache key
            try:
                import re
                users_dir = "tautulli/users"
                # Iterate known users from config rating_groups
                cfg_groups = (self.config or {}).get("rating_groups", {})
                for group in cfg_groups.values():
                    for member in (group.get("members") or []):
                        safe = re.sub(r'[\\/:*?"<>|]', '_', member).strip()
                        ua   = self.global_cache.get(f"tautulli/users/{safe}/affinity")
                        if ua:
                            per_user_affinity[member] = ua
                    for member in (group.get("grace_members") or []):
                        kids_users.append(member)
                    for member in (group.get("members") or []):
                        if member not in kids_users:
                            adult_users.append(member)
            except Exception:
                pass

        people_manager = None
        try:
            trakt_movies   = self.registry.get("manager", "TraktMoviesManager")
            people_manager = getattr(trakt_movies, "people", None) if trakt_movies else None
        except Exception:
            pass

        # GROUP C3 — related-graph collaborative affinity (config.scoring.related_graph).
        _rg = ((self.config or {}).get("scoring", {}) or {}).get("related_graph", {}) or {}
        related_enabled = bool(_rg.get("enabled", True))
        try:
            related_graph_cap = float(_rg.get("cap", 4.0))
        except (TypeError, ValueError):
            related_graph_cap = 4.0
        # File-aware G1 language gate (oracle-mover, default OFF) — see episode_files.
        _lc = ((self.config or {}).get("scoring", {}) or {}).get("language_consumability", {}) or {}
        language_consumability = bool(_lc.get("enabled", False)) if isinstance(_lc, dict) else bool(_lc)

        # GROUP C4 — cast/crew taste overlap (config.scoring.person_affinity). Load the
        # household person-affinity ONCE per pass; the shared resolver forces cap=0.0
        # (byte-identical) when the term is disabled or the people-matrix has never been
        # built, so libraries without it score exactly as before.
        from scripts.managers.machine_learning.scoring._shared import resolve_person_affinity_inputs
        _aff_raw = self.global_cache.get("people_matrix/affinity") if self.global_cache else None
        person_weights, person_affinity_cap = resolve_person_affinity_inputs(self.config, _aff_raw)

        # Iterate plain row dicts (one to_dict("records") pass) rather than building a
        # fresh pd.Series per row via df.loc[idx] — the classic per-row anti-pattern over
        # a few-thousand-row library. build_movie_feature_row reads every field through
        # row.get(col) + pd.notna() coercion, so a dict row yields a byte-identical
        # MovieFeatureRow (and thus an identical score) — see features/test_movie_features.
        return {
            idx: self._score_row(
                row,
                genre_affinity=genre_affinity,
                watched_tmdb_ids=watched_tmdb_ids,
                collection_members=collection_members,
                people_manager=people_manager,
                platform_usage=platform_usage,
                transcode_stats=transcode_stats,
                per_user_affinity=per_user_affinity,
                kids_users=kids_users,
                adult_users=adult_users,
                related_enabled=related_enabled,
                related_graph_cap=related_graph_cap,
                person_weights=person_weights,
                person_affinity_cap=person_affinity_cap,
                language_consumability=language_consumability,
                return_breakdown=with_breakdown,
            )
            for idx, row in zip(df.index, df.to_dict("records"))
        }

    def _load_related_tmdb_ids(self, tmdb_id: int) -> set[int]:
        """Read this movie's daemon-cached Trakt related set (cache-only) and return
        the related neighbours' TMDb ids. Empty set when uncached / empty / unreadable
        — so the C3 term degrades gracefully to 0 until the daemon fills the bucket.

        The daemon writes ``movie_related/{tmdb_id}.json.gz`` as a bare list of movie
        objects, each ``{"ids": {"tmdb": ..., ...}, "title": ..., "year": ...}``.
        """
        import gzip
        import json
        from scripts.managers.factories.daemons.daemon_paths import MOVIE_BUCKETS

        try:
            path = MOVIE_BUCKETS["related"] / f"{int(tmdb_id)}.json.gz"
            if not path.exists():
                return set()
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return set()
        out: set[int] = set()
        for entry in (data or []):
            tid = ((entry or {}).get("ids") or {}).get("tmdb")
            if tid:
                try:
                    out.add(int(tid))
                except (TypeError, ValueError):
                    continue
        return out

    # ── Stage 1: downgrade to HD-720p ────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure_downgrades")
    def run_downgrades(self, instance: str, free_space_gb: float) -> dict:
        """
        Set low-priority/low-score movies to HD-720p and trigger MovieSearch.
        Movies with watchability score >= WATCHABILITY_PROTECT_THRESHOLD are protected.
        """
        stats = {
            "candidates_found":   0,
            "downgraded":         0,
            "already_at_720p":    0,
            "skipped_protected":  0,
            "skipped_high_score": 0,
            "skipped_recent":     0,
            "skipped_universe":   0,
            "failed":             0,
        }

        mfm = self._get_movie_files_manager()
        if mfm is None:
            self.logger.log_warning("[SpacePressure] movie_files manager unavailable — skipping downgrades")
            return stats

        df = mfm.load(instance)
        if df.empty:
            return stats

        ranked_profiles = self._fetch_ranked_profiles(instance)
        if not ranked_profiles:
            self.logger.log_warning("[SpacePressure] Could not fetch quality profiles — skipping downgrades")
            return stats
        # Movies floor at the HD-720p resolution: they step DOWN toward it (4K → 1080p →
        # 720p) but never below (universe titles, which may reach SD, are handled by the
        # universe manager).
        hd720p = self._fetch_hd720p_profile(instance)
        floor_resolution = (self._profile_max_resolution(hd720p) or 720) if hd720p is not None else 720

        now           = datetime.now(tz=timezone.utc)
        recent_cutoff = now - timedelta(days=self.RECENT_WATCH_DAYS)
        active_colls  = self._build_active_collection_set(df)
        score_map     = self._build_score_map(df, instance)

        _floor_gb, U = self._space_targets(instance)
        need_gb = max(0.0, U - float(free_space_gb))
        self.logger.log_info(
            f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free "
            f"(floor {_floor_gb:.0f} GB; need ~{need_gb:.0f} GB to band top {U:.0f} GB). "
            f"Active collections last {self.COLLECTION_WINDOW_DAYS}d: {len(active_colls)}"
        )

        # DECISION (ML Step 7c): the brain (space.downgrade_planner.plan_movie_downgrades)
        # steps the lowest-watchability movies DOWN the ranked ladder one rank at a time,
        # spread across the pool, until ~need_gb is reclaimed (no single title crushed to
        # the floor). The service APPLIES each per-title target (PUT + search + stamp).
        # Optional: widen the downgrade band to MATCH the delete band, so any title the
        # coordinator could delete is shrunk to 720p FIRST and only deleted if downgrades
        # can't free enough (make-before-break via Radarr's replace; deletion = last resort).
        protect = self._downgrade_protect_threshold()
        candidates, _pstats = plan_movie_downgrades(
            df, score_map, ranked_profiles,
            need_gb=need_gb,
            recent_cutoff=recent_cutoff,
            active_colls=active_colls,
            protect_threshold=protect,
            floor_resolution=floor_resolution,
        )
        stats.update(_pstats)

        if not candidates:
            self.logger.log_info("[SpacePressure] No downgrade candidates found.")
            return stats

        self.logger.log_info(
            f"[SpacePressure] {len(candidates)} step-down candidate(s) "
            f"(~{_pstats.get('est_reclaim_gb', 0):.0f} GB projected, "
            f"target {'met' if _pstats.get('target_met') else 'NOT met — deletions cover the rest'}):"
        )

        changed = False
        plan_changed = False
        movie_ids_to_search: list[int] = []

        for c in candidates:
            idx         = c["idx"]
            movie_id    = c["movie_id"]
            target_id   = c["target_id"]
            target_name = c["target_name"]
            reason      = c["reason"]
            reclaim     = c["reclaim_gb"]
            cur_qp_name = c["cur_name"]
            title       = df.at[idx, "title"] or f"movie {movie_id}"
            _sz_raw     = df.at[idx, "size_bytes"] if "size_bytes" in df.columns else None
            _sz_f       = float(_sz_raw) if pd.notna(_sz_raw) else 0.0

            # Decision ledger: record the step-down plan + its (cumulative) reclaim.
            # Persists in dry_run so the plan is previewable.
            self._stamp_plan(df, idx, "downgrade", f"{reason} → {target_name}", reclaim)
            plan_changed = True

            if self.dry_run:
                self.logger.log_info(
                    f"  📉 [dry_run] Would step down: '{title}' "
                    f"({self._fmt_bytes(_sz_f)}, {cur_qp_name} → {target_name}, ~{reclaim:.1f} GB) — {reason}"
                )
                stats["downgraded"] += 1
                continue

            try:
                payload = self.radarr_api._make_request(instance, f"movie/{movie_id}", fallback=None)
                if not payload or not isinstance(payload, dict):
                    self.logger.log_warning(f"  ⚠️ Could not fetch payload for '{title}' (id={movie_id})")
                    stats["failed"] += 1
                    continue

                payload["qualityProfileId"] = target_id
                self.radarr_api._make_request(instance, f"movie/{movie_id}", method="PUT", payload=payload)

                df.at[idx, "quality_profile_id"]   = target_id
                df.at[idx, "quality_profile_name"] = target_name
                df.at[idx, "quality_action"]       = None
                changed = True
                movie_ids_to_search.append(movie_id)
                stats["downgraded"] += 1

                self.logger.log_info(
                    f"  📉 Stepped down: '{title}' "
                    f"({self._fmt_bytes(_sz_f)}, {cur_qp_name} → {target_name}, ~{reclaim:.1f} GB) — {reason}"
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Downgrade failed for '{title}' (id={movie_id}): {e}")
                stats["failed"] += 1

        if movie_ids_to_search:
            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "MoviesSearch", "movieIds": movie_ids_to_search},
                )
                self.logger.log_info(f"  🔍 MovieSearch triggered for {len(movie_ids_to_search)} movie(s)")
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ MovieSearch trigger failed: {e}")

        # Persist when real downgrades happened OR plan stamps were written
        # (the latter is the dry_run preview path).
        if changed or plan_changed:
            mfm.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["stepped down",        stats['downgraded']],
                ["at/below floor",      stats['already_at_720p']],
                ["protected",           stats['skipped_protected']],
                ["hot-universe",        stats.get('skipped_universe', 0)],
                ["high-score protected", stats['skipped_high_score']],
                ["recently watched",    stats['skipped_recent']],
                ["failed",              stats['failed']],
            ],
            title=f"[SpacePressure] {prefix}step-down pass - '{instance}' (~{stats.get('est_reclaim_gb', 0):.0f} GB reclaimed)",
            caption="Result of the HD-720p step-down pass that shrinks low-score movies to free space.",
            descriptions=[
                "movies stepped down one quality rank",
                "movies already at or below the 720p floor",
                "movies protected from downgrade by keep policy",
                "movies protected — hot franchise/universe credit holds an untagged saga member at tier",
                "movies protected by a high watchability score",
                "movies skipped for a recent watch",
                "movies whose PUT/search call errored",
            ],
        )
        return stats

    # ── Stage 2: delete (last resort) ────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure_deletions")
    def run_deletions(self, instance: str, free_space_gb: float) -> dict:
        """
        Target-driven deletion: when free space is below the floor (free_space_limit),
        delete the lowest-rated owned movies — watchability score, then critic ratings
        (imdb/trakt/tmdb/rt/mc), then largest file — until projected free >= U (top of
        the pressure band, for hysteresis). Tiered: watched + grace-expired first, then
        (optionally) unwatched low-watchability. Every deletion is recorded so
        restore_recovered_deletions re-acquires it if its score later recovers. Guards:
        keep_forever/keep_movie/keep_universe, franchise entries/files, and
        recently-watched (within COLLECTION_WINDOW_DAYS) are never deleted.
        """
        stats = {"checked": 0, "deleted": 0, "failed": 0, "bytes_freed": 0.0,
                 "tier_watched": 0, "tier_unwatched": 0, "skipped_universe": 0,
                 "target_met": False}

        if self._coordinator_owns_deletion():
            # Cross-service coordinator deletes movies + TV together on one ranked
            # pool; this per-service loop defers (downgrades already ran).
            return stats
        if not deletions_enabled(self.config):
            # HARD SAFETY GATE: no operator-set free_space_limit → no deletions,
            # anywhere. main.py emits the loud end-of-run banner.
            self.logger.log_warning(
                f"[SpacePressure] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                "skipping the movie delete pass."
            )
            return stats
        if not bool(self.config.get("space_pressure_delete_enabled", True) if self.config else True):
            return stats

        T, U = self._space_targets(instance)
        if free_space_gb >= T:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free — at/above floor "
                f"{T:.0f} GB, no space deletion."
            )
            return stats

        mfm = self._get_movie_files_manager()
        if mfm is None:
            return stats
        df = mfm.load(instance)
        if df.empty:
            return stats

        include_unwatched = bool(self.config.get("space_pressure_include_unwatched", True) if self.config else True)
        try:
            ceiling = int(self.config.get("space_pressure_score_ceiling", 20) if self.config else 20)
        except (TypeError, ValueError):
            ceiling = 20

        score_map          = self._build_score_map(df, instance)
        franchise_file_ids = mfm._build_franchise_file_ids(df)
        now                = datetime.now(tz=timezone.utc)
        no_delete_cutoff   = now - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        marked = (
            df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
            if "marked_for_deletion" in df.columns else pd.Series(False, index=df.index)
        )

        # ── Ranked, tiered candidate list (lowest-rated first) ────────────────────
        # The DECISION (which files, in what order) is the brain's; the target loop
        # below APPLIES it. Tuple: (tier, score, critic_or_None, -size, idx, fid, size);
        # a missing critic sorts NEUTRAL at 5.0 (not to the protected end). The
        # per-row critic blend is the shared scoring.critic.critic_avg (Step 2).
        candidates = build_movie_delete_candidates(
            df, score_map, marked,
            franchise_file_ids=franchise_file_ids,
            no_delete_cutoff=no_delete_cutoff,
            include_unwatched=include_unwatched,
            ceiling=ceiling,
            universe_age_days=self._universe_delete_age_days(),
            now=now,
            stats=stats,
        )

        _uni = stats.get("skipped_universe", 0)
        self.logger.log_info(
            f"[SpacePressure] '{instance}': {free_space_gb:.1f} GB free — target loop to {U:.0f} GB "
            f"({len(candidates)} candidate(s), lowest-rated first"
            f"{f'; {_uni} held by hot-universe credit' if _uni else ''})."
        )

        freed_gb = 0.0
        changed  = False
        stamped  = False   # any planned_action='delete' written → persist for the ledger
        deleted_tmdbs: list[int] = []
        for tier, score, critic, _neg, idx, fid, size in candidates:
            if free_space_gb + freed_gb >= U:
                stats["target_met"] = True
                break
            stats["checked"] += 1
            title      = df.at[idx, "title"] or f"movie {df.at[idx, 'movie_id']}"
            size_gb    = size / (1024 ** 3)
            tmdb_id    = df.at[idx, "tmdb_id"] if "tmdb_id" in df.columns else None
            critic_str = f"{critic:.1f}/10" if critic is not None else "n/a"
            reason     = (f"{'watched' if tier == 0 else 'unwatched'}, score={score}, critic={critic_str}, "
                          f"{self._fmt_bytes(size)}; space target {U:.0f} GB (free {free_space_gb:.0f} GB)")
            self._stamp_plan(df, idx, "delete", reason, size_gb)
            stamped = True

            if effective_dry_run(self.dry_run, self.global_cache):    # also dry when backup gate disarmed
                self.logger.log_info(f"  🗑️ [dry_run] Would delete: '{title}' ({self._fmt_bytes(size)}) — {reason}")
                freed_gb += size_gb
                stats["bytes_freed"] += size
                stats["deleted"] += 1
                stats["tier_watched" if tier == 0 else "tier_unwatched"] += 1
                continue
            try:
                self.radarr_api._make_request(instance, f"moviefile/{fid}", method="DELETE")
                df.at[idx, "marked_for_deletion"] = False
                freed_gb += size_gb
                stats["bytes_freed"] += size
                stats["deleted"] += 1
                stats["tier_watched" if tier == 0 else "tier_unwatched"] += 1
                changed = True
                if tmdb_id is not None and pd.notna(tmdb_id):
                    deleted_tmdbs.append(int(tmdb_id))
                self.logger.log_info(f"  🗑️ Deleted: '{title}' ({self._fmt_bytes(size)}) — {reason}")
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Delete failed for '{title}' (movieFileId={fid}): {e}")
                stats["failed"] += 1

        if free_space_gb + freed_gb >= U:
            stats["target_met"] = True

        # Record real deletions so restore_recovered_deletions can re-acquire them.
        if deleted_tmdbs and self.global_cache:
            try:
                from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
                dkey = RadarrRepairAnomalyManager._DELETED_SET_KEY.format(inst=instance)
            except Exception:
                dkey = f"radarr/{instance}/demote_deleted"
            try:
                dset = self.global_cache.get(dkey)
                dset = dset if isinstance(dset, dict) else {}
                for t in deleted_tmdbs:
                    dset[str(t)] = now.isoformat()
                self.global_cache.set(dkey, dset)
            except Exception as e:
                # These files are already deleted on disk; if we can't record them
                # in the restore-set, restore_recovered_deletions can never re-grab
                # them. Make the loss LOUD instead of silent so it's recoverable.
                self.logger.log_error(
                    f"[SpacePressure] ⚠️ Failed to persist restore-set for {len(deleted_tmdbs)} "
                    f"deleted movie(s) ({dkey}): {e} — these deletions are NOT restorable."
                )

        # Persist the ledger annotations even in dry_run when we stamped a plan (i.e.
        # under pressure) so the dry-run ledger reflects what WOULD be deleted. In
        # dry_run only the plan columns change — no Radarr writes were issued.
        if (changed and not self.dry_run) or (self.dry_run and stamped):
            mfm.save(instance, df)

        prefix  = "[dry_run] " if self.dry_run else ""
        verb    = "would free" if self.dry_run else "freed"
        _target = 'met' if stats['target_met'] else 'NOT met (candidates exhausted)'
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["checked",          stats['checked']],
                ["deleted",          stats['deleted']],
                ["watched tier",     stats['tier_watched']],
                ["unwatched tier",   stats['tier_unwatched']],
                ["failed",           stats['failed']],
            ],
            title=f"[SpacePressure] {prefix}target-loop deletion - '{instance}' (target {U:.0f} GB {_target})",
            caption=f"Result of the last-resort delete loop that {verb} {self._fmt_bytes(stats['bytes_freed'])} toward the space target.",
            descriptions=[
                "movies examined as delete candidates",
                "movie files deleted from disk",
                "deletions from the watched + grace-expired tier",
                "deletions from the unwatched low-score tier",
                "movies whose DELETE call errored",
            ],
        )
        return stats


    # ── Cross-service coordinator hooks (Phase 4) ────────────────────────────────
    # build_delete_candidates + delete_selected_movie_files are the reusable
    # primitives the SpaceCoordinatorManager calls to merge movies and TV into ONE
    # ranked deletion pool. run_deletions stays the single-service fallback (used
    # only when space_coordinator_enabled is off).

    def _row_critic_avg(self, df, idx) -> "float | None":
        """Delegate the critic-consensus blend to the brain (scoring/critic).
        Service keeps only the column extraction (only present columns are passed)."""
        ratings = {c: df.at[idx, c]
                   for c in ("imdb_rating", "tmdb_rating", "trakt_rating",
                             "rotten_tomatoes_score", "metacritic_score")
                   if c in df.columns}
        return critic_avg(ratings)

    @timeit("build_movie_delete_candidates")
    def build_delete_candidates(self, instance: str, df, *,
                                ignore_score_ceiling: bool = False) -> list[dict]:
        """Return the ranked-but-unsorted list of MOVIE delete-candidates for the
        coordinator's combined pool. Same guards/tiers as run_deletions but it does
        NOT delete. Reads the persisted watchability_score column (refresh_scores)
        falling back to a live score_map. Each dict: service/idx/fid/tmdb_id/score/
        critic/size_bytes/size_gb/tier/title/resolution.

        ``ignore_score_ceiling`` (default False) keeps every keep/franchise/recently-watched
        guard but skips ONLY the watchability score ceiling — used to build the dual-version
        4K-copy reclaim pool, where each baseline-backed 4K copy is pure reclaim (no title lost)
        regardless of watchability, so it must be reclaimable before any whole title."""
        out: list[dict] = []
        if df is None or df.empty:
            return out
        include_unwatched = bool(self.config.get("space_pressure_include_unwatched", True) if self.config else True)
        try:
            ceiling = int(self.config.get("space_pressure_score_ceiling", 20) if self.config else 20)
        except (TypeError, ValueError):
            ceiling = 20

        have_col = "watchability_score" in df.columns
        # If the persisted column exists but is entirely empty, refresh_scores didn't
        # populate it — the fallback would rank every movie as deletable. Defer rather
        # than delete on fallback scores. (Absent column → live _build_score_map below,
        # which computes real scores, so only guard the present-but-empty case.)
        if have_col and len(df) > 0 and \
                pd.to_numeric(df["watchability_score"], errors="coerce").notna().sum() == 0:
            self.logger.log_warning(
                f"[SpacePressure] '{instance}' watchability_score is empty — refresh_scores "
                f"likely didn't run; yielding NO movie delete candidates (won't delete on fallback scores)."
            )
            return out
        score_map = None if have_col else self._build_score_map(df, instance)
        mfm = self._get_movie_files_manager()
        franchise_file_ids = mfm._build_franchise_file_ids(df) if mfm else frozenset()
        now = datetime.now(tz=timezone.utc)
        no_delete_cutoff = now - timedelta(days=self.COLLECTION_WINDOW_DAYS)
        marked = (
            df["marked_for_deletion"].infer_objects(copy=False).fillna(False).astype(bool)
            if "marked_for_deletion" in df.columns else pd.Series(False, index=df.index)
        )
        _univ_age = self._universe_delete_age_days()   # bare-universe ageing (default None = off)
        _held_universe = 0   # hot-saga rows the credit guard spared from the pool (observability)

        for idx in df.index:
            fid = df.at[idx, "movie_file_id"]
            if pd.isna(fid):
                continue
            keep_policy = df.at[idx, "keep_policy"] if "keep_policy" in df.columns else None
            is_fe = bool(df.at[idx, "is_franchise_entry"]) if "is_franchise_entry" in df.columns else False
            if is_fe or keep_policy in ("keep_forever", "keep_movie", "keep_universe"):
                continue
            if fid in franchise_file_ids:
                continue
            # Bare-universe ageing (default-off): mirror of the brain delete-planner guard
            # so the coordinated pool spares a still-ageing 'universe' title too.
            if bare_universe_protected(
                keep_policy, df.at[idx, "date_added"] if "date_added" in df.columns else None,
                now, age_days=_univ_age,
            ):
                continue
            # Borrowed franchise/universe credit (per-movie, recency-decayed by refresh_scores):
            # an UNTAGGED hot-saga member resists DELETION just as plan_movie_downgrades makes it
            # resist a step-down — mirror of the brain delete-planner guard (build_movie_delete_candidates)
            # so the coordinated pool spares it too (run_deletions, the single-service fallback, already
            # does). Skipped for the 4K-copy reclaim pool (ignore_score_ceiling): there the 1080p baseline
            # survives, so the bonus copy is pure reclaim and loses no title — the same reason the score
            # ceiling is relaxed there. Byte-identical when the column is absent / credit unset.
            if not ignore_score_ceiling and "universe_credit" in df.columns:
                _uc = df.at[idx, "universe_credit"]
                try:
                    if _uc is not None and pd.notna(_uc) and float(_uc) >= UNIVERSE_PROTECT_MIN:
                        _held_universe += 1
                        continue
                except (TypeError, ValueError):
                    pass
            lw = df.at[idx, "last_watched_at"] if "last_watched_at" in df.columns else None
            if lw:
                try:
                    if pd.to_datetime(lw, utc=True) >= no_delete_cutoff:
                        continue
                except Exception:
                    pass

            if have_col:
                _sc = df.at[idx, "watchability_score"]
                score = int(_sc) if pd.notna(_sc) else 5
            else:
                score = int(score_map.get(idx, 5))
            size = float(df.at[idx, "size_bytes"]) if ("size_bytes" in df.columns and pd.notna(df.at[idx, "size_bytes"])) else 0.0

            if bool(marked.loc[idx]):
                tier = 0
            else:
                if not include_unwatched or (score >= ceiling and not ignore_score_ceiling):
                    continue
                da = df.at[idx, "date_added"] if "date_added" in df.columns else None
                if da:
                    try:
                        if pd.to_datetime(da, utc=True) >= no_delete_cutoff:
                            continue
                    except Exception:
                        pass
                tier = 1

            tmdb_id = df.at[idx, "tmdb_id"] if "tmdb_id" in df.columns else None
            _res = df.at[idx, "resolution"] if "resolution" in df.columns else None
            out.append({
                "service": "movie", "tier": tier, "score": score,
                "critic": self._row_critic_avg(df, idx), "size_bytes": size,
                "size_gb": size / (1024 ** 3), "idx": idx, "fid": int(fid),
                "tmdb_id": int(tmdb_id) if (tmdb_id is not None and pd.notna(tmdb_id)) else None,
                "resolution": int(_res) if (_res is not None and pd.notna(_res)) else None,
                "title": (df.at[idx, "title"] if "title" in df.columns else None) or f"movie {fid}",
            })
        if _held_universe:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {_held_universe} title(s) held by hot-universe credit "
                f"(coordinator delete pool)."
            )
        return out

    @timeit("delete_selected_movie_files")
    def delete_selected_movie_files(self, instance: str, df, picks: list[dict]) -> dict:
        """Delete the chosen movie files (moviefile/{id} DELETE), record them in the
        restore-set, and persist df. ``picks`` are dicts from build_delete_candidates
        that the coordinator selected. dry_run stamps the plan but issues no DELETE."""
        stats = {"deleted": 0, "failed": 0, "bytes_freed": 0.0}
        if not picks:
            return stats
        if not deletions_enabled(self.config):
            # Belt-and-braces: the coordinator can't run without a floor, but never
            # delete through this APPLY primitive either when the gate is closed.
            self.logger.log_warning(
                f"[SpacePressure] deletions DISABLED — {deletions_disabled_reason(self.config)}; "
                f"refusing coordinator delete of {len(picks)} movie pick(s)."
            )
            return stats
        now = datetime.now(tz=timezone.utc)
        changed = False
        deleted_tmdbs: list[int] = []
        for c in picks:
            idx, fid, size = c["idx"], c["fid"], float(c.get("size_bytes") or 0.0)
            title = c.get("title") or f"movie {fid}"
            self._stamp_plan(df, idx, "delete", c.get("reason") or "coordinator pool", size / (1024 ** 3))
            if effective_dry_run(self.dry_run, self.global_cache):    # also dry when backup gate disarmed
                self.logger.log_info(f"  🗑️ [dry_run] Would delete movie: '{title}' ({self._fmt_bytes(size)})")
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                continue
            try:
                self.radarr_api._make_request(instance, f"moviefile/{fid}", method="DELETE")
                if "marked_for_deletion" in df.columns:
                    df.at[idx, "marked_for_deletion"] = False
                stats["deleted"] += 1
                stats["bytes_freed"] += size
                changed = True
                if c.get("tmdb_id") is not None:
                    deleted_tmdbs.append(int(c["tmdb_id"]))
                self.logger.log_info(f"  🗑️ Deleted movie: '{title}' ({self._fmt_bytes(size)})")
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Movie delete failed for '{title}' (movieFileId={fid}): {e}")
                stats["failed"] += 1

        if deleted_tmdbs and self.global_cache:
            try:
                from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
                dkey = RadarrRepairAnomalyManager._DELETED_SET_KEY.format(inst=instance)
            except Exception:
                dkey = f"radarr/{instance}/demote_deleted"
            try:
                dset = self.global_cache.get(dkey)
                dset = dset if isinstance(dset, dict) else {}
                for t in deleted_tmdbs:
                    dset[str(t)] = now.isoformat()
                self.global_cache.set(dkey, dset)
            except Exception as e:
                self.logger.log_error(
                    f"[SpacePressure] ⚠️ Failed to persist restore-set for {len(deleted_tmdbs)} "
                    f"deleted movie(s) ({dkey}): {e} — these deletions are NOT restorable."
                )

        # Persist plan stamps even in dry_run (coordinator preview) so the ledger
        # reflects what the unified pool would delete. dry_run touches only the
        # plan columns — no Radarr writes were issued.
        if (changed and not self.dry_run) or (self.dry_run and picks):
            mfm = self._get_movie_files_manager()
            if mfm:
                mfm.save(instance, df)
        return stats

    def load_movie_files(self, instance: str):
        """Coordinator helper: load the movie_files parquet (or None)."""
        mfm = self._get_movie_files_manager()
        return mfm.load(instance) if mfm else None

    # ── Stage 0: upgrade actively-watched movies ─────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_active_watcher_upgrades")
    def run_active_watcher_upgrades(self, instance: str, free_space_gb: float) -> dict:
        """
        When free space is comfortably above the upgrade threshold, upgrade
        non-kids movies that the household is actively watching to the best
        available quality profile.

        "Actively watching" = watched within ACTIVE_WATCH_DAYS (default 30d)
        and NOT in the kids library (certification G/PG excluded unless
        the household adults also watched it).

        NEVER touches keep_universe, keep_forever, keep_movie — the universe
        manager owns keep_universe upgrades.

        Only upgrades when free_space_gb >= U (top of the pressure band derived from
        free_space_limit) so we never upgrade into a space crunch.
        """
        ACTIVE_WATCH_DAYS   = 30
        KIDS_CERTS          = {"g", "pg", "tv-g", "tv-y", "tv-y7"}
        _, upgrade_min_free_gb = self._space_targets(instance)   # U = free_space_limit + headroom

        stats = {
            "checked": 0, "upgraded": 0, "already_best": 0,
            "skipped_kids": 0, "skipped_not_active": 0, "failed": 0,
        }

        if free_space_gb < upgrade_min_free_gb:
            self.logger.log_debug(
                f"[SpacePressure] Active-watcher upgrades skipped: "
                f"{free_space_gb:.1f} GB < {upgrade_min_free_gb:.0f} GB threshold."
            )
            return stats

        mfm = self._get_movie_files_manager()
        if mfm is None:
            return stats

        df = mfm.load(instance)
        if df.empty:
            return stats

        # Fetch all quality profiles sorted best-last
        try:
            raw_profiles = (
                self.global_cache.get(f"radarr.quality.{instance}") or []
                if self.global_cache else []
            )
            if not raw_profiles:
                raw_profiles = self.radarr_api._make_request(
                    instance, "qualityprofile", fallback=[]
                ) or []
        except Exception:
            return stats

        if not raw_profiles:
            return stats

        # Rank profiles by max resolution — highest = best
        def _max_res(p: dict) -> int:
            best = 0
            for item in (p.get("items") or []):
                if not item.get("allowed"):
                    continue
                res = (item.get("quality") or {}).get("resolution", 0)
                if isinstance(res, (int, float)):
                    best = max(best, int(res))
                for sub in (item.get("items") or []):
                    if not sub.get("allowed"):
                        continue
                    sr = (sub.get("quality") or {}).get("resolution", 0)
                    if isinstance(sr, (int, float)):
                        best = max(best, int(sr))
            return best

        ranked = sorted(raw_profiles, key=_max_res)
        if not ranked:
            return stats

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ACTIVE_WATCH_DAYS)
        movie_ids_to_search: list[int] = []
        changed = False
        plan_changed = False

        # DECISION (which titles, to which profile, expected reclaim) is the
        # brain's; the loop below APPLIES it (stamp + dry-run preview or PUT).
        candidates, _pstats = plan_movie_upgrades(
            df, ranked, active_cutoff=cutoff, config=self.config
        )
        stats.update(_pstats)   # checked / already_best / skipped_kids / skipped_not_active

        for _cand in candidates:
            idx            = _cand["idx"]
            movie_id       = _cand["movie_id"]
            target_profile = _cand["target_profile"]
            target_id      = _cand["target_id"]
            target_name    = _cand["target_name"]
            likelihood     = _cand["likelihood"]
            title          = df.at[idx, "title"] or f"movie {movie_id}"
            cur_qp_name    = df.at[idx, "quality_profile_name"] if "quality_profile_name" in df.columns else "?"

            self._stamp_plan(df, idx, "upgrade", _cand["reason"], _cand["reclaim_gb"])
            plan_changed = True

            if self.dry_run:
                self.logger.log_info(
                    f"  📈 [dry_run] Would upgrade: '{title}' "
                    f"({cur_qp_name} → {target_name}) [L={likelihood:.0f}% → profile {target_id}]"
                )
                stats["upgraded"] += 1
                continue

            try:
                payload = self.radarr_api._make_request(
                    instance, f"movie/{int(movie_id)}", fallback=None
                )
                if payload and isinstance(payload, dict):
                    payload["qualityProfileId"] = target_id
                    self.radarr_api._make_request(
                        instance, f"movie/{int(movie_id)}", method="PUT", payload=payload
                    )
                    df.at[idx, "quality_profile_id"]   = target_id
                    df.at[idx, "quality_profile_name"] = target_name
                    movie_ids_to_search.append(int(movie_id))
                    stats["upgraded"] += 1
                    changed = True
                    self.logger.log_info(
                        f"  📈 Upgraded: '{title}' "
                        f"({cur_qp_name} → {target_name})"
                    )
            except Exception as e:
                self.logger.log_warning(
                    f"  ⚠️ Upgrade failed for '{title}': {e}"
                )
                stats["failed"] += 1

        if movie_ids_to_search and not self.dry_run:
            try:
                self.radarr_api._make_request(
                    instance, "command", method="POST",
                    payload={"name": "MoviesSearch", "movieIds": movie_ids_to_search},
                )
                self.logger.log_info(
                    f"  🔍 Search triggered for {len(movie_ids_to_search)} upgraded movie(s)"
                )
            except Exception as e:
                self.logger.log_warning(f"  ⚠️ Search trigger failed: {e}")

        if changed or plan_changed:
            mfm.save(instance, df)

        prefix = "[dry_run] " if self.dry_run else ""
        self.logger.log_table(
            ["Outcome", "Count"],
            [
                ["upgraded",      stats['upgraded']],
                ["already best",  stats['already_best']],
                ["skipped kids",  stats['skipped_kids']],
                ["not active",    stats['skipped_not_active']],
                ["failed",        stats['failed']],
            ],
            title=f"[SpacePressure] {prefix}active-watcher upgrades - '{instance}'",
            caption="Result of the pass that upgrades actively-watched movies to the best profile when space is plentiful.",
            descriptions=[
                "movies upgraded to a higher quality profile",
                "movies already at the best available profile",
                "kids-library movies skipped from upgrade",
                "movies not watched recently enough to upgrade",
                "movies whose PUT/search call errored",
            ],
        )
        return stats

    # ── Combined run ─────────────────────────────────────────────────────────────

    @timeit("refresh_scores")
    def refresh_scores(self, instance: str) -> int:
        """
        Compute watchability scores for every movie_files Parquet row and
        write them back to ``watchability_score``.

        Must be called before the universe quality manager runs so it has
        valid scores to gate 4K eligibility.
        """
        mfm = self._get_movie_files_manager()
        if mfm is None:
            return 0
        import json

        instance  = self._resolve_instance(instance)
        df        = mfm.load(instance)
        if df.empty:
            return 0
        # with_breakdown=True → {idx: (score, breakdown)}. The score is byte-identical
        # to the no-breakdown path; only the explanation dict is also returned, and it
        # is persisted alongside as a small flat JSON so the advise view can read back
        # WHICH signal groups raised/lowered each title's score.
        score_map = self._build_score_map(df, instance, with_breakdown=True)
        if not score_map:
            return 0
        if "watchability_score" not in df.columns:
            df["watchability_score"] = None
        if "watchability_breakdown" not in df.columns:
            df["watchability_breakdown"] = None
        elif df["watchability_breakdown"].dtype != object:
            # An all-null reloaded column comes back float64; force object so the
            # JSON-string assignments below don't trip the dtype FutureWarning.
            df["watchability_breakdown"] = df["watchability_breakdown"].astype(object)
        score_only: dict[int, int] = {}
        for idx, (score, breakdown) in score_map.items():
            score_only[idx] = score
            df.at[idx, "watchability_score"] = score
            df.at[idx, "watchability_breakdown"] = (
                json.dumps(breakdown, separators=(",", ":")) if breakdown else None
            )
        score_map = score_only   # downstream logging/percentile expects {idx: int}
        # Percentile rank of each score within the library — the rank-based input to
        # the watch-likelihood ladder (Option 1: spreads affinity across the tiers
        # instead of bunching at the low end of the 0-100 score).
        _sc = pd.to_numeric(df["watchability_score"], errors="coerce")
        df["watchability_percentile"] = (_sc.rank(pct=True, method="average") * 100).round(1)
        # Franchise/universe credit: a hot saga lends borrowed effective-watch-count to its members,
        # so the likelihood-gated upgrade AND the space-pressure downgrade pass elevate a single-watch
        # member (and let it fall again as the saga's last watch recedes). Non-destructive; 0 when cold.
        try:
            self._apply_universe_credit(instance, df)
        except Exception as e:
            self.logger.log_debug(f"[Universe] credit pass skipped for '{instance}': {e}")
        # The score is a non-destructive annotation — persist it even in dry_run
        # so the Parquet is sortable by watchability ("least valuable first").
        mfm.save(instance, df)
        self.logger.log_info(
            f"[SpacePressure] Scored {len(score_map)} movies for '{instance}' "
            f"(range: {min(score_map.values())}–{max(score_map.values())})"
        )
        try:
            _rows = self.report_size_anomalies(instance, df)
            self.remediate_size_anomalies(instance, _rows)
        except Exception as e:
            self.logger.log_debug(f"[SizeAnomaly] report/remediate failed for '{instance}': {e}")
        try:
            self.report_codec_routing(instance, df)   # read-only preview; changes nothing
        except Exception as e:
            self.logger.log_debug(f"[CodecRoute] preview failed for '{instance}': {e}")
        return len(score_map)

    def _apply_universe_credit(self, instance: str, df) -> None:
        """Broadcast a per-movie ``universe_credit`` column: borrowed effective-watch-count from a HOT
        saga (rewatched siblings), recency-decayed. Membership is the movie's TMDB COLLECTION
        (``collection_name`` — populated automatically from TMDB metadata, NO keep tag required, so it
        works for every user) unioned with any curated ``universe_name`` labels (pipe-sep) when present;
        a film keeps the credit of its HOTTEST group. Read by ``watch_likelihood`` so BOTH the
        space-pressure passes (untagged saga movies, via plan_movie_downgrades / plan_movie_upgrades)
        AND the universe manager (keep-tagged) elevate a single-watch member of a hot saga, and let it
        fall again as the saga's last watch recedes. 0 everywhere with no franchise heat → byte-identical."""
        has_coll = "collection_name" in df.columns
        has_uni  = "universe_name" in df.columns
        if "movie_id" not in df.columns or not (has_coll or has_uni):
            df["universe_credit"] = 0.0
            return
        _mid = pd.to_numeric(df["movie_id"], errors="coerce")
        _wc  = pd.to_numeric(df["watch_count"], errors="coerce") if "watch_count" in df.columns else None
        _lw  = pd.to_datetime(df["last_watched_at"], utc=True, errors="coerce") \
            if "last_watched_at" in df.columns else None
        now = pd.Timestamp.now(tz="UTC")
        universe_map: dict = {}
        stats: dict = {}
        for pos, idx in enumerate(df.index):
            m = _mid.iat[pos]
            if pd.isna(m):
                continue
            mid = int(m)
            labels: list[str] = []
            if has_coll:
                c = df.at[idx, "collection_name"]
                if c is not None and pd.notna(c) and str(c).strip():
                    labels.append(str(c).strip())
            if has_uni:
                u = df.at[idx, "universe_name"]
                if u is not None and pd.notna(u) and str(u).strip():
                    labels.append(str(u).strip())
            if labels:
                universe_map[mid] = "|".join(labels)
            wc = float(_wc.iat[pos]) if (_wc is not None and pd.notna(_wc.iat[pos])) else 0.0
            ds = (now - _lw.iat[pos]).days if (_lw is not None and pd.notna(_lw.iat[pos])) else 1e9
            stats[mid] = {"watch_count": wc, "days_since": ds}
        # Drop the junk placeholder group names ("universe"/"franchise"/"standalone"/…) — keep_policy
        # stamps bare-universe films with universe_name="universe", which must NOT fuse unrelated films.
        credits = movie_universe_credits(universe_map, stats, config=self.config,
                                         drop_labels=PLACEHOLDER_AFFINITY)
        df["universe_credit"] = _mid.map(lambda m: credits.get(int(m), 0.0) if pd.notna(m) else 0.0)
        if credits:
            self.logger.log_info(
                f"[Universe] '{instance}': lent franchise/collection credit to {len(credits)} movies "
                f"(max {max(credits.values()):.2f} watch-counts)."
            )

    @timeit("report_size_anomalies")
    def report_size_anomalies(self, instance: str, df=None) -> list:
        """Flag movies whose file is WILDLY out of size profile for their graded quality (e.g.
        a 45 GiB file graded 720p ≈ 6x its expected size). Read-only diagnostic: logs a count
        and records a detail table in the end-of-run summary. Returns the anomaly rows (sorted
        biggest-reclaim first) so a space pass can act on them. Off via size_anomaly.enabled=false."""
        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("enabled", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            mfm = self._get_movie_files_manager()
            df = mfm.load(instance) if mfm is not None else None
        if df is None or getattr(df, "empty", True):
            return []

        rows = size_anomaly.find_size_anomalies(
            df, id_cols=("title", "year", "movie_id", "movie_file_id"),
            size_col="size_bytes", runtime_col="runtime_minutes", runtime_unit="minutes",
            quality_col="quality_name", resolution_col="resolution",
            over_ratio=cfg["over_ratio"], under_ratio=cfg["under_ratio"],
            min_samples=cfg["min_samples"],
        )
        if not rows:
            return []
        over = [r for r in rows if r["verdict"] == "oversized"]
        reclaim = sum(r["reclaim_gb"] for r in over)
        self.logger.log_info(
            f"[SizeAnomaly] '{instance}': {len(rows)} file(s) wildly out of size profile — "
            f"{len(over)} oversized (~{reclaim:.0f} GB reclaimable at the in-profile size), "
            f"{len(rows) - len(over)} undersized."
        )
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            table = [[str(r.get("title"))[:30], r["quality_name"], r["looks_like"],
                      f"{r['size_gb']:.1f} GB", f"{r['expected_gb']:.1f} GB", f"x{r['ratio']:.1f}",
                      f"{r['reclaim_gb']:.1f} GB", r["verdict"]] for r in rows[:cfg["report_limit"]]]
            _rs.add_rows(
                "radarr", "Size anomalies", instance,
                ["Title", "Graded", "Looks like", "Size", "Expected", "Ratio", "Reclaim", "Verdict"],
                table, order=35,
            )
        return rows

    def report_codec_routing(self, instance: str, df=None) -> list:
        """READ-ONLY codec-routing preview. For each owned, WATCHED movie, show the codec the
        transcode-minimising policy WOULD pick for its actual viewers (profile_selector.
        choose_codec_profile over the per-user device→transcode matrix) vs the file's CURRENT
        codec. Changes NOTHING — logs a count and records a 'Codec routing preview' table in the
        end-of-run summary so the codec-aware-routing decisions are visible before any actuation
        is wired. Cheap no-op when there are no codec-variant profiles or no watch history.
        Off via ``scoring.codec_profiles.report=false``. (v1 covers WATCHED titles; extending the
        compute to all owned titles via affinity-predicted viewers is the Phase-2 follow-up.)"""
        if not (((self.config or {}).get("scoring") or {}).get("codec_profiles") or {}).get("report", True):
            return []
        instance = self._resolve_instance(instance)
        if df is None:
            mfm = self._get_movie_files_manager()
            df = mfm.load(instance) if mfm is not None else None
        if df is None or getattr(df, "empty", True):
            return []
        history = (self.global_cache.get("tautulli/history/all") if self.global_cache else None) or []
        if not history:
            return []
        try:
            profiles = self.radarr_api._make_request(instance, "qualityprofile", fallback=[]) or []
        except Exception:
            profiles = []
        if not profiles:
            return []
        from scripts.managers.machine_learning.quality_analytics.codec_report import (
            build_per_title_watchers, codec_report_rows, per_user_platform_usage_from_history,
        )
        from scripts.managers.machine_learning.quality_analytics.transcode_fingerprint import (
            per_user_transcode_fingerprint_matrix,
        )
        rows = codec_report_rows(
            df, profiles,
            per_user_transcode_fingerprint_matrix(history),
            per_user_platform_usage_from_history(history),
            build_per_title_watchers(history),
        )
        if not rows:
            return []
        changed = [r for r in rows if r["change"]]
        self.logger.log_info(
            f"[CodecRoute] '{instance}': {len(changed)} of {len(rows)} watched title(s) would "
            f"change codec to reduce transcoding for their viewers (read-only preview; nothing applied)."
        )
        _rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
        if _rs is not None:
            table = [[str(r["title"])[:30], ",".join(r["watchers"])[:18], r["current_codec"],
                      r["recommended_codec"], f"{(r['cost'] or 0.0):.2f}", "YES" if r["change"] else "-"]
                     for r in rows[:40]]
            _rs.add_rows(
                "radarr", "Codec routing preview", instance,
                ["Title", "Viewers", "Current", "Recommend", "TranscodeCost", "Change"],
                table, order=37,
            )
        return rows

    @timeit("remediate_size_anomalies")
    def remediate_size_anomalies(self, instance: str, rows: "list | None") -> dict:
        """ACT on the size anomalies (opt-in: ``size_anomaly.remediate=true``).

          * MIS-GRADED (junk/SD grade, really HD) → ``RefreshMovie`` rescans mediainfo to fix the
            grade. Non-destructive.
          * BLOATED (oversized at a real HD/UHD grade) → re-grab at the profile target: DELETE the
            oversized file + ``MoviesSearch`` so Radarr re-acquires a properly-sized release.
            DESTRUCTIVE — only for MONITORED movies (else the delete would orphan the slot), and
            only when the backup gate is armed (``effective_dry_run`` False); otherwise logged as
            'would …'. Honours the run's dry_run AND the degrade-to-dry-run backup gate."""
        cfg = size_anomaly.config_for(self.config)
        if not cfg.get("remediate", False) or not rows:
            return {}
        instance = self._resolve_instance(instance)
        eff_dry = effective_dry_run(self.dry_run, self.global_cache)
        stats = {"rescanned": 0, "regrabbed": 0, "skipped_unmonitored": 0, "failed": 0}

        # ── rescan mis-graded (non-destructive) ──────────────────────────────────
        mids = [int(r["movie_id"]) for r in rows
                if r.get("action") == "rescan" and r.get("movie_id") is not None]
        if mids:
            if eff_dry:
                self.logger.log_info(f"[SizeAnomaly] [dry_run] would RefreshMovie (rescan) "
                                     f"{len(mids)} mis-graded movie(s) on '{instance}'.")
            else:
                try:
                    self.radarr_api._make_request(instance, "command", method="POST",
                                                  payload={"name": "RefreshMovie", "movieIds": mids})
                    stats["rescanned"] = len(mids)
                    self.logger.log_info(f"[SizeAnomaly] rescanned {len(mids)} mis-graded "
                                         f"movie(s) on '{instance}' to fix the grade.")
                except Exception as e:
                    stats["failed"] += 1
                    self.logger.log_warning(f"[SizeAnomaly] rescan batch failed on '{instance}': {e}")

        # ── re-grab bloated (destructive: delete + search) ───────────────────────
        for r in rows:
            if r.get("action") != "regrab":
                continue
            mid, fid = r.get("movie_id"), r.get("movie_file_id")
            if mid is None or fid is None:
                continue
            # Only re-grab MONITORED movies — deleting an unmonitored movie's file leaves it gone
            # with nothing to re-acquire it.
            mv = self.radarr_api._make_request(instance, f"movie/{int(mid)}", fallback=None)
            if not (isinstance(mv, dict) and mv.get("monitored")):
                stats["skipped_unmonitored"] += 1
                self.logger.log_info(f"[SizeAnomaly] skip re-grab '{r.get('title')}' — not monitored.")
                continue
            if eff_dry:
                self.logger.log_info(
                    f"[SizeAnomaly] [dry_run] would re-grab '{r.get('title')}' "
                    f"({r.get('size_gb')} GB {r.get('quality_name')} -> profile target, "
                    f"~{r.get('reclaim_gb')} GB reclaim): delete file {fid} + MoviesSearch.")
                continue
            try:
                self.radarr_api._make_request(instance, f"moviefile/{int(fid)}", method="DELETE")
                self.radarr_api._make_request(instance, "command", method="POST",
                                              payload={"name": "MoviesSearch", "movieIds": [int(mid)]})
                stats["regrabbed"] += 1
                self.logger.log_info(f"[SizeAnomaly] re-grabbing '{r.get('title')}': deleted "
                                     f"bloated file, searching at profile target.")
            except Exception as e:
                stats["failed"] += 1
                self.logger.log_warning(f"[SizeAnomaly] re-grab failed for '{r.get('title')}': {e}")

        acted = stats["rescanned"] + stats["regrabbed"]
        if acted or stats["skipped_unmonitored"]:
            self.logger.log_info(
                f"[SizeAnomaly] '{instance}' remediation: {stats['rescanned']} rescanned, "
                f"{stats['regrabbed']} re-grabbed, {stats['skipped_unmonitored']} skipped "
                f"(unmonitored), {stats['failed']} failed."
            )
        return stats

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure")
    def run(self, instance: str) -> dict:
        """
        Full pipeline:
          1. Check free space — bail if above threshold.
          2. Stage 1: downgrade low-score movies to HD-720p + trigger search.
          3. Re-read free space.
          4. Stage 2: delete watched + grace-expired + already-720p if still tight.
        """
        instance = self._resolve_instance(instance)
        free_gb  = self._get_free_space_gb(instance)
        T, U     = self._space_targets(instance)

        if free_gb >= U:
            self.logger.log_info(
                f"[SpacePressure] '{instance}': {free_gb:.1f} GB free — "
                f"above {U:.0f} GB (free_space_limit {T:.0f} GB +headroom), no action needed."
            )
            return {"free_space_gb": free_gb, "action": "none"}

        self.logger.log_info(
            f"[SpacePressure] ⚠️ '{instance}': {free_gb:.1f} GB free — "
            f"below {U:.0f} GB pressure band (floor {T:.0f} GB). Starting pipeline."
        )

        # Stage 0: upgrade actively-watched movies when space is plentiful
        upgrade_stats   = self.run_active_watcher_upgrades(instance, free_gb)
        downgrade_stats = self.run_downgrades(instance, free_gb)
        free_gb_after   = self._get_free_space_gb(instance)
        deletion_stats  = self.run_deletions(instance, free_gb_after)

        return {
            "free_space_before_gb": round(free_gb, 2),
            "free_space_after_gb":  round(free_gb_after, 2),
            "active_upgrades":      upgrade_stats,
            "downgrades":           downgrade_stats,
            "deletions":            deletion_stats,
        }

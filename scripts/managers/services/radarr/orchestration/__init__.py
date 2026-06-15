from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrOrchestrationManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, radarr_api=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api = (
            radarr_api
            or getattr(parent, "radarr_api", None)
            or self.registry.get("manager", self.parent_name)
        )
        # movies lives on RadarrManager (parent), not on radarr_api (RadarrInstanceManager)
        self.movies  = getattr(parent, "movies", None) or self.registry.get("manager", "RadarrMoviesManager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__}")

    # ── Instance helpers ─────────────────────────────────────────────────────────

    def _all_instances(self) -> list[str]:
        """Return all configured Radarr instance names."""
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return list(self.radarr_api.get_all_radarr_apis().keys())
            except Exception:
                pass
        return []

    def _resolve_instance(self, instance):
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    def _get_cache_manager(self):
        try:
            return self.registry.get("manager", "RadarrCacheManager")
        except Exception:
            return None

    def _get_movies_manager(self):
        return self.movies or self.registry.get("manager", "RadarrMoviesManager")

    def _get_quality_manager(self):
        """Retrieve RadarrQualityManager from registry."""
        try:
            return self.registry.get("manager", "RadarrQualityManager")
        except Exception:
            return None

    def _get_storage_manager(self):
        """Retrieve RadarrStorageManager from registry."""
        try:
            return self.registry.get("manager", "RadarrStorageManager")
        except Exception:
            return None

    def _get_monitoring_manager(self):
        """Retrieve top-level RadarrMonitoringManager from registry."""
        try:
            return self.registry.get("manager", "RadarrMonitoringManager")
        except Exception:
            return None

    def _get_trakt_movies_manager(self):
        """
        Retrieve TraktMoviesManager from registry if available.
        Returns None when Trakt is not configured or not yet initialised —
        callers must treat this as an optional enrichment source.
        """
        try:
            return self.registry.get("manager", "TraktMoviesManager")
        except Exception:
            return None

    # ── Data pull methods ────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_movie_data_pull")
    def run_movie_data_pull(self, instance):
        movies_mgr = self._get_movies_manager()
        for instance_name in self._all_instances():
            if movies_mgr and hasattr(getattr(movies_mgr, "retrieval", None), "get_all_movies"):
                movie_list = movies_mgr.retrieval.get_all_movies(instance_name)
            else:
                movie_list = self.radarr_api._make_request(instance_name, "movie", fallback=[]) if self.radarr_api else []
            self.global_cache.set(f"radarr.movies.{instance_name}.full", movie_list)
            self.logger.log_info(f"Pulled {len(movie_list) if isinstance(movie_list, list) else 0} movies from {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_monitoring_data_pull")
    def run_monitoring_data_pull(self, instance):
        """Pull monitored/unmonitored summary via the top-level RadarrMonitoringManager."""
        monitoring_mgr = self._get_monitoring_manager()
        movies_monitoring = (
            getattr(monitoring_mgr, "movies", None) if monitoring_mgr else None
        )
        for instance_name in self._all_instances():
            if movies_monitoring and hasattr(movies_monitoring, "get_monitoring_summary"):
                monitored, unmonitored = movies_monitoring.get_monitoring_summary(instance_name)
            else:
                # Prefer the already-cached movie list from run_movie_data_pull
                all_movies = (
                    (self.global_cache.get(f"radarr.movies.{instance_name}.full") or [])
                    if self.global_cache else []
                )
                if not all_movies:
                    _mov = self._get_movies_manager()
                    all_movies = (
                        _mov.retrieval.get_all_movies(instance_name)
                        if _mov and hasattr(getattr(_mov, "retrieval", None), "get_all_movies")
                        else (self.radarr_api._make_request(instance_name, "movie", fallback=[]) if self.radarr_api else [])
                    )
                monitored   = [m for m in all_movies if m.get("monitored")]
                unmonitored = [m for m in all_movies if not m.get("monitored")]

            data = {
                "monitored":   monitored,
                "unmonitored": unmonitored,
                "meta": {
                    "timestamp":        datetime.now(timezone.utc).isoformat(),
                    "monitoredCount":   len(monitored),
                    "unmonitoredCount": len(unmonitored),
                },
            }
            self.global_cache.set(f"radarr.monitoring.{instance_name}", data)
            self.logger.log_info(f"Monitoring summary cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_quality_data_pull")
    def run_quality_data_pull(self, instance):
        """Pull quality profiles via RadarrQualityManager.selector."""
        quality_mgr = self._get_quality_manager()
        selector    = getattr(quality_mgr, "selector", None) if quality_mgr else None
        for instance_name in self._all_instances():
            if selector and hasattr(selector, "get_quality_profiles"):
                profiles = selector.get_quality_profiles(instance_name)
            else:
                # Fallback: direct API call
                profiles = (
                    self.radarr_api._make_request(instance_name, "qualityprofile", fallback=[])
                    if self.radarr_api else []
                )
            self.global_cache.set(f"radarr.quality.{instance_name}", profiles)
            self.logger.log_info(f"Quality profiles cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_tag_data_pull")
    def run_tag_data_pull(self, instance):
        """Pull tags via the Radarr API directly."""
        for instance_name in self._all_instances():
            tags = (
                self.radarr_api._make_request(instance_name, "tag", fallback=[])
                if self.radarr_api else []
            )
            self.global_cache.set(f"radarr.tags.{instance_name}", tags)
            self.logger.log_info(f"Tags cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_custom_format_data_pull")
    def run_custom_format_data_pull(self, instance):
        """Pull custom formats via RadarrQualityManager.custom_formats."""
        quality_mgr  = self._get_quality_manager()
        cf_mgr       = getattr(quality_mgr, "custom_formats", None) if quality_mgr else None
        for instance_name in self._all_instances():
            if cf_mgr and hasattr(cf_mgr, "get_custom_formats"):
                formats = cf_mgr.get_custom_formats(instance_name)
            else:
                formats = (
                    self.radarr_api._make_request(instance_name, "customformat", fallback=[])
                    if self.radarr_api else []
                )
            self.global_cache.set(f"radarr.custom_formats.{instance_name}", formats)
            self.logger.log_info(f"Custom formats cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_storage_data_pull")
    def run_storage_data_pull(self, instance):
        """Pull disk/root-folder data via RadarrStorageManager.space."""
        storage_mgr = self._get_storage_manager()
        space_mgr   = getattr(storage_mgr, "space", None) if storage_mgr else None
        for instance_name in self._all_instances():
            if space_mgr and hasattr(space_mgr, "get_root_folders"):
                disk_data = space_mgr.get_root_folders(instance_name)
            else:
                disk_data = (
                    self.radarr_api._make_request(instance_name, "rootfolder", fallback=[])
                    if self.radarr_api else []
                )
            self.global_cache.set(f"radarr.disk.{instance_name}", disk_data)
            self.logger.log_info(f"Disk usage cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_adjustment_data_pull")
    def run_adjustment_data_pull(self, instance):
        """Pull quality-definition adjustments via RadarrQualityManager.adjustments."""
        quality_mgr  = self._get_quality_manager()
        adj_mgr      = getattr(quality_mgr, "adjustments", None) if quality_mgr else None
        for instance_name in self._all_instances():
            if adj_mgr and hasattr(adj_mgr, "get_quality_adjustments"):
                adjustments = adj_mgr.get_quality_adjustments(instance_name)
            else:
                adjustments = (
                    self.radarr_api._make_request(instance_name, "qualitydefinition", fallback=[])
                    if self.radarr_api else []
                )
            self.global_cache.set(f"radarr.quality.adjustments.{instance_name}", adjustments)
            self.logger.log_info(f"Quality adjustments cached for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("run_keywords_data_pull")
    def run_keywords_data_pull(self, instance):
        movies_mgr = self._get_movies_manager()
        keywords_mgr = getattr(movies_mgr, "keywords", None) if movies_mgr else None
        for instance_name in self._all_instances():
            if keywords_mgr and hasattr(keywords_mgr, "get_keywords"):
                keywords = keywords_mgr.get_keywords(instance_name)
                self.global_cache.set(f"radarr.keywords.{instance_name}", keywords)
                self.logger.log_info(f"Keywords cached for {instance_name}")
            else:
                self.logger.log_debug(f"[Orchestration] keywords manager unavailable — skipping for '{instance_name}'")

    @LoggerManager().log_function_entry
    @timeit("run_credits_data_pull")
    def run_credits_data_pull(self, instance):
        movies_mgr = self._get_movies_manager()
        credits_mgr = getattr(movies_mgr, "credits", None) if movies_mgr else None
        for instance_name in self._all_instances():
            if credits_mgr and hasattr(credits_mgr, "get_people_and_studios"):
                credits = credits_mgr.get_people_and_studios(instance_name)
                self.global_cache.set(f"radarr.credits.{instance_name}", credits)
                self.logger.log_info(f"Credits cached for {instance_name}")
            else:
                self.logger.log_debug(f"[Orchestration] credits manager unavailable — skipping for '{instance_name}'")

    @LoggerManager().log_function_entry
    @timeit("run_enrichment")
    def run_enrichment(self):
        movies_mgr = self._get_movies_manager()
        enrich_mgr = getattr(movies_mgr, "enrich", None) if movies_mgr else None
        for instance_name in self._all_instances():
            if enrich_mgr and hasattr(enrich_mgr, "build_enriched_movies"):
                enriched = enrich_mgr.build_enriched_movies(instance_name)
                self.global_cache.set(f"radarr.movies.{instance_name}.enriched", enriched)
                self.logger.log_info(f"Enriched movie data cached for {instance_name}")
            else:
                self.logger.log_debug(f"[Orchestration] enrich manager unavailable — skipping for '{instance_name}'")

    @LoggerManager().log_function_entry
    @timeit("run_dataframe_build")
    def run_dataframe_build(self):
        movies_mgr  = self._get_movies_manager()
        df_mgr      = getattr(movies_mgr, "dataframe", None) if movies_mgr else None
        for instance_name in self._all_instances():
            if df_mgr and hasattr(df_mgr, "build_movie_dataframe"):
                df = df_mgr.build_movie_dataframe(instance_name)
                self.global_cache.set(f"radarr.movies.{instance_name}.dataframe", df)
                self.logger.log_info(f"DataFrame built and cached for {instance_name}")
            else:
                self.logger.log_debug(f"[Orchestration] dataframe manager unavailable — skipping for '{instance_name}'")

    # ── New: Movie files + relational parquet pulls ──────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_movie_files_pull")
    def run_movie_files_pull(self, instance: str) -> dict:
        """
        Build / refresh the movie_files Parquet for the given instance.
        Delegates to RadarrCacheMovieFilesManager.run().
        """
        resolved = self._resolve_instance(instance)
        cache_mgr = self._get_cache_manager()
        movie_files = getattr(cache_mgr, "movie_files", None) if cache_mgr else None

        if movie_files is None:
            # Fallback: try registry directly
            try:
                movie_files = self.registry.get("manager", "RadarrCacheMovieFilesManager")
            except Exception:
                pass

        if movie_files is None:
            self.logger.log_warning(
                f"[Orchestration] RadarrCacheMovieFilesManager not available — "
                f"skipping movie_files pull for '{resolved}'"
            )
            return {}

        self.logger.log_info(f"[Orchestration] Starting movie_files pull for '{resolved}'")
        stats = movie_files.run(resolved)
        self.logger.log_info(
            f"[Orchestration] movie_files pull complete for '{resolved}': {stats}"
        )
        return stats

    @LoggerManager().log_function_entry
    @timeit("run_movie_enrichment")
    def run_movie_enrichment(self, instance: str) -> int:
        """Broadcast the enrich daemon's cached cast/crew + Trakt rating onto movie_files
        rows (RadarrCacheMovieFilesManager.refresh_enrichment). CACHE-ONLY (the daemon owns
        fetching); runs before refresh_scores so the Group-B cast/crew affinity sees the
        enriched columns. The Radarr twin of the Sonarr run_episode_file_enrichment task."""
        resolved = self._resolve_instance(instance)
        cache_mgr = self._get_cache_manager()
        movie_files = getattr(cache_mgr, "movie_files", None) if cache_mgr else None
        if movie_files is None:
            try:
                movie_files = self.registry.get("manager", "RadarrCacheMovieFilesManager")
            except Exception:
                movie_files = None
        if movie_files is None or not hasattr(movie_files, "refresh_enrichment"):
            return 0
        try:
            return movie_files.refresh_enrichment(resolved)
        except Exception as e:
            self.logger.log_warning(f"[Orchestration] movie_enrichment failed for '{resolved}': {e}")
            return 0

    @LoggerManager().log_function_entry
    @timeit("run_relational_pull")
    def run_relational_pull(self, instance: str) -> dict:
        """
        Build / refresh the relational Parquet tables (people, relations, studios).

        If TraktMoviesManager is registered, movies are enriched with Trakt
        cast/crew data before the tables are built, populating the people and
        relations Parquets that are otherwise empty (Radarr's bulk /movie
        endpoint does not include credits).

        Falls back to relational.run() when no Trakt enrichment is available.
        """
        resolved  = self._resolve_instance(instance)
        cache_mgr = self._get_cache_manager()
        relational = getattr(cache_mgr, "relational", None) if cache_mgr else None

        if relational is None:
            try:
                relational = self.registry.get("manager", "RadarrCacheRelationalManager")
            except Exception:
                pass

        if relational is None:
            self.logger.log_warning(
                f"[Orchestration] RadarrCacheRelationalManager not available — "
                f"skipping relational pull for '{resolved}'"
            )
            return {}

        self.logger.log_info(f"[Orchestration] Starting relational pull for '{resolved}'")

        trakt_movies = self._get_trakt_movies_manager()
        if trakt_movies:
            # Pull movies from global cache (populated by run_movie_data_pull)
            movies = self.global_cache.get(f"radarr.movies.{resolved}.full") or []
            if not movies and self.radarr_api:
                movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []

            # Prioritise movies the user has actually watched — both from Trakt's own
            # sync history (exact tmdbId match) and from Tautulli watch history (title
            # match for Plex plays not scrobbled to Trakt).  Everything else is processed
            # in chunks of chunk_size per run to stay under Trakt's rate limit.
            watched_tmdb_ids: set[int] = set()
            watched_titles:   set[str] = set()

            if self.global_cache:
                # Trakt movie history — exact tmdbId match (cached by TraktManager.run)
                trakt_history = self.global_cache.get("trakt/history/movies")
                if isinstance(trakt_history, list):
                    for entry in trakt_history:
                        tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
                        if tmdb_id:
                            watched_tmdb_ids.add(int(tmdb_id))

                # Tautulli history — title fallback for Plex plays not scrobbled to Trakt
                tautulli_history = self.global_cache.get("tautulli/history/all")
                if isinstance(tautulli_history, list):
                    watched_titles = {
                        e.get("title", "").lower().strip()
                        for e in tautulli_history
                        if e.get("media_type") == "movie" and e.get("title")
                    }

            if watched_tmdb_ids or watched_titles:
                self.logger.log_info(
                    f"[Orchestration] Priority set: {len(watched_tmdb_ids)} Trakt tmdbIds, "
                    f"{len(watched_titles)} Tautulli titles."
                )

            # When the background enrichment daemon is enabled it owns ALL live
            # Trakt fetching; the run reads only what it has already cached, so it
            # can never hang on a 429. Surface the coverage state + an ETA so the
            # user knows results sharpen as the daemon fills the cache.
            daemon_enabled = bool(
                ((self.config.get("daemons", {}) or {}).get("enrich") or {}).get("enabled")
            ) if self.config else False

            if daemon_enabled:
                self._log_enrichment_eta(resolved, movies)
            else:
                self.logger.log_info(
                    f"[Orchestration] Trakt enrichment active for '{resolved}' "
                    f"({len(movies)} movies total)"
                )
            movies = trakt_movies.enrich_movies(
                movies,
                watched_titles=watched_titles,
                watched_tmdb_ids=watched_tmdb_ids,
                chunk_size=500,
                cache_only=daemon_enabled,
            )
            stats  = relational.build_relations_from_movies(movies, resolved)
        else:
            # No Trakt — studios-only run via the standard path
            stats = relational.run(resolved)

        self.logger.log_info(
            f"[Orchestration] Relational pull complete for '{resolved}': {stats}"
        )
        return stats

    def _log_enrichment_eta(self, resolved: str, movies: list) -> None:
        """Explain that enrichment is delegated to the daemon and estimate how long
        full coverage of the in-library (owned) movie set will take at the rate limit."""
        try:
            from math import ceil

            from scripts.managers.factories.daemons.daemon_paths import (
                DEFAULT_SCOPE, MOVIE_BUCKETS, SAFE_THROUGHPUT_CALLS, SLEEP_SECONDS,
            )

            enrich_cfg = (self.config.get("daemons", {}) or {}).get("enrich", {}) or {}
            scope = [s for s in (enrich_cfg.get("scope") or DEFAULT_SCOPE) if s in MOVIE_BUCKETS]
            scope = scope or list(DEFAULT_SCOPE)

            owned = sum(1 for m in movies if m.get("hasFile"))
            m = len(scope)
            total_calls = owned * m
            windows = max(1, ceil(total_calls / SAFE_THROUGHPUT_CALLS)) if total_calls else 0
            eta_s = windows * SLEEP_SECONDS

            def _human(sec: int) -> str:
                if sec <= 0:
                    return "0m"
                if sec < 3600:
                    return f"~{ceil(sec / 60)}m"
                if sec < 48 * 3600:
                    return f"~{sec / 3600:.1f}h"
                return f"~{sec / 86400:.1f}d"

            # Route into the consolidated end-of-run summary: one row per (service,
            # instance) instead of a per-instance vertical block scattered through the live
            # log. Pure ASCII cells - no emoji/unicode that mojibakes on cp1252. Falls back
            # to the original inline callout table when no run_summary collector is present.
            rs = getattr(self.global_cache, "run_summary", None) if self.global_cache else None
            if rs is not None:
                rs.add_rows(
                    "radarr",
                    "Trakt enrichment -> background daemon (cache-only this run)",
                    resolved,
                    ["owned movies", "endpoints/movie", "Trakt calls", "throughput", "est. full enrich"],
                    [[f"{owned:,}", str(m), f"{total_calls:,}",
                      f"~{SAFE_THROUGHPUT_CALLS}/5min", _human(eta_s)]],
                    order=90,   # enrichment sits near the end of each service's block
                )
            else:
                self.logger.log_table(
                    [f"TRAKT ENRICHMENT -> background daemon ('{resolved}')", ""],
                    [
                        ["this run",            "CACHE-ONLY - zero live Trakt calls (no 429 hang)"],
                        ["in-library movies",   f"{owned:,}"],
                        ["endpoints / movie",   str(m)],
                        ["Trakt calls to cover", f"{total_calls:,}"],
                        ["throughput",          f"~{SAFE_THROUGHPUT_CALLS} calls / 5 min"],
                        ["est. full enrich",    _human(eta_s)],
                        ["accuracy",            "improves every run as the daemon fills the cache"],
                    ],
                )
        except Exception as e:
            self.logger.log_debug(f"[Orchestration] enrichment ETA log skipped: {e}")

    @LoggerManager().log_function_entry
    @timeit("run_movie_ratings")
    def run_movie_ratings(self, instance: str) -> dict:
        """
        Auto-rate movies on Trakt based on what the household has watched,
        using the completion map + affinity data built by TautulliManager.run().

        Requires:
        - ``tautulli/affinity``                           — genre/actor/director affinity
        - ``tautulli/group/<group>/tmdb_completions``     — per-movie completion pct
        - ``trakt/history/movies``                        — for collection-bonus calculation
        - TraktRatingsManager in registry
        """
        resolved = self._resolve_instance(instance)

        # ── Movies from cache or Radarr API ──────────────────────────────
        movies: list[dict] = self.global_cache.get(f"radarr.movies.{resolved}.full") or []
        if not movies and self.radarr_api:
            movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []

        if not movies:
            self.logger.log_warning(
                f"[Orchestration] No movies available for '{resolved}' — skipping movie ratings."
            )
            return {}

        # ── Completion map: merge all configured groups ───────────────────
        # Each group stores {tmdb_id (str|int): {"pct": float, "threshold": float}}
        completion_map: dict[int, dict] = {}
        # Discover group names from config
        rating_groups_cfg = (self.config.get("rating_groups", {}) if self.config else {})
        for group_name in (rating_groups_cfg or {"household": {}}):
            raw = (self.global_cache.get(
                f"tautulli/group/{group_name}/tmdb_completions"
            ) or {}) if self.global_cache else {}
            for tmdb_str, data in raw.items():
                try:
                    tmdb_id = int(tmdb_str)
                except (ValueError, TypeError):
                    continue
                existing = completion_map.get(tmdb_id, {})
                if float(data.get("pct", 0.0)) >= float(existing.get("pct", 0.0)):
                    completion_map[tmdb_id] = data

        if not completion_map:
            self.logger.log_info(
                "[Orchestration] Tautulli group completion map is empty — "
                "no watched movies with resolved tmdb_id yet. "
                "This resolves automatically once the Tautulli metadata cache rebuilds "
                "(check 'tautulli/group/*/tmdb_completions' in cache)."
            )
            return {}

        # ── Genre / actor / director affinity ────────────────────────────
        genre_affinity: dict = (
            self.global_cache.get("tautulli/affinity") or {}
        ) if self.global_cache else {}

        # ── Watched tmdbIds for collection bonus ─────────────────────────
        watched_tmdb_ids: set[int] = set()
        if self.global_cache:
            trakt_history = self.global_cache.get("trakt/history/movies") or []
            for entry in trakt_history:
                tmdb_id = ((entry.get("movie") or {}).get("ids") or {}).get("tmdb")
                if tmdb_id:
                    watched_tmdb_ids.add(int(tmdb_id))
            # Also include all tmdb_ids from the completion map (Tautulli-watched)
            watched_tmdb_ids.update(completion_map.keys())

        # ── Managers ─────────────────────────────────────────────────────
        ratings_mgr = None
        try:
            ratings_mgr = self.registry.get("manager", "TraktRatingsManager")
        except Exception:
            pass

        if not ratings_mgr:
            self.logger.log_warning(
                "[Orchestration] TraktRatingsManager not in registry — "
                "skipping movie ratings (is TraktManager configured?)."
            )
            return {}

        trakt_movies   = self._get_trakt_movies_manager()
        people_manager = getattr(trakt_movies, "people", None) if trakt_movies else None

        self.logger.log_info(
            f"[Orchestration] Movie ratings: {len(completion_map)} watched movies, "
            f"{len(watched_tmdb_ids)} in watched set, "
            f"people_manager={'yes' if people_manager else 'no'}."
        )

        return ratings_mgr.auto_rate_watched_movies(
            movies=movies,
            completion_map=completion_map,
            watched_tmdb_ids=watched_tmdb_ids,
            genre_affinity=genre_affinity,
            people_manager=people_manager,
        )

    # ── Full orchestrated run ────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self, instance: str | None = None) -> dict:
        """
        Orchestrate all data pulls and lifecycle management for Radarr.
        If instance is None, iterates all configured instances.
        """
        instances = [instance] if instance else self._all_instances()
        if not instances:
            self.logger.log_warning("[Orchestration] No Radarr instances found — nothing to run")
            return {}

        # Start each Radarr run with a cold movie_files cache so the per-task reads
        # below reuse one in-memory dataframe instead of re-reading the parquet ~8-11x
        # per instance. Best-effort: a missing manager must never break the run.
        try:
            _cache_mgr = self._get_cache_manager()
            _mf = getattr(_cache_mgr, "movie_files", None) if _cache_mgr else None
            if _mf is None:
                _mf = self.registry.get("manager", "RadarrCacheMovieFilesManager")
            if _mf is not None and hasattr(_mf, "reset_run_cache"):
                _mf.reset_run_cache()
        except Exception:
            pass

        results: dict = {}
        for inst in instances:
            resolved = self._resolve_instance(inst)
            inst_results: dict = {}

            # Core data pulls + universe quality management
            for task_name, task_fn in [
                ("movie_data",     lambda i: self.run_movie_data_pull(i)),
                ("monitoring",     lambda i: self.run_monitoring_data_pull(i)),
                ("quality",        lambda i: self.run_quality_data_pull(i)),
                ("tags",           lambda i: self.run_tag_data_pull(i)),
                ("movie_files",    lambda i: self.run_movie_files_pull(i)),
                ("relational",     lambda i: self.run_relational_pull(i)),
                ("movie_ratings",  lambda i: self.run_movie_ratings(i)),
                ("movie_enrichment", lambda i: self.run_movie_enrichment(i)),
                # refresh_scores MUST come before space_pressure: the deletion and
                # active-watcher-upgrade stages read the persisted watchability_score
                # column, and refresh_scores is its only writer. Running space_pressure
                # first left those stages ranking on the *previous* run's scores (and
                # the live-fallback only on a cold cache). Downgrades are unaffected —
                # they recompute scores live — but deletions/upgrades read the column.
                # Sonarr already orders scoring before its upgrade/downgrade passes.
                ("refresh_scores",  lambda i: self.run_refresh_scores(i)),
                ("space_pressure", lambda i: self.run_space_pressure(i)),
                ("universe",       lambda i: self.run_universe_quality(i)),
            ]:
                try:
                    inst_results[task_name] = task_fn(resolved) or "done"
                except Exception as e:
                    inst_results[task_name] = f"error: {e}"
                    self.logger.log_warning(f"[Orchestration] {task_name} failed for '{resolved}': {e}")

            results[resolved] = inst_results

        return results

    @LoggerManager().log_function_entry
    @timeit("run_refresh_scores")
    def run_refresh_scores(self, instance: str) -> int:
        """
        Compute watchability scores for every Parquet row and persist them.
        Must run before run_space_pressure (whose deletion + active-watcher
        upgrade stages read the persisted watchability_score column) and before
        run_universe_quality (which gates 4K eligibility on the score).
        """
        resolved    = self._resolve_instance(instance)
        quality_mgr = self._get_quality_manager()
        sp_mgr      = getattr(quality_mgr, "space_pressure", None) if quality_mgr else None
        if sp_mgr is None:
            return 0
        n = sp_mgr.refresh_scores(resolved)
        self.logger.log_info(
            f"[Orchestration] Refreshed scores for {n} movies in '{resolved}'"
        )
        return n

    @LoggerManager().log_function_entry
    @timeit("run_space_pressure")
    def run_space_pressure(self, instance: str) -> dict:
        """
        Run the space-pressure pipeline: downgrade low-priority movies to
        HD-720p when free space is below 25 GB, then delete as last resort.
        Delegates to RadarrSpacePressureManager.run().
        """
        resolved    = self._resolve_instance(instance)
        quality_mgr = self._get_quality_manager()
        sp_mgr      = getattr(quality_mgr, "space_pressure", None) if quality_mgr else None

        if sp_mgr is None:
            self.logger.log_debug(
                f"[Orchestration] RadarrSpacePressureManager not available — "
                f"skipping space pressure pass for '{resolved}'"
            )
            return {}

        stats = sp_mgr.run(resolved)
        self.logger.log_info(f"[Orchestration] Space pressure stats for '{resolved}': {stats}")
        return stats

    @LoggerManager().log_function_entry
    @timeit("run_universe_quality")
    def run_universe_quality(self, instance: str) -> dict:
        """
        Evaluate free space and apply quality changes (downgrade / upgrade) for
        universe-tagged movies.  Delegates to RadarrQualityUniverseManager.run().
        No-ops gracefully if the manager is unavailable.
        """
        resolved    = self._resolve_instance(instance)
        quality_mgr = self._get_quality_manager()
        universe    = getattr(quality_mgr, "universe", None) if quality_mgr else None

        if universe is None:
            self.logger.log_debug(
                f"[Orchestration] RadarrQualityUniverseManager not available — "
                f"skipping universe quality pass for '{resolved}'"
            )
            return {}

        # Determine current free space from storage manager
        storage_mgr = self._get_storage_manager()
        space_mgr   = getattr(storage_mgr, "space", None) if storage_mgr else None
        free_gb     = 0.0
        if space_mgr and hasattr(space_mgr, "get_free_space_per_instance"):
            try:
                space_map = space_mgr.get_free_space_per_instance()
                free_gb   = space_map.get(resolved, 0.0)
            except Exception as e:
                self.logger.log_debug(f"[Orchestration] Could not read free space for '{resolved}': {e}")

        self.logger.log_info(
            f"[Orchestration] Universe quality pass for '{resolved}' ({free_gb:.1f} GB free)"
        )
        stats = universe.run(resolved, free_gb)
        self.logger.log_info(f"[Orchestration] Universe quality stats for '{resolved}': {stats}")
        return stats

    @timeit("get_warmup_tasks")
    def get_warmup_tasks(self) -> dict:
        """
        Return a dict of {task_name: callable(instance)} for cache warmup.
        All callables route through the correct sub-managers.
        """
        quality_mgr = self._get_quality_manager()
        cache_mgr   = self._get_cache_manager()

        return {
            "tags": lambda i: (
                self.radarr_api._make_request(i, "tag", fallback=[]) if self.radarr_api else []
            ),
            "quality_profiles": lambda i: (
                getattr(quality_mgr, "selector", None).get_quality_profiles(i)
                if quality_mgr and hasattr(getattr(quality_mgr, "selector", None), "get_quality_profiles")
                else (self.radarr_api._make_request(i, "qualityprofile", fallback=[]) if self.radarr_api else [])
            ),
            "custom_formats": lambda i: (
                getattr(quality_mgr, "custom_formats", None).get_custom_formats(i)
                if quality_mgr and hasattr(getattr(quality_mgr, "custom_formats", None), "get_custom_formats")
                else (self.radarr_api._make_request(i, "customformat", fallback=[]) if self.radarr_api else [])
            ),
            "quality_definitions": lambda i: (
                self.radarr_api._make_request(i, "qualitydefinition", fallback=[]) if self.radarr_api else []
            ),
            "disk": lambda i: (
                self.radarr_api._make_request(i, "rootfolder", fallback=[]) if self.radarr_api else []
            ),
            "history": lambda i: (
                self.radarr_api._make_request(i, "history", fallback=[]) if self.radarr_api else []
            ),
        }

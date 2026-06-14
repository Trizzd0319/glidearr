from scripts.managers.services.tautulli.watch_history import TautulliWatchHistoryManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class WatchHistoryAggregator:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache, config, cache_manager=None, api_tautulli=None, api_trakt=None, api_plex=None):
        self.logger = logger
        self.cache = cache
        self.config = config
        self.cache_manager = cache_manager
        self.api_tautulli = api_tautulli
        self.api_trakt = api_trakt
        self.api_plex = api_plex

        # Initialize the watch-history manager (Tautulli-backed)
        self.watch_history_manager = TautulliWatchHistoryManager(
            logger=logger, global_cache=cache, tautulli_api=api_tautulli
        )

        # Store API references
        self.trakt = api_trakt
        self.plex = api_plex

    @LoggerManager().log_function_entry
    @timeit("get_all_watched_series")
    def get_all_watched_series(self):
        """
        Returns a set of TVDB IDs that have been watched by any user across Tautulli, Trakt, or Plex.
        """
        watched_tvdb_ids = set()

        watched_tvdb_ids.update(self._get_tautulli_watched())
        watched_tvdb_ids.update(self._get_trakt_watched())
        watched_tvdb_ids.update(self._get_plex_watched())

        if watched_tvdb_ids:
            self.logger.log_info(f"🎬 Watched Series (TVDB IDs): {sorted(watched_tvdb_ids)}")
        else:
            self.logger.log_info("📭 No watched movies found across any source.")

        return watched_tvdb_ids

    @LoggerManager().log_function_entry
    @timeit("_get_tautulli_watched")
    def _get_tautulli_watched(self):
        """
        Loads Tautulli watch history and extracts TVDB IDs from episode-level data.
        """
        data = self.cache.get_or_generate_cache(
            "tautulli/watch_history",
            lambda: self.watch_history_manager.get_combined_history(source="tautulli"),
            expiration_time=24
        )
        return {
            int(entry.get("grandparent_rating_key"))
            for entry in data
            if entry.get("media_type") == "episode" and entry.get("grandparent_rating_key")
        }

    @LoggerManager().log_function_entry
    @timeit("_get_trakt_watched")
    def _get_trakt_watched(self):
        """
        Loads watched Trakt shows and extracts TVDB IDs.
        """
        data = self.cache.get_or_generate_cache(
            "trakt/watched",
            lambda: self.api_trakt.api.get_watched_series() if self.api_trakt and hasattr(self.api_trakt,
                                                                                          "api") else [],
            expiration_time=24
        )
        tvdb_ids = set()
        for item in data:
            ids = (item.get("show") or {}).get("ids") or {}
            if tvdb := ids.get("tvdb"):
                tvdb_ids.add(tvdb)
        return tvdb_ids

    @LoggerManager().log_function_entry
    @timeit("_get_plex_watched")
    def _get_plex_watched(self):
        """
        Loads Plex watched shows and extracts TVDB IDs (ratingKey assumed to be TVDB ID).
        """
        data = self.cache.get_or_generate_cache(
            "plex/watched",
            lambda: self.api_plex.api.get_watched_series() if self.api_plex and hasattr(self.api_plex, "api") else [],
            expiration_time=24
        )
        return {
            int(entry.get("ratingKey"))
            for entry in data
            if entry.get("type") == "show" and entry.get("ratingKey")
        }

    @LoggerManager().log_function_entry
    @timeit("_detect_and_assign_binge_profiles")
    def _detect_and_assign_binge_profiles(self):
        """
        Detects users from Tautulli watch history and ensures they are added to config with default binge hours.
        """
        config = self.cache.config
        if "binge_hours_per_user" not in config:
            config["binge_hours_per_user"] = {}

        tautulli_data = self.cache.get_or_generate_cache(
            "tautulli/watch_history",
            lambda: self.watch_history_manager.get_combined_history(source="tautulli"),
            expiration_time=24
        )

        for entry in tautulli_data:
            user = entry.get("user")
            if user and user not in config["binge_hours_per_user"]:
                config["binge_hours_per_user"][user] = 2  # Default to 2 hours
                self.logger.log_info(f"🆕 Assigned default binge window (2h) to user '{user}'")

        # Save updated config if new entries were added
        self.cache.save_config(config)

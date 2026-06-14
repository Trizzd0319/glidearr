from scripts.mal import anime

from scripts.managers.factories.config import ConfigManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

# from scripts.managers.machine_learning.instance_selector import InstanceSelector

logger = LoggerManager()
config_manager = ConfigManager(logger)


class SeriesManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache, watch_history, metadata_manager, config, sonarr_apis, transcode_manager,
                 user_manager):
        self.logger = logger
        self.cache = cache
        self.watch_watch_history = watch_history
        self.metadata_manager = metadata_manager
        self.config = config

        # self.instance_selector = InstanceSelector(
        #     logger=logger,
        #     cache=cache,
        #     config=config.get_config(),
        #     sonarr_apis=sonarr_apis,
        #     transcode_manager=transcode_manager,
        #     user_manager=user_manager
        # )

        anime_genres = [
            g.lower()
            for g in ConfigManager(self.logger).config.get("animeGenres", ["anime"])
        ]

    @LoggerManager().log_function_entry
    @timeit("_enrich_genres_with_mal")
    def _enrich_genres_with_mal(self, title):
        """
        Enrich genre list using MyAnimeList via mal-api.py.
        """
        try:
            client_id = self.cache.sonarr_api.config.get("mal", {}).get("client_id")
            if not client_id:
                self.logger.log_warning("⚠️ MAL client_id not found in config.")
                return []

            search = anime.AnimeSearch(title, client_id=client_id)
            if not search.results:
                self.logger.log_warning(f"⚠️ No MAL results found for '{title}'.")
                return []

            mal_genres = [genre.name.lower() for genre in search.results[0].genres]
            self.logger.log_info(f"🎌 MAL genres for '{title}': {mal_genres}")
            return mal_genres

        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch MAL genres for '{title}': {e}")
            return []

    @LoggerManager().log_function_entry
    @timeit("get_watched_series")
    def get_watched_series(self):
        """Retrieves watched movies and maps them to Sonarr instance using ML + MAL genre enrichment for anime."""
        self.logger.log_info("🔄 Fetching watched movies from Tautulli cache...")

        history = self.watch_watch_history.get_history()
        if not history:
            self.logger.log_warning("⚠️ No watch history data found in Tautulli.")
            return {}

        watched_series = {}

        for entry in history:
            title = entry.get("grandparent_title", "Unknown Series")
            season = int(entry.get("parent_media_index", 1))
            episode = int(entry.get("media_index", 1))
            user = entry.get("user", None)
            rating_key = entry.get("rating_key")

            metadata = self.metadata_manager.get_metadata(rating_key)
            genres = metadata.get("genres", [])
            runtime = metadata.get("duration", 0) // 60  # seconds → minutes

            lower_genres = [g.lower() for g in genres]
            is_anime = any(g in self.anime_genres for g in lower_genres)

            # ✅ Enrich anime genres via MAL
            if is_anime:
                mal_genres = self._enrich_genres_with_mal(title)
                genres = list(set(genres + mal_genres))

            instance = self.instance_selector.select_instance(
                series_title=title,
                genre_list=genres,
                runtime_minutes=runtime,
                user=user
            )

            key = (instance, title)
            if key not in watched_series or (season, episode) > watched_series[key]:
                watched_series[key] = (season, episode)

            self.logger.log_info(
                f"✅ '{title}' matched to Sonarr instance {instance} at S{season}E{episode} (Genres: {genres})."
            )

        structured_watched_series = {}
        for (instance, title), (season, episode) in watched_series.items():
            structured_watched_series.setdefault(instance, {})[title] = {
                "season": season,
                "episode": episode
            }

        return structured_watched_series

    @LoggerManager().log_function_entry
    @timeit("_generate_episode_trends")
    def _generate_episode_trends(self):
        """Generates episode watch trends based on viewing history."""
        watched_history = self.watch_watch_history.get_history()
        if not watched_history:
            self.logger.log_warning("⚠️ No Tautulli history data available. Skipping processing.")
            return {}

        episode_trends = {}
        for entry in watched_history:
            user = entry.get("user", "Unknown User")
            series = entry.get("grandparent_title", "Unknown Series")
            season = str(entry.get("parent_media_index", "00")).zfill(2)
            episode = str(entry.get("media_index", "00")).zfill(2)

            key = f"{user}.{series}.S{season}E{episode}"
            episode_trends[key] = episode_trends.get(key, 0) + 1

        return episode_trends

    @LoggerManager().log_function_entry
    @timeit("_generate_tracked_series")
    def _generate_tracked_series(self):
        history = self.watch_watch_history.get_history()
        if not history:
            self.logger.log_warning("⚠️ No viewing history found.")
            return []

        return list(set(entry.get("grandparent_title", "Unknown Series") for entry in history))

    @LoggerManager().log_function_entry
    @timeit("_generate_future_episodes")
    def _generate_future_episodes(self):
        """Generates the next 5 episodes based on watched history."""
        watched_series = self.get_watched_series()
        if not watched_series:
            self.logger.log_warning("⚠️ No watched movies found.")
            return {}

        future_episodes = {}
        future_table = []
        for instance, series_data in watched_series.items():
            for series, data in series_data.items():
                season = data["season"]
                episode = data["episode"]
                future_episodes.setdefault(instance, {})[series] = [(season, episode + i) for i in range(1, 6)]

                for s, e in future_episodes[instance][series]:
                    future_table.append([instance, series, f"S{s}E{e}"])

        self.logger.log_table(["Instance", "Series", "Next Episode"], future_table,
                              title="📽️ Future Episode Predictions")
        return future_episodes

    @LoggerManager().log_function_entry
    @timeit("cache_tracked_series")
    def cache_tracked_series(self):
        """
        Caches all movies currently being tracked by Tautulli.
        Logs before and after caching to track execution flow.
        """
        self.logger.log_info("🔍 Fetching and caching tracked movies from Tautulli.")

        try:
            cached_data = self.cache.get_or_generate_cache(
                "tracked_series",
                self._generate_tracked_series,
                instance="default"
            )
            self.logger.log_info(f"✅ Successfully cached {len(cached_data)} tracked movies.")
            return cached_data
        except Exception as e:
            self.logger.log_error(f"❌ Error caching tracked movies: {e}")
            return {}

    @LoggerManager().log_function_entry
    @LoggerManager().log_function_entry
    @timeit("cache_future_episodes")
    def cache_future_episodes(self):
        """
        Caches the next set of episodes expected to be watched.
        Ensures caching is handled efficiently and logs details.
        Displays a formatted table showing all future episodes per movies.
        """
        self.logger.log_info("🔍 Fetching and caching future episodes.")

        try:
            cached_data = self.cache.get_or_generate_cache(
                "future_episodes",
                self._generate_future_episodes,
                instance="default"
            )

            self.logger.log_info(f"✅ Successfully cached future episodes for {len(cached_data)} movies.")

            # 🔍 Build pretty table showing future episodes in one row
            table_data = []

            for instance, series_dict in cached_data.items():
                for series, episode_list in series_dict.items():
                    formatted_episodes = []
                    genres = self._lookup_genres(series)
                    runtime = self._lookup_runtime(series)

                    for s, e in episode_list:
                        current_instance = instance

                        # 🔄 Call MLManager's method for expected instance prediction
                        future_instance = self.ml_manager.get_expected_instance_for_episode(
                            series_title=series,
                            genres=genres,
                            runtime_minutes=runtime,
                            user=None  # Or add user logic here if needed
                        )

                        if future_instance and future_instance != current_instance:
                            formatted_episodes.append(f"S{s}E{e} ({future_instance})")
                        else:
                            formatted_episodes.append(f"S{s}E{e}")

                    joined_episodes = ", ".join(formatted_episodes)
                    table_data.append([instance, series, joined_episodes])

            self.logger.log_table(["Instance", "Series", "Next Episodes"], table_data,
                                  title="📽️ Future Episode Predictions")
            return cached_data

        except Exception as e:
            self.logger.log_error(f"❌ Error caching future episodes: {e}")
            return {}

    @LoggerManager().log_function_entry
    @timeit("_generate_episode_trends")
    def _generate_episode_trends(self):
        """Generates episode watch trends based on viewing history."""
        watched_history = self.watch_watch_history.get_history()
        if not watched_history:
            self.logger.log_warning("⚠️ No Tautulli history data available. Skipping processing.")
            return {}

        episode_trends = {}
        for entry in watched_history:
            user = entry.get("user", "Unknown User")
            series = entry.get("grandparent_title", "Unknown Series")
            season = str(entry.get("parent_media_index", "00")).zfill(2)
            episode = str(entry.get("media_index", "00")).zfill(2)

            key = f"{user}.{series}.S{season}E{episode}"
            episode_trends[key] = episode_trends.get(key, 0) + 1

        return episode_trends

    @LoggerManager().log_function_entry
    @timeit("_lookup_genres")
    def _lookup_genres(self, series_title):
        """
        Fetch genres from local metadata_manager cache, but if 'anime' is detected,
        enhance the genre list using MyAnimeList for richer context.
        """
        # Step 1: Fetch from local Sonarr/TVDB cache
        metadata = self.cache.load_cache("series_metadata", instance="default")
        genres = metadata.get(series_title, {}).get("genres", [])

        # Step 2: If it's marked as 'anime', enhance with MAL genres
        if "anime" in [g.lower() for g in genres]:
            cache_key = f"mal_genres::{series_title.lower()}"
            mal_genres = self.cache.get_or_generate_cache(
                cache_key,
                lambda: self._enrich_genres_with_mal(series_title),
                instance="default"
            )
            if mal_genres:
                self.logger.log_info(
                    f"🎌 Enhanced anime genres for '{series_title}' via cached MAL lookup: {mal_genres}")
                genres.extend(g for g in mal_genres if g not in genres)

        return genres

    @LoggerManager().log_function_entry
    @timeit("_lookup_mal_genres")
    def _lookup_mal_genres(self, series_title):
        """
        Searches MyAnimeList for the given title and returns the genre list.
        Requires `mal` client_id and authentication from config.
        """
        try:
            from scripts.myanimelistpy import models
            search = AnimeSearch(series_title)
            if not search.results:
                self.logger.log_warning(f"⚠️ No results from scripts.mAL for '{series_title}'.")
                return []

            # Use first result
            anime = search.results[0]
            mal_details = anime.details  # triggers fetch of full data if lazy-loaded

            return [genre["name"] for genre in mal_details.genres]

        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed MAL genre lookup for '{series_title}': {e}")
            return []

    @LoggerManager().log_function_entry
    @timeit("_lookup_runtime")
    def _lookup_runtime(self, series_title):
        """Stub or fallback runtime. Replace with actual logic if needed."""
        # Optionally read from Tautulli or Sonarr episode metadata_manager
        return 45  # Default to 45 minutes

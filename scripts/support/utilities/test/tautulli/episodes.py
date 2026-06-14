from collections import Counter

from scripts.managers.services.tautulli.api import TautulliAPI

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class EpisodeManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger: LoggerManager, cache: GlobalCacheManager, api: TautulliAPI, watch_history=None):
        """
        Initializes EpisodeManager with necessary dependencies.
        """
        self.logger = logger
        self.cache = cache
        self.api = api
        self.watch_history = watch_history

    @LoggerManager().log_function_entry
    @timeit("_generate_episode_trends")
    def _generate_episode_trends(self):
        """
        Generates episode watch trends based on viewing history.
        """
        watched_history = self.history_manager.get_history()
        if not watched_history:
            self.logger.log_warning("⚠️ No Tautulli history data available. Skipping processing.")
            return {}

        episode_trends = {}
        for entry in watched_history:
            user = entry.get("user", "Unknown User")
            series = entry.get("grandparent_title", "Unknown Series")
            season = str(entry.get("parent_media_index", "00")).zfill(2)
            episode = str(entry.get("media_index", "00")).zfill(2)

            key = f"{series}.S{season}E{episode}"
            episode_trends[key] = episode_trends.get(key, 0) + 1

        self.logger.log_info(f"✅ Generated episode trends for {len(episode_trends)} episodes.")
        return episode_trends

    @LoggerManager().log_function_entry
    @timeit("cache_episode_trends")
    def cache_episode_trends(self):
        """
        Caches episode watch trends.
        Ensures that caching only occurs when necessary and logs relevant details.
        """
        self.logger.log_info("🔍 Fetching and caching episode watch trends.")

        return self.cache.get_or_generate_cache(
            "episode_trends",
            self._generate_episode_trends,
            instance="default"
        )

    @LoggerManager().log_function_entry
    @timeit("monitor_next_episode")
    def monitor_next_episode(self, tvdb_id, season, episode):
        """
        Monitors the next episode of a movies using cached episode data.
        """
        self.logger.log_info(f"📌 Checking next episode for TVDB ID {tvdb_id}, S{season}E{episode}.")

        cached_episodes = self.cache.load_cache("episode_monitoring") or {}
        next_episode_num = int(episode) + 1

        for key, episode_data in cached_episodes.items():
            if (
                    episode_data.get("tvdb_id") == tvdb_id
                    and int(episode_data.get("season", 0)) == int(season)
                    and int(episode_data.get("episode", 0)) == next_episode_num
            ):
                episode_id = key.split(".")[1]
                self.logger.log_info(
                    f"✅ Monitoring next episode: S{season.zfill(2)}E{str(next_episode_num).zfill(2)} (Episode ID: {episode_id})")
                self.api._make_request("monitor_episode", {"episode_id": episode_id})
                return True

        self.logger.log_warning(
            f"⚠️ Next episode (S{season.zfill(2)}E{str(next_episode_num).zfill(2)}) not found in cached data.")
        return False

    @LoggerManager().log_function_entry
    @timeit("get_most_watched_episodes")
    def get_most_watched_episodes(self, top_n=10):
        """
        Returns the top N most frequently watched episodes.
        """
        episode_trends = self.cache_episode_trends()
        if not episode_trends:
            self.logger.log_warning("⚠️ No episode trends data available.")
            return []

        most_common = Counter(episode_trends).most_common(top_n)
        self.logger.log_info(f"🏆 Top {top_n} most-watched episodes retrieved.")
        return most_common

    @LoggerManager().log_function_entry
    @timeit("get_user_favorite_episodes")
    def get_user_favorite_episodes(self, user, top_n=5):
        """
        Returns the top N favorite episodes for a specific user.
        """
        watched_history = self.history_manager.get_history()
        if not watched_history:
            self.logger.log_warning("⚠️ No watch history data available.")
            return []

        user_episodes = [
            f"{entry.get('grandparent_title', 'Unknown Series')}.S{str(entry.get('parent_media_index', '00')).zfill(2)}E{str(entry.get('media_index', '00')).zfill(2)}"
            for entry in watched_history if entry.get('user') == user
        ]

        favorite_episodes = Counter(user_episodes).most_common(top_n)
        self.logger.log_info(f"⭐ User '{user}' favorite episodes retrieved.")
        return favorite_episodes

    @LoggerManager().log_function_entry
    @timeit("get_least_watched_episodes")
    def get_least_watched_episodes(self, bottom_n=10):
        """
        Returns the bottom N least-watched episodes.
        """
        episode_trends = self.cache_episode_trends()
        if not episode_trends:
            self.logger.log_warning("⚠️ No episode trends data available.")
            return []

        least_common = sorted(episode_trends.items(), key=lambda item: item[1])[:bottom_n]
        self.logger.log_info(f"📉 Bottom {bottom_n} least-watched episodes retrieved.")
        return least_common

    @LoggerManager().log_function_entry
    @timeit("determine_episodes_to_monitor")
    def determine_episodes_to_monitor(self, series_title, user):
        """
        Uses binge-watching patterns from user cache to decide how many episodes to pre-monitor.
        """
        binge_data = self.cache.load_cache("binge_watching_patterns")
        key = f"{user}.{series_title}"

        if key in binge_data:
            recent_watches = binge_data[key][-5:]  # last 5 watch timestamps
            watch_intervals = [j - i for i, j in zip(recent_watches[:-1], recent_watches[1:])]
            average_interval = sum(watch_intervals) / len(watch_intervals)

            if average_interval < 3600:  # User watches episodes within an hour consistently
                episodes_to_monitor = 5
            elif average_interval < 86400:  # Within a day
                episodes_to_monitor = 3
            else:
                episodes_to_monitor = 1
        else:
            episodes_to_monitor = 1  # Default fallback

        self.logger.log_info(
            f"✅ {episodes_to_monitor} episodes will be pre-monitored for user '{user}' in movies '{series_title}'."
        )

        return episodes_to_monitor

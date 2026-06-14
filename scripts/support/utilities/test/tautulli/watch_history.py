# Standard library imports
import datetime
import json
import math
import time
from collections import Counter, defaultdict

import requests
from tqdm.contrib import concurrent

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


# Local module imports


class WatchHistoryManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger, cache, api_tautulli=None, api_trakt=None, api_plex=None):
        self.logger = logger
        self.cache = cache or GlobalCacheManager(logger=logger, config={})
        self.api_tautulli = api_tautulli
        self.api_trakt = api_trakt
        self.api_plex = api_plex
        self.default_length = 1000
        self._history_loaded = False
        self._history_cache = None

    @LoggerManager().log_function_entry
    @timeit("load_history_once")
    def load_history_once(self, length=None):
        if self._history_loaded:
            self.logger.log_debug("⚡ Tautulli history already loaded into memory.")
            return self._history_cache

        self._history_cache = self.get_history(length)
        self._history_loaded = True
        return self._history_cache

    @LoggerManager().log_function_entry
    @timeit("get_history")
    def get_history(self, length=None):
        from scripts.support.config.cache_keys import CacheKeyPaths
        cache_key = CacheKeyPaths.tautulli.GET_HISTORY
        length = length or self.default_length

        result = self.cache.get_or_generate_cache(
            cache_key,
            lambda: self._fetch_and_cache_history(length),
            instance="default"
        )

        if not result:
            self.logger.log_warning("⚠️ Tautulli get_history() returned None.")
            return {"data": []}

        # ✅ Nested unwrap support
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return {"data": data}
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                return {"data": data["data"]}

        self.logger.log_warning("⚠️ Unrecognized format in Tautulli get_history() response.")
        return {"data": []}

    @LoggerManager().log_function_entry
    @timeit("_fetch_and_cache_history")
    def _fetch_and_cache_history(self, length):
        """
        Fetches playback history from Tautulli API using pagination.

        Args:
            length (int): Total number of history entries to retrieve.

        Returns:
            list[dict]: Combined watch history entries.
        """
        if not self.api_tautulli:
            self.logger.log_error("❌ No Tautulli API client found.")
            return []

        all_results = []
        per_page = 500
        start = 0
        command = "get_history"

        self.logger.log_info(f"🚀 Fetching up to {length} Tautulli watch history entries (page size: {per_page})...")

        while len(all_results) < length:
            remaining = length - len(all_results)
            page_size = min(per_page, remaining)

            params = {
                "length": page_size,
                "start": start,
                "order_column": "date",
                "order_dir": "desc"
            }

            try:
                chunk = self.api_tautulli._make_request(command, params)

                self.logger.log_debug(f"📐 chunk type: {type(chunk)}")

                if not isinstance(chunk, list):
                    self.logger.log_warning(
                        f"⚠️ Unexpected format for chunk at start={start}: {type(chunk)}. Skipping.")
                    break

                if not chunk:
                    self.logger.log_info(f"📭 No more history returned at start={start}.")
                    break

                all_results.extend(chunk)
                self.logger.log_info(f"📄 Page fetched: {len(chunk)} entries (total: {len(all_results)})")

                if len(chunk) < page_size:
                    self.logger.log_info("✅ Final page reached.")
                    break

                start += page_size

            except Exception as e:
                self.logger.log_error(f"❌ Exception during Tautulli history fetch at start={start}: {e}")
                break

        self.logger.log_info(f"📺 Tautulli history fetch complete — {len(all_results)} entries retrieved.")
        self.logger.log_debug(f"🧪 Returning {len(all_results)} records from history (wrapped in dict)")
        return {
            "data": all_results
        }

    @LoggerManager().log_function_entry
    @timeit("_fetch_series_chunked")
    def _fetch_series_chunked(self, instance, chunk_size=500, max_workers=6):
        """
        Fetches all Sonarr movies in parallel chunks if full fetch times out.

        Args:
            instance (str): Sonarr instance name.
            chunk_size (int): Max items per chunk (default: 500).
            max_workers (int): Number of threads (default: 6).

        Returns:
            list[dict]: Combined movies data.
        """
        self.logger.log_warning(f"🧵 Using parallel chunked fallback for {instance}")
        try:
            api_key = self.sonarr_instances[instance].get("api", "MISSING_API_KEY")
            base_url = self.sonarr_instances[instance]["base_url"]
            url = f"{base_url}/api/v3/movies"
            headers = {"X-Api-Key": api_key}
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            full_list = response.json()

            total = len(full_list)
            pages = math.ceil(total / chunk_size)
            self.logger.log_info(f"🔄 Splitting {total} movies into {pages} chunks")

            results = []

            @LoggerManager().log_function_entry
            @timeit("process_chunk")
            def process_chunk(start_index):
                chunk = full_list[start_index:start_index + chunk_size]
                self.logger.log_debug(f"✅ Chunk [{start_index}:{start_index + chunk_size}] - {len(chunk)} items")
                return chunk

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_chunk, i * chunk_size) for i in range(pages)]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        results.extend(future.result())
                    except Exception as e:
                        self.logger.log_error(f"❌ Error during chunk fetch: {e}")

            self.logger.log_info(f"✅ Fallback chunked fetch complete: {len(results)} total movies")
            return results
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fallback chunk fetch for {instance}: {e}")
            return []

    @LoggerManager().log_function_entry
    @timeit("filter_history")
    def filter_history(self, user=None, series=None, start_date=None, end_date=None, resolution=None,
                       transcode_only=False):
        """Filters watch history based on provided criteria."""
        history = self.get_history()

        filtered = [
            entry for entry in history
            if (not user or entry.get("user") == user) and
               (not series or entry.get("grandparent_title") == series) and
               (not resolution or entry.get("video_resolution") == resolution) and
               (not transcode_only or entry.get("transcode_decision").lower() == "transcode") and
               (not start_date or entry.get("date") >= start_date) and
               (not end_date or entry.get("date") <= end_date)
        ]

        self.logger.log_info(f"🔍 Filtered watch history down to {len(filtered)} entries.")
        return filtered

    @LoggerManager().log_function_entry
    @timeit("analyze_top_series")
    def analyze_top_series(self, top_n=10):
        """Analyzes and returns the top N most-watched movies."""
        history = self.get_history()
        series_counter = Counter(entry.get("grandparent_title") for entry in history if entry.get("grandparent_title"))

        top_series = series_counter.most_common(top_n)
        self.logger.log_info(f"📊 Top {top_n} most-watched movies analyzed.")
        return top_series

    @LoggerManager().log_function_entry
    @timeit("analyze_most_active_users")
    def analyze_most_active_users(self, top_n=10):
        """Analyzes and returns the top N most active users."""
        history = self.get_history()
        user_counter = Counter(entry.get("user") for entry in history if entry.get("user"))

        top_users = user_counter.most_common(top_n)
        self.logger.log_info(f"📊 Top {top_n} most active users analyzed.")
        return top_users

    @LoggerManager().log_function_entry
    @timeit("backup_history")
    def backup_history(self):
        """Backs up current watch history to a timestamped file."""
        history = self.get_history()
        backup_path = f"history_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        with open(backup_path, "w") as file:
            json.dump(history, file, indent=4)

        self.logger.log_info(f"💾 Watch history successfully backed up to {backup_path}.")

    @LoggerManager().log_function_entry
    @timeit("fetch_with_retry")
    def fetch_with_retry(self, max_retries=3):
        """Fetches history with retry logic for resilience against API failures."""
        for attempt in range(max_retries):
            try:
                history = self.get_history()
                if history:
                    return history
            except Exception as e:
                self.logger.log_warning(f"⚠️ Attempt {attempt + 1} failed: {e}")
                time.sleep(2 ** attempt)

        self.logger.log_error("❌ Failed to fetch watch history after multiple retries.")
        return []

    @LoggerManager().log_function_entry
    @timeit("_get_timestamp")
    def _get_timestamp(self, entry):
        ts = entry.get("last_viewed_at") or entry.get("viewed_at") or entry.get("date")
        return int(ts) if ts else 0

    @LoggerManager().log_function_entry
    @timeit("get_last_watched_episode_per_user")
    def get_last_watched_episode_per_user(self, prefer_order=("tautulli", "plex", "trakt")):
        sources = {
            "tautulli": self.get_last_watched_from_tautulli(),
            "plex": self.get_last_watched_from_plex(),
            "trakt": self.get_last_watched_from_trakt()
        }

        combined = defaultdict(dict)
        timestamps = defaultdict(lambda: defaultdict(int))

        for source in prefer_order:
            for user, shows in sources[source].items():
                for show, data in shows.items():
                    ts = data.get("last_viewed")
                    if ts > timestamps[user][show]:
                        combined[user][show] = {"season": data["season"], "episode": data["episode"]}
                        timestamps[user][show] = ts

        self.logger.log_info("✅ Unified watch history compiled from Tautulli, Plex, and Trakt.")
        return combined

    @LoggerManager().log_function_entry
    @timeit("get_last_watched_from_tautulli")
    def get_last_watched_from_tautulli(self):
        self.logger.log_info("📡 Retrieving last watched episodes from Tautulli.")
        records = self.api_tautulli._make_request("get_history", {"length": 1000}) or []
        user_data = defaultdict(dict)

        for entry in records:
            if entry.get("media_type") != "episode":
                continue
            user = entry.get("user")
            show = entry.get("grandparent_title")
            season = int(entry.get("parent_media_index", 1))
            episode = int(entry.get("media_index", 1))
            ts = self._get_timestamp(entry)

            key = user_data[user].get(show)
            if not key or ts > key["last_viewed"]:
                user_data[user][show] = {"season": season, "episode": episode, "last_viewed": ts}

        return user_data

    @LoggerManager().log_function_entry
    @timeit("get_last_watched_from_plex")
    def get_last_watched_from_plex(self):
        self.logger.log_info("📡 Retrieving last watched episodes from Plex.")
        user_data = defaultdict(dict)
        try:
            base = f"http://{self.config['plex_ip']}:{self.config['plex_port']}"
            token = self.config['plex_token']
            headers = {"X-Plex-Token": token}

            # Fetch sections (libraries)
            sections = requests.get(f"{base}/library/sections", headers=headers).json()
            for section in sections["MediaContainer"]["Directory"]:
                section_id = section["key"]
                url = f"{base}/library/sections/{section_id}/all?type=4&viewCount>0"
                response = requests.get(url, headers=headers)
                items = response.json().get("MediaContainer", {}).get("Metadata", [])

                for item in items:
                    user = "PlexUser"  # Static fallback unless mapped from account
                    show = item.get("grandparentTitle")
                    season = int(item.get("parentIndex", 1))
                    episode = int(item.get("index", 1))
                    ts = int(item.get("lastViewedAt", 0))

                    key = user_data[user].get(show)
                    if not key or ts > key["last_viewed"]:
                        user_data[user][show] = {"season": season, "episode": episode, "last_viewed": ts}
        except Exception as e:
            self.logger.log_error(f"❌ Failed to retrieve Plex history: {e}")
        return user_data

    @LoggerManager().log_function_entry
    @timeit("get_last_watched_from_trakt")
    def get_last_watched_from_trakt(self):
        self.logger.log_info("📡 Retrieving last watched episodes from Trakt.")
        user_data = defaultdict(dict)
        try:
            token = self.config['trakt']['token']
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "trakt-api-key": self.config['trakt']['client_id'],
                "trakt-api-version": "2"
            }
            url = "https://api.trakt.tv/users/me/history/shows"
            page = 1

            while True:
                resp = requests.get(url, headers=headers, params={"page": page, "limit": 100})
                if resp.status_code != 200:
                    break

                data = resp.json()
                if not data:
                    break

                for item in data:
                    show = item["show"]["title"]
                    ep = item["episode"]
                    season = ep.get("season", 1)
                    episode = ep.get("number", 1)
                    ts = int(datetime.strptime(item["watched_at"], "%Y-%m-%dT%H:%M:%S.%fZ").timestamp())

                    key = user_data["TraktUser"].get(show)
                    if not key or ts > key["last_viewed"]:
                        user_data["TraktUser"][show] = {"season": season, "episode": episode, "last_viewed": ts}

                page += 1

        except Exception as e:
            self.logger.log_error(f"❌ Failed to retrieve Trakt history: {e}")
        return user_data

    @LoggerManager().log_function_entry
    @timeit("get_combined_history")
    def get_combined_history(self, source=None):
        """
        Returns merged watch history from all sources, grouped by TVDB ID.
        For each TVDB ID, keeps only the latest watched episode (based on timestamp).
        """
        combined = defaultdict(dict)

        @LoggerManager().log_function_entry
        @timeit("safe_extract")
        def safe_extract(entry):
            try:
                tvdb_id = int(entry.get("grandparent_rating_key") or entry.get("tvdb_id"))
                ts = int(entry.get("last_viewed_at") or entry.get("viewed_at") or entry.get("date", 0))
                season = int(entry.get("parent_media_index", 0))
                episode = int(entry.get("media_index", 0))
                return tvdb_id, ts, season, episode
            except Exception:
                return None, 0, 0, 0

        sources = [source] if source else ["tautulli", "trakt", "plex"]

        for source in sources:
            data = []
            try:
                if source == "tautulli" and self.api_tautulli:
                    response = self.api_tautulli._make_request("get_history", {"length": 1000})
                    data = response.get("data", []) if isinstance(response, dict) else []
                elif source == "trakt" and self.api_trakt and hasattr(self.api_trakt, 'api'):
                    data = self.api_trakt.api.get_watched_series()
                elif source == "plex" and self.plex:
                    data = self.plex.api.get_watched_series()
            except Exception as e:
                self.logger.log_error(f"❌ Failed to retrieve {source} watch history: {e}")
                continue

            for entry in data:
                tvdb_id, ts, season, episode = safe_extract(entry)
                if not tvdb_id:
                    continue
                existing = combined.get(tvdb_id, {})
                if not existing or ts > existing.get("last_viewed", 0):
                    combined[tvdb_id] = {
                        "season": season,
                        "episode": episode,
                        "last_viewed": ts,
                        "source": source
                    }

        self.logger.log_info(f"✅ Combined watch history generated for {len(combined)} TVDB IDs.")
        return combined

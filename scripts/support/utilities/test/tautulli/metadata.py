# manager_metadata.py
from scripts.managers.services.tautulli.api import TautulliAPI

from scripts.managers.factories.cache import GlobalCacheManager
from scripts.managers.factories.config import ConfigManager
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class MetadataManager:
    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger: LoggerManager, cache: GlobalCacheManager, api: TautulliAPI, sonarr_api):
        self.logger = logger
        self.cache = cache
        self.api = api

    @LoggerManager().log_function_entry
    @timeit("get_metadata")
    def get_metadata(self, rating_key, fallback_info=None):
        cache_key = f"tautulli_metadata_{rating_key}"
        instance = "default"

        @LoggerManager().log_function_entry
        @timeit("fetch_metadata_by_key")
        def fetch_metadata_by_key(rating_key):
            self.logger.log_info(f"⚡ Fetching metadata from Tautulli for rating_key: {rating_key}")
            response = self.api._make_request("get_metadata", {"rating_key": rating_key})
            return response.get("response", {}).get("data", {}) if response else {}

        @LoggerManager().log_function_entry
        @timeit("attempt_repair")
        def attempt_repair(metadata, rating_key):
            media_info = metadata.get("media_info", [{}])[0]
            file_path = media_info.get("file", "")
            resolution = media_info.get("video_full_resolution") or media_info.get("video_resolution")

            expected_instance = self._determine_expected_instance(file_path, resolution, metadata)
            actual_instance = self._get_actual_instance_by_path(file_path)

            if actual_instance and expected_instance and actual_instance != expected_instance:
                self.logger.log_warning(
                    f"⚠️ Instance mismatch: '{actual_instance}' vs expected '{expected_instance}' for {file_path}")
                from scripts.managers.services.sonarr.repair import repair_mismatched_instance
                repair_mismatched_instance(
                    logger=self.logger,
                    cache=self.cache,
                    rating_key=rating_key,
                    metadata=metadata,
                    expected_instance=expected_instance,
                    actual_instance=actual_instance
                )

        @LoggerManager().log_function_entry
        @timeit("generate_metadata")
        def generate_metadata():
            metadata = fetch_metadata_by_key(rating_key)

            if metadata:
                attempt_repair(metadata, rating_key)
                return metadata

            self.logger.log_warning(f"⚠️ No metadata found for rating_key: {rating_key}")

            if fallback_info:
                resolved_key = self._reverse_lookup_rating_key(fallback_info)
                if resolved_key and resolved_key != rating_key:
                    self.logger.log_info(f"🔄 Re-attempting metadata fetch using resolved rating_key: {resolved_key}")
                    return self.get_metadata(resolved_key)

            return {}

        metadata = self.cache.get_or_generate_cache(cache_key, generate_metadata, instance=instance)

        if metadata:
            self.logger.log_info(f"✅ Metadata loaded for rating_key: {rating_key}")

        return metadata

    @LoggerManager().log_function_entry
    @timeit("_reverse_lookup_rating_key")
    def _reverse_lookup_rating_key(self, fallback_info):
        title = fallback_info.get("grandparent_title")
        season = int(fallback_info.get("parent_media_index", 1))
        episode = int(fallback_info.get("media_index", 1))

        history = self.cache.get_or_generate_cache(
            CacheKeyPaths.tautulli.watch_history,
            generate_history,
            instance="default"
        )
        for entry in history:
            if (
                    entry.get("grandparent_title") == title and
                    int(entry.get("parent_media_index", -1)) == season and
                    int(entry.get("media_index", -1)) == episode
            ):
                return entry.get("rating_key")
        return None

    @LoggerManager().log_function_entry
    @timeit("extract_movie_metadata")
    def extract_movie_metadata(self, data):
        media_info = data.get("media_info", [{}])[0]

        return {
            "type": "movie",
            "title": data.get("title"),
            "year": data.get("year"),
            "summary": data.get("summary"),
            "actors": data.get("actors", []),
            "directors": data.get("directors", []),
            "writers": data.get("writers", []),
            "video_codec": media_info.get("video_codec"),
            "audio_codec": media_info.get("audio_codec"),
            "audio_channels": media_info.get("audio_channels"),
            "resolution": media_info.get("video_full_resolution"),
            "bitrate": media_info.get("bitrate"),
            "file_size": media_info.get("parts", [{}])[0].get("file_size"),
            "subtitles": [{
                "language": stream.get("subtitle_language"),
                "codec": stream.get("subtitle_codec"),
                "forced": bool(stream.get("subtitle_forced"))
            } for stream in media_info.get("parts", [{}])[0].get("streams", []) if stream.get("type") == "3"],
            "guid": data.get("guid"),
            "added_at": data.get("added_at"),
            "last_viewed_at": data.get("last_viewed_at")
        }

    @LoggerManager().log_function_entry
    @timeit("extract_series_metadata")
    def extract_series_metadata(self, data):
        return {
            "type": "movies",
            "title": data.get("title"),
            "year": data.get("year"),
            "summary": data.get("summary"),
            "studio": data.get("studio"),
            "actors": data.get("actors", []),
            "genres": data.get("genres", []),
            "collections": data.get("collections", []),
            "labels": data.get("labels", []),
            "guid": data.get("guid"),
            "added_at": data.get("added_at"),
            "last_viewed_at": data.get("last_viewed_at")
        }

    @LoggerManager().log_function_entry
    @timeit("extract_season_metadata")
    def extract_season_metadata(self, data):
        return {
            "type": "season",
            "title": data.get("title"),
            "series_title": data.get("parent_title"),
            "season_number": data.get("media_index"),
            "summary": data.get("summary"),
            "actors": data.get("actors", []),
            "genres": data.get("genres", []),
            "studio": data.get("studio"),
            "episodes_count": data.get("children_count"),
            "added_at": data.get("added_at"),
            "last_viewed_at": data.get("last_viewed_at"),
            "guid": data.get("guid"),
            "parent_guid": data.get("parent_guid")
        }

    @LoggerManager().log_function_entry
    @timeit("extract_episode_metadata")
    def extract_episode_metadata(self, data):
        media_info = data.get("media_info", [{}])[0]

        video_resolution = media_info.get("video_full_resolution") or media_info.get("video_resolution", "Unknown")

        return {
            "type": "episode",
            "title": data.get("title"),
            "movies": data.get("grandparent_title"),
            "season": data.get("parent_media_index"),
            "episode": data.get("media_index"),
            "summary": data.get("summary"),
            "studio": data.get("studio"),
            "genres": data.get("genres", []),
            "actors": data.get("actors", []),
            "duration": int(data.get("duration", 0)),
            "video_codec": media_info.get("video_codec"),
            "video_resolution": video_resolution,
            "audio_codec": media_info.get("audio_codec"),
            "audio_channels": media_info.get("audio_channels"),
            "bitrate": media_info.get("bitrate"),
            "file_size": media_info.get("parts", [{}])[0].get("file_size"),
            "subtitles": [{
                "language": stream.get("subtitle_language"),
                "codec": stream.get("subtitle_codec"),
                "forced": bool(stream.get("subtitle_forced"))
            } for stream in media_info.get("parts", [{}])[0].get("streams", []) if stream.get("type") == "3"],
            "added_at": data.get("added_at"),
            "last_viewed_at": data.get("last_viewed_at"),
            "guid": data.get("guid")
        }

    @LoggerManager().log_function_entry
    @timeit("_get_actual_instance_by_path")
    def _get_actual_instance_by_path(self, file_path):
        """
        Determines the actual Sonarr instance the file belongs to by matching its path
        against all root folders in each Sonarr instance.

        Args:
            file_path (str): The absolute file path to check.

        Returns:
            str | None: The matched Sonarr instance name, or None if no match found.
        """
        for instance in self.api.sonarr_apis:
            root_folders = self.api.get_root_folders(instance)
            for root in root_folders:
                root_path = root.get("path", "").rstrip("/")
                if file_path.startswith(root_path):
                    return instance
        return None

    @LoggerManager().log_function_entry
    @timeit("_determine_expected_instance")
    def _determine_expected_instance(self, file_path, resolution, raw_data):
        config = ConfigManager().get_config()
        anime_genres = config.get("animeGenres", [])

        is_anime = any(g.lower() in anime_genres for g in raw_data.get("genres", []))
        if "anime" in file_path and not is_anime:
            return "standard"

        if resolution is None:
            return None

        resolution = str(resolution)
        if "2160" in resolution:
            return "4k"
        elif "1080" in resolution:
            return "1080"
        else:
            return "720"

from datetime import datetime, timezone
import time

import requests

from scripts.managers.factories.base_manager import BaseManager


from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class RadarrHistoryCacheManager(BaseManager, ComponentManagerMixin):
    """
    Manages Radarr history-specific caches and incremental sync.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrCacheManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.manager          = parent
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def get_recent_history(self, instance):
        return self.global_cache.get(f"radarr.history.{instance}") or []

    def run_incremental_sync(self):
        all_instances = (self.config.get("radarr_instances") or {}).keys()
        for instance in all_instances:
            self.logger.log_info(f"🚀 Starting incremental sync for instance: {instance}")
            self.sync_from_history(instance)
        self.logger.log_info("🎉 Completed incremental sync across all Radarr instance")

    def sync_from_history(self, instance):
        instance_config = (self.config.get("radarr_instances") or {}).get(instance)
        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{instance}'")
            return

        cache_key = self.key_builder.format_cache_key("radarr/{instance}/library", instance=instance)
        cached_data = self.global_cache.get(cache_key) or {}
        cached_movies = cached_data.get("movies", {})
        cached_timestamp = (cached_data.get("meta") or {}).get("timestamp")

        if not cached_timestamp:
            self.logger.log_warning(f"⚠️ No timestamp found in cached movies for {instance}, regenerating full cache")
            self.manager.orchestration.run_movie_data_pull()
            return

        url = f"{instance_config['base_url']}/api/v3/history/since"
        params = {
            "date": cached_timestamp,
            "includeMovie": "true",
            "apikey": instance_config['api']
        }

        try:
            response = requests.get(url, params=params)
            self.logger.log_info(f"🌐 Full Radarr history request URL: {response.url}")
            response.raise_for_status()
            history_items = response.json()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch history since {cached_timestamp}: {e}")
            return

        valid_event_types = {"downloadFolderImported", "movieFileRenamed", "movieAdded"}
        new_movie_items = [
            item.get("movie") for item in history_items
            if item.get("eventType") in valid_event_types and item.get("movie")
        ]

        merged_movies, stats = self.global_cache.deduplicate_entries(
            cached_movies, new_movie_items, id_field="id"
        )

        updated_cache = {
            "movies": merged_movies,
            "meta": {"timestamp": datetime.now(timezone.utc).isoformat()}
        }
        self.global_cache.set(cache_key, updated_cache)

        self.logger.log_info(
            f"✅ Sync complete for {instance}: "
            f"{stats['total']} total updates — "
            f"{stats['new']} added, {stats['updated']} updated, {stats['skipped']} skipped"
        )

    def get_movie_watch_counts(self, instance):
        history = self.get_recent_history(instance)
        watch_counts = {}
        for item in history:
            mid = item.get("movieId")
            if mid:
                watch_counts[mid] = watch_counts.get(mid, 0) + 1
        return watch_counts

    def refresh_history(self, instance, days_back=30):
        history = self.radarr_api._make_request(instance, "history", fallback=[]) if self.radarr_api else []
        if history:
            self.global_cache.set(f"radarr.history.{instance}", history, compressed=True)
            self.logger.log_info(f"✅ Cached history for {instance} ({len(history)} entries)")
        else:
            self.logger.log_warning(f"⚠️ No history data retrieved for {instance}")

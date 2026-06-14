from datetime import datetime, timezone
import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin


class SonarrCacheHistoryManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr history-specific caches and incremental sync.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrCache"
        class_name = self.__class__.__name__

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = manager or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.key_builder = getattr(self.manager, "key_builder", None)

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    def refresh_history(self, instance, days_back=7):
        history = self.sonarr_api.get_history(instance, days_back=days_back)
        if history:
            key = f"sonarr/{instance}/history"
            self.sonarr_cache.set(key, {"meta": {}, "data": history})
            self.logger.log_info(f"✅ Refreshed history cache for {instance}")
        else:
            self.logger.log_warning(f"⚠️ No history data retrieved for {instance}")

    def get_recent_history(self, instance):
        key = f"sonarr/{instance}/history"
        return (self.sonarr_cache.get(key) or {}).get("data", [])

    def run_incremental_sync(self):
        all_instances = (self.config.get("sonarr_instances") or {}).keys()
        for instance in all_instances:
            self.logger.log_info(f"🚀 Starting incremental sync for instance: {instance}")
            self.sync_from_history(instance)
        self.logger.log_info("🎉 Completed incremental sync across all Sonarr instances")

    def sync_from_history(self, instance):
        instance_config = (self.config.get("sonarr_instances") or {}).get(instance)
        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{instance}'")
            return

        if not self.key_builder:
            self.logger.log_error("❌ Missing key_builder for cache key generation")
            return

        cache_key = self.key_builder.format_cache_key("sonarr", instance, "library")
        cached_data = self.global_cache.get(cache_key) or {}
        cached_series = cached_data.get("movies", {})
        cached_timestamp = (cached_data.get("meta") or {}).get("timestamp")

        if not cached_timestamp:
            self.logger.log_warning(f"⚠️ No timestamp in cache for {instance} — full reload triggered")
            self.manager.orchestration.run_series_data_pull()
            self.manager.orchestration.run_episode_data_pull()
            return

        url = f"{instance_config['base_url']}/api/v3/history/since"
        params = {
            "date": cached_timestamp,
            "includeSeries": "true",
            "includeEpisode": "true"
        }

        try:
            response = requests.get(url, params=params, headers={"X-Api-Key": instance_config["api"]})
            self.logger.log_info(f"🌐 Requesting incremental history: {str(response.url).split('?', 1)[0]}")
            response.raise_for_status()
            history_items = response.json()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch incremental history: {e}")
            return

        valid_event_types = {"downloadFolderImported", "seriesFolderImported", "episodeFileRenamed"}
        new_series_items = [
            item.get("movies") for item in history_items
            if item.get("eventType") in valid_event_types and item.get("movies")
        ]

        merged_series, stats = self.global_cache.deduplicate_entries(
            cached_series, new_series_items, id_field="id", instance=instance
        )

        updated_cache = {
            "movies": merged_series,
            "meta": {"timestamp": datetime.now(timezone.utc).isoformat()}
        }
        self.global_cache.set(cache_key, updated_cache)

        self.logger.log_info(
            f"✅ Sync complete for {instance}: "
            f"{stats['total']} total — {stats['new']} new, {stats['updated']} updated, {stats['skipped']} skipped"
        )

    def get_episode_watch_counts(self, instance):
        history = self.get_recent_history(instance)
        watch_counts = {}
        for item in history:
            eid = item.get("episodeId")
            if eid:
                watch_counts[eid] = watch_counts.get(eid, 0) + 1
        return watch_counts

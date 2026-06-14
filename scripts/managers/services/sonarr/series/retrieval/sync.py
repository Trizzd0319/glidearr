import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.registry import RegistryHelper


class SonarrSeriesRetrievalSyncManager(BaseManager, ComponentManagerMixin):
    """
    Syncs series metadata by fetching recent history and updating only those series that changed.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache support
        manager = kwargs.get("manager") or None
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        if not self.manager or not self.sonarr_api:
            self.logger.log_warning("⚠️ SonarrSeriesRetrievalSyncManager: manager or API not resolved — sync operations will be unavailable.")

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} with dual-cache")

    @LoggerManager().log_function_entry
    @timeit("sync_series_from_history")
    def sync_series_from_history(self, instance: str, timestamp: str) -> int:
        resolved_instance = self.manager.instance_manager.resolve_instance(instance)
        instance_config = (self.config.get("sonarr_instances") or {}).get(resolved_instance)

        if not instance_config:
            self.logger.log_error(f"❌ No configuration found for instance '{resolved_instance}'")
            return 0

        url = f"{instance_config['base_url']}/api/v3/history/since"
        params = {
            "date": timestamp,
            "includeSeries": "true",
            "includeEpisode": "true"
        }

        try:
            response = requests.get(url, params=params, headers={"X-Api-Key": instance_config['api']})
            response.raise_for_status()
            history_items = response.json()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch history since {timestamp}: {e}")
            return 0

        valid_event_types = {
            "downloadFolderImported",
            "seriesFolderImported",
            "episodeFileRenamed"
        }

        updated_series_ids = {
            item.get("seriesId")
            for item in history_items
            if item.get("eventType") in valid_event_types and item.get("seriesId")
        }

        self.logger.log_info(f"📦 {len(updated_series_ids)} series flagged for refresh based on history")

        updated_data = []
        for series_id in updated_series_ids:
            data = self.manager.series_fetch._fetch_series_by_id(resolved_instance, series_id)
            if data:
                updated_data.append(data)
                self.sonarr_cache.series.save_series_to_letter_file(resolved_instance, data)

        # ✅ Update global cache timestamp
        self.global_cache.update_timestamp(CacheKeyPaths.sonarr.SONARR_LIBRARY, instance=resolved_instance)

        self.logger.log_info(f"✅ Sync complete: {len(updated_data)} series updated from history in {resolved_instance}")
        return len(updated_data)

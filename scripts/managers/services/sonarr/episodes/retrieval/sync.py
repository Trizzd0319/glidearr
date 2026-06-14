import hashlib
import json
from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalSyncManager(BaseManager, ComponentManagerMixin):
    """
    Handles synchronization logic for Sonarr episodes, including timestamps, fingerprints, and sync drift detection.
    """
    parent_name = "SonarrEpisodesRetrieval"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.logger.log_debug(f"🔁 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_last_sync_timestamp")
    def get_last_sync_timestamp(self, instance_name):
        path = f"sonarr/{instance_name}/episodes/last_sync"
        return (self.global_cache.get(path) or {}).get("timestamp")

    @LoggerManager().log_function_entry
    @timeit("update_last_sync_timestamp")
    def update_last_sync_timestamp(self, instance_name):
        now = datetime.now(timezone.utc).isoformat()
        path = f"sonarr/{instance_name}/episodes/last_sync"
        self.global_cache.set(path, {"timestamp": now})
        self.logger.log_info(f"✅ Updated last sync timestamp for {instance_name} → {now}")

    @LoggerManager().log_function_entry
    @timeit("reset_all_sync_timestamps")
    def reset_all_sync_timestamps(self):
        for instance_name in self.config.get_sonarr_instances().keys():
            self.global_cache.delete(f"sonarr/{instance_name}/episodes/last_sync")
            self.logger.log_info(f"🗑️ Reset last sync timestamp for {instance_name}")

    @LoggerManager().log_function_entry
    @timeit("generate_episode_fingerprint")
    def generate_episode_fingerprint(self, episode: dict) -> str:
        """
        Generate a stable fingerprint hash for an episode's critical data.
        """
        core_fields = {
            "seriesId": episode.get("seriesId"),
            "seasonNumber": episode.get("seasonNumber"),
            "episodeNumber": episode.get("episodeNumber"),
            "title": episode.get("title"),
            "airDateUtc": episode.get("airDateUtc"),
            "hasFile": episode.get("hasFile"),
            "monitored": episode.get("monitored")
        }
        json_str = json.dumps(core_fields, sort_keys=True)
        return hashlib.md5(json_str.encode("utf-8")).hexdigest()

    @LoggerManager().log_function_entry
    @timeit("get_cached_episode_fingerprints")
    def get_cached_episode_fingerprints(self, instance_name):
        path = f"sonarr/{instance_name}/episodes/fingerprints"
        return self.sonarr_cache.get(path) or {}

    @LoggerManager().log_function_entry
    @timeit("set_cached_episode_fingerprints")
    def set_cached_episode_fingerprints(self, instance_name, fingerprint_map: dict):
        path = f"sonarr/{instance_name}/episodes/fingerprints"
        self.sonarr_cache.set(path, fingerprint_map)

    @LoggerManager().log_function_entry
    @timeit("detect_episode_drift")
    def detect_episode_drift(self, instance_name, episodes: list) -> list:
        """
        Compares current fingerprints to last-saved and returns list of drifted episodes.
        """
        cached = self.get_cached_episode_fingerprints(instance_name)
        drifted = []

        for ep in episodes:
            eid = ep.get("id")
            if not eid:
                continue
            current_fp = self.generate_episode_fingerprint(ep)
            last_fp = cached.get(str(eid))
            if last_fp != current_fp:
                drifted.append(ep)

        self.logger.log_info(f"🔎 {len(drifted)} episodes changed in {instance_name} out of {len(episodes)}")
        return drifted

    @LoggerManager().log_function_entry
    @timeit("update_episode_fingerprints")
    def update_episode_fingerprints(self, instance_name, episodes: list):
        """
        Stores the latest fingerprints for all episodes.
        """
        new_fingerprints = {
            str(ep["id"]): self.generate_episode_fingerprint(ep)
            for ep in episodes if "id" in ep
        }
        self.set_cached_episode_fingerprints(instance_name, new_fingerprints)
        self.logger.log_info(f"✅ Updated {len(new_fingerprints)} episode fingerprints for {instance_name}")

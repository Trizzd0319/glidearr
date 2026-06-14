from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairOrphansManager(BaseManager, ComponentManagerMixin):
    """
    Handles detection and cleanup of orphaned series and episodes from Sonarr's local cache.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("scan_orphaned_cache_entries")
    def scan_orphaned_cache_entries(self, instance_name=None):
        """
        Scans for orphaned series or episodes in the cache that no longer map to valid data.
        """
        self.logger.log_info("🧩 Scanning for orphaned cache entries...")
        orphaned = {"series": [], "episodes": []}

        lib_key = self.global_cache.format_cache_key(CacheKeyPaths.sonarr.LIBRARY, instance=instance_name)
        ep_key = self.global_cache.format_cache_key(CacheKeyPaths.sonarr.EPISODES, instance=instance_name)

        library = self.global_cache.get(lib_key) or {}
        episodes = self.global_cache.get(ep_key) or {}

        library_ids = {s.get("id") for s in library.get("series", []) if isinstance(s, dict)}
        episode_ids = set(episodes.keys())

        for eid in episode_ids:
            if eid not in library_ids:
                orphaned["episodes"].append(eid)

        for sid in library_ids:
            if not any(ep.get("seriesId") == sid for ep in episodes.values() if isinstance(ep, dict)):
                orphaned["series"].append(sid)

        self.logger.log_info(
            f"🧹 Found {len(orphaned['series'])} orphaned series and {len(orphaned['episodes'])} orphaned episodes."
        )
        return orphaned

    @LoggerManager().log_function_entry
    @timeit("purge_orphaned_cache")
    def purge_orphaned_cache(self, orphaned_data, instance_name=None, dry_run=False):
        """
        Purges orphaned cache entries from the series and episodes cache.
        """
        purged_series = 0
        purged_episodes = 0

        lib_key = self.global_cache.format_cache_key(CacheKeyPaths.sonarr.LIBRARY, instance=instance_name)
        ep_key = self.global_cache.format_cache_key(CacheKeyPaths.sonarr.EPISODES, instance=instance_name)

        library = self.global_cache.get(lib_key) or {}
        episodes = self.global_cache.get(ep_key) or {}

        if dry_run:
            self.logger.log_info("🚫 Dry run enabled — no orphaned entries will be deleted.")
            return {"dry_run": True, "purged_series": 0, "purged_episodes": 0}

        for sid in orphaned_data.get("series", []):
            before = len(library.get("series", []))
            library["series"] = [s for s in library.get("series", []) if s.get("id") != sid]
            if len(library["series"]) < before:
                purged_series += 1

        for eid in orphaned_data.get("episodes", []):
            if eid in episodes:
                del episodes[eid]
                purged_episodes += 1

        self.global_cache.set(lib_key, library)
        self.global_cache.set(ep_key, episodes)

        self.logger.log_debug(f"🔐 Updated cache key: {lib_key} (removed {purged_series} series)")
        self.logger.log_debug(f"🔐 Updated cache key: {ep_key} (removed {purged_episodes} episodes)")

        self.registry.set_flag("sonarr.repair.orphans.last_purge", str(datetime.now()))

        return {
            "purged_series": purged_series,
            "purged_episodes": purged_episodes,
            "keys_updated": [lib_key, ep_key]
        }

    @LoggerManager().log_function_entry
    @timeit("run_full_orphan_check")
    def run_full_orphan_check(self, instance_name=None, auto_purge=True, dry_run=False):
        """
        Orchestrates a full orphan scan and optionally purges the identified entries.
        """
        orphaned = self.scan_orphaned_cache_entries(instance_name=instance_name)
        purged = None

        if auto_purge:
            purged = self.purge_orphaned_cache(orphaned, instance_name=instance_name, dry_run=dry_run)

        return {
            "orphaned": orphaned,
            "purged": purged
        }

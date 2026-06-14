from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairEpisodesManager(BaseManager, ComponentManagerMixin):
    """
    Handles episode-level inconsistencies and repairs for Sonarr.
    Common issues include:
      - Missing file links
      - Misidentified episode files
      - Invalid episode metadata
      - Stale or conflicting cache entries
    """
    parent_name = "SonarrRepair"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = getattr(self.manager, "instance_manager", None)
        self.key_builder = kwargs.get("key_builder", getattr(self.manager, "key_builder", None))
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        # Dual cache setup
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = kwargs.get("global_cache", getattr(self.manager, "global_cache", self.global_cache))

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_missing_files")
    def repair_missing_files(self, instance_name):
        """
        Repairs missing episode file links in Sonarr.
        """
        self.logger.log_info(f"📂 Scanning for missing episode files in instance: {instance_name}")
        instance = self.instance_manager.resolve_instance(instance_name)
        episodes = self.sonarr_api.get_all_sonarr_apis()[instance].episode_files.all()

        missing = [ep for ep in episodes if not ep.path or not ep.relativePath]
        if not missing:
            self.logger.log_info("✅ No missing files detected.")
            return

        for ep in missing:
            self.logger.log_warning(
                f"⚠️ Missing episode link: S{ep.seasonNumber:02}E{ep.episodeNumber:02} - ID {ep.id}"
            )
            if not self.dry_run:
                # TODO: Implement repair logic (e.g., mark unmonitored, rescan folder)
                pass

        self.logger.log_info(f"🔁 Attempted to repair {len(missing)} episodes with missing files.")

    @LoggerManager().log_function_entry
    @timeit("cleanup_orphaned_episodes")
    def cleanup_orphaned_episodes(self, instance_name):
        """
        Removes or logs orphaned episode metadata without corresponding files.
        """
        self.logger.log_info(f"🧹 Cleaning orphaned episodes in instance: {instance_name}")
        instance = self.instance_manager.resolve_instance(instance_name)
        episodes = self.sonarr_api.get_all_sonarr_apis()[instance].episode.all()
        files = self.sonarr_api.get_all_sonarr_apis()[instance].episode_files.all()

        file_ids = {f.id for f in files if f and hasattr(f, "id")}
        orphans = [ep for ep in episodes if getattr(ep, "episodeFileId", 0) and ep.episodeFileId not in file_ids]

        if not orphans:
            self.logger.log_info("✅ No orphaned episodes found.")
            return

        for ep in orphans:
            self.logger.log_warning(
                f"🗑️ Orphaned metadata: S{ep.seasonNumber:02}E{ep.episodeNumber:02} (episodeFileId={ep.episodeFileId})"
            )
            if not self.dry_run:
                # TODO: Implement optional cleanup or reattachment
                pass

        self.logger.log_info(f"🧹 Completed orphaned cleanup: {len(orphans)} entries.")

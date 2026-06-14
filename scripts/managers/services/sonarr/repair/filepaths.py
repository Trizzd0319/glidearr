from pathlib import Path

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairFilepathsManager(BaseManager, ComponentManagerMixin):
    """
    Validates and optionally repairs invalid root folder mappings across Sonarr instances.
    Also detects missing or broken root folders and can trigger symlink cleanup.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.parent_name = "SonarrRepair"

        self.sonarr_api = kwargs.get("sonarr_api") or kwargs.get("api") or getattr(self.registry.get("manager", self.parent_name), "api", None)
        self.manager = kwargs.get("manager")
        self.dry_run = getattr(self.manager, "dry_run", False)
        self.auto_repair = getattr(self.manager, "auto_repair", False)
        self.rebuild_metadata = getattr(self.manager, "rebuild_metadata", False)

        if not self.sonarr_api:
            raise ValueError("❌ SonarrRepairPathsManager could not resolve a valid API interface.")

    @LoggerManager().log_function_entry
    @timeit("repair_root_folder_mappings")
    def repair_root_folder_mappings(self):
        """
        Verifies all configured root folders exist and match expected configuration.
        Optionally repairs broken or mismatched mappings.
        """
        self.logger.log_info("📁 Verifying root folder mappings...")
        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            root_folders = api.get_root_folders()
            for folder in root_folders:
                path = folder.path
                folder_id = folder.id
                self.logger.log_debug(f"🔎 Instance: {instance_name}, Folder: {path}")
                if not Path(path).exists():
                    self.logger.log_warning(f"⚠️ Missing root folder: {path}")
                    if self.auto_repair and not self.dry_run:
                        self.logger.log_info(f"🛠️ Attempting to remove broken folder {path}")
                        try:
                            api.delete_root_folder(folder_id)
                            self.logger.log_info(f"✅ Removed broken root folder: {path}")
                        except Exception as e:
                            self.logger.log_error(f"❌ Failed to remove root folder {path}: {e}")
                else:
                    self.logger.log_debug(f"✅ Root folder exists: {path}")

    @LoggerManager().log_function_entry
    @timeit("cleanup_orphaned_folders")
    def cleanup_orphaned_folders(self):
        """
        Scans library paths to detect Sonarr folders that no longer correspond to tracked series.
        """
        self.logger.log_info("🧹 Detecting orphaned series folders...")
        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            all_series = api.all_series()

            if not all_series:
                self.logger.log_error(
                    f"🛑 Aborting orphan folder cleanup for '{instance_name}': "
                    "all_series() returned empty — possible API failure. No folders will be deleted."
                )
                continue

            valid_paths = {Path(s.path).resolve() for s in all_series}
            root_folders = api.get_root_folders()

            for root in root_folders:
                root_path = Path(root.path)
                if not root_path.exists():
                    continue

                try:
                    for folder in root_path.iterdir():
                        if folder.is_dir() and folder.resolve() not in valid_paths:
                            self.logger.log_warning(f"🗑️ Orphaned folder found: {folder}")
                            if self.auto_repair and not self.dry_run:
                                try:
                                    for child in folder.iterdir():
                                        if child.is_file():
                                            child.unlink()
                                        elif child.is_dir():
                                            for f in child.glob("**/*"):
                                                if f.is_file():
                                                    f.unlink()
                                            child.rmdir()
                                    folder.rmdir()
                                    self.logger.log_info(f"✅ Removed orphaned folder: {folder}")
                                except Exception as e:
                                    self.logger.log_error(f"❌ Failed to delete {folder}: {e}")
                except Exception as e:
                    self.logger.log_error(f"❌ Could not inspect root {root_path}: {e}")

    @LoggerManager().log_function_entry
    @timeit("purge_orphaned_cache_keys")
    def purge_orphaned_cache_keys(self):
        """
        Purges cache entries that no longer match any known series by path or ID.
        """
        self.logger.log_info("🗂️ Purging orphaned cache entries...")

        for instance_name, api in self.sonarr_api.get_all_sonarr_apis().items():
            cache_key = self.global_cache.format_cache_key(CacheKeyPaths.sonarr.LIBRARY, instance=instance_name)
            cached = self.global_cache.get(cache_key)
            if not cached:
                continue

            live_series = api.all_series()
            live_ids = {s.id for s in live_series}
            valid_entries = [s for s in cached.get("series", []) if s.get("id") in live_ids]
            removed = len(cached.get("series", [])) - len(valid_entries)

            if removed > 0:
                self.logger.log_info(f"🧹 Removing {removed} orphaned entries from: {cache_key}")
                if not self.dry_run:
                    self.global_cache.set(cache_key, {"series": valid_entries})
            else:
                self.logger.log_debug(f"✅ No orphaned cache entries found for: {instance_name}")

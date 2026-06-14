from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSyncFoldersManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrStorage"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")


    @LoggerManager().log_function_entry
    @timeit("get_root_folders")
    def get_root_folders(self, instance):
        self.logger.log_info(f"📁 Fetching root folders from {instance}")
        return self.global_cache.get_or_generate_cache(
            key=Paths.sonarr.SPACE_ESTIMATES,
            generator_function=lambda: self.sonarr_api._make_request(instance, "rootfolder") or [],
        )

    @LoggerManager().log_function_entry
    @timeit("get_series_root_folder")
    def get_series_root_folder(self, series_path, instance):
        folders = self.get_root_folders(instance)
        for folder in folders:
            if series_path.startswith(folder["path"]):
                return folder["path"]
        return None

    @LoggerManager().log_function_entry
    @timeit("initialize_root_folders")
    def initialize_root_folders(self, instance, dry_run=False):
        """
        Ensures required root folders are created in the Sonarr instance.
        """
        self.logger.log_info(f"🔍 Syncing root folders for Sonarr instance: {instance}")
        self.global_cache.clear_cache(Paths.sonarr.SPACE_ESTIMATES, instance)

        # Get current folders
        current_folders = self.sonarr_api._make_request(instance, "rootfolder") or []
        current_paths = {f["path"].rstrip("/").lower() for f in current_folders}

        # Expected root folders from config
        expected_config = self.config.get("rootFolders", {})
        expected_paths = {
            name: f"{base_path.rstrip('/')}/{instance}".lower()
            for name, base_path in expected_config.items()
        }

        # Detect and create missing
        missing_folders = {
            name: path for name, path in expected_paths.items()
            if path not in current_paths
        }

        if not missing_folders:
            self.logger.log_info("✅ All required root folders already exist.")
            return

        for name, path in missing_folders.items():
            if dry_run:
                self.logger.log_info(f"[DRY-RUN] Would create missing folder: {path}")
                continue

            payload = {"path": path}
            result = self.sonarr_api._make_request(instance, "rootfolder", method="POST", payload=payload)
            status = "✅" if result else "❌"
            self.logger.log_info(f"{status} Created {name} root folder: {path}")

    @LoggerManager().log_function_entry
    @timeit("clear_cached_root_folders")
    def clear_cached_root_folders(self, instance):
        key = f"{Paths.sonarr.SPACE_ESTIMATES}.{instance}"
        self.global_cache.clear_cache(key)
        self.logger.log_info(f"🧹 Cleared cached root folders for {instance}")

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        instance = config.get_default_sonarr_instance_name()
        manager = SonarrSyncFoldersManager(logger=logger, config=config, global_cache=cache)
        cache.get_or_generate_cache(
            key=Paths.sonarr.SPACE_ESTIMATES,
            generator_function=manager.get_root_folders,
            expiration_time=300,
        )

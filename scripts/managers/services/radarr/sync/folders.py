from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrSyncFoldersManager(BaseManager, ComponentManagerMixin):
    """
    Manages and synchronises root folder configuration across Radarr instances.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrSyncManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.logger.log_debug(f"Initialized {self.__class__.__name__}")

    def _resolve_instance(self, instance):
        if self.instance_manager and hasattr(self.instance_manager, "resolve_instance"):
            return self.instance_manager.resolve_instance(instance)
        if self.radarr_api and hasattr(self.radarr_api, "resolve_instance"):
            return self.radarr_api.resolve_instance(instance)
        return instance or "default"

    @LoggerManager().log_function_entry
    @timeit("get_root_folders")
    def get_root_folders(self, instance: str) -> list:
        resolved = self._resolve_instance(instance)
        cached = self.global_cache.get(f"radarr.rootfolders.{resolved}", default=None)
        if cached is not None:
            return cached
        folders = self.radarr_api._make_request(resolved, "rootfolder", fallback=[]) or []
        self.global_cache.set(f"radarr.rootfolders.{resolved}", folders)
        return folders

    @LoggerManager().log_function_entry
    @timeit("get_movie_root_folder")
    def get_movie_root_folder(self, movie_path: str, instance: str):
        resolved = self._resolve_instance(instance)
        folders = self.get_root_folders(resolved)
        for folder in folders:
            if movie_path.startswith(folder["path"]):
                return folder["path"]
        return None

    @LoggerManager().log_function_entry
    @timeit("clear_cached_root_folders")
    def clear_cached_root_folders(self, instance: str):
        """Invalidate the root-folder cache for an instance."""
        resolved = self._resolve_instance(instance)
        self.global_cache.set(f"radarr.rootfolders.{resolved}", None)
        self.logger.log_info(f"Cleared cached root folders for {resolved}")

    @LoggerManager().log_function_entry
    @timeit("initialize_root_folders")
    def initialize_root_folders(self, instance: str):
        resolved = self._resolve_instance(instance)
        self.logger.log_info(f"Syncing root folders for Radarr instance: {resolved}")
        self.global_cache.set(f"radarr.rootfolders.{resolved}", None)  # invalidate

        current_folders = self.radarr_api._make_request(resolved, "rootfolder", fallback=[]) or []
        current_paths = {f["path"].rstrip("/").lower() for f in current_folders}

        expected_config = self.config.get("rootFolders", {})
        expected_paths = {
            name: f"{base_path.rstrip('/')}/{resolved}".lower()
            for name, base_path in expected_config.items()
        }

        missing_folders = {
            name: path for name, path in expected_paths.items()
            if path not in current_paths
        }

        if not missing_folders:
            self.logger.log_info("All required root folders already exist.")
            return

        for name, path in missing_folders.items():
            if self.dry_run:
                self.logger.log_info(f"[dry_run] Would create missing folder: {path}")
                continue

            payload = {"path": path}
            result = self.radarr_api._make_request(resolved, "rootfolder", method="POST", payload=payload)
            status = "Created" if result else "Failed to create"
            self.logger.log_info(f"{status} {name} root folder: {path}")

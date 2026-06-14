from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrSyncNamingManager(BaseManager, ComponentManagerMixin):
    """
    Synchronises movie file naming configuration across Radarr instances.
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

    def sanitize_naming_format(self, fmt: str) -> str:
        """Ensures consistent trimming while preserving all Radarr token formatting."""
        return fmt.strip() if fmt else fmt

    @LoggerManager().log_function_entry
    @timeit("get_naming_config")
    def get_naming_config(self, instance: str) -> dict:
        resolved = self._resolve_instance(instance)
        return self.radarr_api._make_request(resolved, "config/naming", fallback={}) or {}

    @LoggerManager().log_function_entry
    @timeit("sync_naming_settings")
    def sync_naming_settings(self, naming_config: dict):
        """Sync the given naming config to all configured Radarr instances."""
        fields_to_clean = ["standardMovieFormat", "movieFolderFormat"]
        config = naming_config.copy()

        for field in fields_to_clean:
            if field in config:
                original = config[field]
                config[field] = self.sanitize_naming_format(config[field])
                if config[field] != original:
                    self.logger.log_debug(f"Cleaned naming format field: {field}")

        instances = list((self.config.get("radarr_instances") or {}).keys())
        if not instances:
            self.logger.log_warning("No Radarr instances configured for naming sync.")
            return

        for instance in instances:
            resolved = self._resolve_instance(instance)
            if self.dry_run:
                self.logger.log_info(f"[dry_run] Would apply naming config to {resolved}")
                continue
            try:
                self.radarr_api._make_request(resolved, "config/naming", method="PUT", payload=config)
                self.logger.log_info(f"Synced naming config to {resolved}")
            except Exception as e:
                self.logger.log_error(f"Naming sync failed for {resolved}: {e}")

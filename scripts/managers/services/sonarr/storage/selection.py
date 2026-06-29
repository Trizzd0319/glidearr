from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrStorageSelectionManager(BaseManager, ComponentManagerMixin):
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
    @timeit("select_instance_by_free_space")
    def select_instance_by_free_space(self, required_gb: float = 5.0) -> str:
        from .space import SonarrStorageSpaceManager
        space_manager = SonarrStorageSpaceManager(
            logger=self.logger, config=self.config, global_cache=self.global_cache,
            validator=self.validator, registry=self.registry, sonarr_api=self.sonarr_api,
            manager=self.manager, dry_run=self.dry_run,
        )
        free_space = space_manager.get_free_space_per_instance()
        self.logger.log_debug(f"📦 Free space by instance: {free_space}")

        for instance, space in free_space.items():
            if space >= required_gb:
                self.logger.log_info(f"✅ Selected instance '{instance}' with {space:.2f} GB free.")
                return instance

        if free_space:
            fallback = max(free_space.items(), key=lambda x: x[1])[0]
            self.logger.log_warning(f"⚠️ No instance had enough space. Defaulting to '{fallback}'.")
            return fallback

        self.logger.log_error("❌ No Sonarr instance found during storage selection.")
        return self.manager.resolve_instance(None)

    @LoggerManager().log_function_entry
    @timeit("select_root_path_for_instance")
    def select_root_path_for_instance(self, instance: str) -> str:
        resolved_instance = self.manager.resolve_instance(instance)
        root_path = self.config.get_sonarr_instance_root(resolved_instance)

        if root_path:
            self.logger.log_info(f"📁 Using configured root path for {resolved_instance}: {root_path}")
            return root_path

        from .space import SonarrStorageSpaceManager
        space_manager = SonarrStorageSpaceManager(
            logger=self.logger, config=self.config, global_cache=self.global_cache,
            validator=self.validator, registry=self.registry, sonarr_api=self.sonarr_api,
            manager=self.manager, dry_run=self.dry_run,
        )
        folders = space_manager.get_root_folders(resolved_instance)
        if folders:
            fallback_path = folders[0].get("path", "/tv")
            self.logger.log_info(f"📁 Using fallback root path from scripts.sonarr: {fallback_path}")
            return fallback_path

        self.logger.log_warning(f"⚠️ No root path found for instance '{resolved_instance}'. Defaulting to '/tv'.")
        return "/tv"

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache):
        cache.get("sonarr/instance/mappings", default=None)
        logger.log_debug("📦 Warmed cache key: sonarr/instance/mappings")
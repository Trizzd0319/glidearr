from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSyncNamingManager(BaseManager, ComponentManagerMixin):
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

        self.dry_run = kwargs.get("dry_run", False)

    @LoggerManager().log_function_entry
    @timeit("sanitize_naming_format")
    def sanitize_naming_format(self, fmt):
        """Ensures consistent trimming but preserves all Sonarr token formatting."""
        if not fmt:
            return fmt
        # Only trim extra surrounding spaces, no regex manipulation
        return fmt.strip()

    @LoggerManager().log_function_entry
    @timeit("sync_naming_settings")
    def sync_naming_settings(self, naming_config):
        """
        Syncs the given naming config to all configured Sonarr instance.

        Args:
            naming_config (dict): The naming configuration from base/source instance.
        """
        fields_to_clean = ["standardEpisodeFormat", "dailyEpisodeFormat", "animeEpisodeFormat"]
        config = naming_config.copy()

        for field in fields_to_clean:
            if field in config:
                original = config[field]
                config[field] = self.sanitize_naming_format(config[field])
                if config[field] != original:
                    self.logger.log_debug(f"🧽 Cleaned naming format field: {field}")

        instances = self.config.get_sonarr_instances()
        if not instances:
            self.logger.log_warning("⚠️ No Sonarr instance configured for naming sync.")
            return

        for instance in instances:
            if self.dry_run:
                self.logger.log_info(f"[DRY-RUN] Would apply naming config to {instance}.")
                continue
            try:
                self.sonarr_api._make_request(instance, "config/naming", method="PUT", payload=config)
                self.logger.log_info(f"✅ Synced naming config to {instance}")
            except Exception as e:
                self.logger.log_error(f"❌ Naming sync failed for {instance}: {e}")

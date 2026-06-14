from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairValidatorManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 sonarr_api=None, instance_manager=None, sonarr_cache=None, dry_run=False, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = sonarr_api
        self.instance_manager = instance_manager
        self.sonarr_cache = sonarr_cache
        self.dry_run = dry_run

        if not self.sonarr_api or not self.instance_manager:
            raise ValueError("❌ Missing required API or instance manager for SonarrRepairValidatorManager")

        self.logger.log_debug(f"🧪 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("validate_series_integrity")
    def validate_series_integrity(self, instance_name):
        """
        Run a basic validation pass on all series in the specified instance.
        Looks for missing IDs, broken paths, and invalid season info.
        """
        resolved_instance = self.instance_manager.resolve_instance(instance_name)
        api = self.sonarr_api.get_all_sonarr_apis()[resolved_instance]
        series_list = api.all_series()

        for series in series_list:
            issues = []

            if not series.title or not series.id:
                issues.append("🆔 Missing ID or title")
            if not series.path:
                issues.append("📂 Missing path")
            if not series.seasons or not any(series.seasons):
                issues.append("📆 No valid season data")

            if issues:
                self.logger.log_warning(f"⚠️ Validation issue for '{series.title}' (ID {series.id}): " + " | ".join(issues))
            else:
                self.logger.log_debug(f"✅ Validated series: {series.title}")

    @LoggerManager().log_function_entry
    @timeit("validate_endpoint_health")
    def validate_endpoint_health(self, instance_name):
        """
        Perform a simple ping and core endpoint check to ensure Sonarr instance is responsive.
        """
        resolved_instance = self.instance_manager.resolve_instance(instance_name)
        api = self.sonarr_api.get_all_sonarr_apis()[resolved_instance]

        try:
            system_status = api.get_system_status()
            disk_space = api.get_disk_space()
            tags = api.get_tags()

            if not system_status:
                self.logger.log_warning(f"⚠️ Missing system status response from instance: {resolved_instance}")
            else:
                self.logger.log_debug(f"💡 System Version: {system_status.version}")

            if not isinstance(disk_space, list) or not disk_space:
                self.logger.log_warning(f"⚠️ Disk space response empty for instance: {resolved_instance}")

            if not isinstance(tags, list):
                self.logger.log_warning(f"⚠️ Tag list failed to load for instance: {resolved_instance}")

            self.logger.log_info(f"✅ Completed endpoint health check for {resolved_instance}")
        except Exception as e:
            self.logger.log_error(f"❌ Failed endpoint check for {resolved_instance}: {e}")

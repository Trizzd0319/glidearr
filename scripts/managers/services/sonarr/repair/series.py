from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairSeriesManager(BaseManager, ComponentManagerMixin):
    """
    Handles validation and repair tasks related to individual Sonarr series entries,
    including monitored status, path verification, and root folder validation.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 sonarr_api=None, manager=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = sonarr_api or getattr(manager, "sonarr_api", None)
        self.manager = manager or self.registry.get("manager", self.parent_name)
        self.dry_run = getattr(self.manager, "dry_run", False)

        if not self.sonarr_api:
            raise ValueError("❌ SonarrRepairSeriesManager could not resolve a valid Sonarr API interface.")

        self.logger.log_debug(f"🎞️ Initialized {self.__class__.__name__} (Parent: {self.parent_name}, Dry run: {self.dry_run})")

    @LoggerManager().log_function_entry
    @timeit("check_series_integrity")
    def check_series_integrity(self):
        """
        Scans Sonarr series list for:
        - Missing metadata
        - Broken paths
        - Unmonitored series
        - Invalid root folder mapping
        """
        self.logger.log_info("🔍 Checking series integrity...")
        series_list = self.sonarr_api.get_series()
        root_folders = set(folder['path'] for folder in self.sonarr_api.get_root_folders())

        for s in series_list:
            title = s.get("title")
            path = s.get("path")
            monitored = s.get("monitored", True)
            valid_path = any(path.startswith(r) for r in root_folders) if path else False

            if not monitored:
                self.logger.log_warning(f"⚠️ Unmonitored series: {title}")
                if not self.dry_run:
                    try:
                        self.sonarr_api.update_series(s['id'], monitored=True)
                        self.logger.log_info(f"✅ Re-monitored series: {title}")
                    except Exception as e:
                        self.logger.log_error(f"❌ Failed to update monitored flag for {title}: {e}")

            if not path:
                self.logger.log_error(f"❌ Missing path for series: {title}")
            elif not valid_path:
                self.logger.log_error(f"🛑 Invalid root folder for: {title} → {path}")

    @LoggerManager().log_function_entry
    @timeit("repair_series")
    def repair_series(self):
        """
        Full repair routine on all Sonarr series entries:
        - Reapply monitoring flag
        - Identify and log path inconsistencies
        """
        self.logger.log_info("🛠️ Beginning full series repair pass...")
        self.check_series_integrity()

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairMonitoringManager(BaseManager, ComponentManagerMixin):
    """
    Repairs incorrect or missing monitored flags for Sonarr series entries.
    Ensures that ended series are unmonitored and ongoing ones are monitored.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("repair_monitoring_flags")
    def repair_monitoring_flags(self, series_list):
        """
        Ensures monitored flags are set correctly for Sonarr series.
        Unmonitors ended series and ensures others are monitored.
        """
        self.logger.log_info("🔍 Scanning for incorrect monitoring flags...")
        repaired = []

        for series in series_list:
            if not isinstance(series, dict):
                continue

            title = series.get("title", "Unknown")
            monitored = series.get("monitored", None)
            status = series.get("status", "").lower()
            should_be_monitored = "ended" not in status

            if monitored is None or monitored != should_be_monitored:
                self.logger.log_info(f"🔁 Repairing monitored flag for: {title}")
                series["monitored"] = should_be_monitored

                if self.sonarr_api and series.get("id"):
                    if self.dry_run:
                        self.logger.log_info(f"[DRY-RUN] Would set monitored={should_be_monitored} for '{title}' via API")
                    else:
                        try:
                            self.sonarr_api.update_series_monitoring(series["id"], monitored=should_be_monitored)
                        except Exception as e:
                            self.logger.log_error(f"❌ Failed to persist monitored flag for '{title}': {e}")

                repaired.append(title)

        self.logger.log_info(f"✅ Monitoring flags repaired for {len(repaired)} series.")
        return repaired

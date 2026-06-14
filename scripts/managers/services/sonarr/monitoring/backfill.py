from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.config.cache_keys import CacheKeyPaths


class SonarrMonitoringBackfillManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrMonitoring"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("backfill_monitoring_status")
    def backfill_monitoring_status(self):
        if not self.sonarr_api:
            self.logger.log_warning("⚠️ No API reference found for backfill.")
            return

        all_instances = list(self.sonarr_api.get_all_sonarr_apis().items())

        for instance_name, arrapi_client in all_instances:
            try:
                all_series = arrapi_client.all_series()
                monitored = [s.id for s in all_series if s.monitored]
                unmonitored = [s.id for s in all_series if not s.monitored]

                if not self.sonarr_cache:
                    raise ValueError("❌ sonarr_cache is not defined in SonarrMonitoringBackfillManager")

                cache_key = self.sonarr_cache.format_cache_key(
                    CacheKeyPaths.sonarr.MONITORED_SYNC, instance=instance_name
                )

                updated_cache = {
                    "monitoredSeries": monitored,
                    "unmonitoredSeries": unmonitored,
                    "meta": {
                        "timestamp": datetime.utcnow().isoformat(),
                        "instance": instance_name,
                        "monitoredCount": len(monitored),
                        "unmonitoredCount": len(unmonitored)
                    }
                }

                self.sonarr_cache.set_with_pretty_output(cache_key, updated_cache)
                self.logger.log_info(
                    f"📦 Backfilled monitoring cache for {instance_name}: {len(monitored)} monitored, {len(unmonitored)} unmonitored"
                )
            except Exception as e:
                self.logger.log_warning(f"❌ Failed to backfill monitoring data for {instance_name}: {e}")

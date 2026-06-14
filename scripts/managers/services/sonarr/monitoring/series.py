from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrMonitoringSeriesManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrMonitoring"
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

        # Dual-cache support
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.key_builder = kwargs.get("key_builder") or getattr(self.manager, "key_builder", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    # ─────────────────────────────────────────────
    # 🔍 MONITORING DATA INSPECTION
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("get_series_with_monitoring_status")
    def get_series_with_monitoring_status(self, instance: str) -> tuple[list, list]:
        """Return two lists of monitored and unmonitored series."""
        all_series = self.sonarr_api.get_all_series(instance)
        monitored = [s for s in all_series if s.get("monitored")]
        unmonitored = [s for s in all_series if not s.get("monitored")]
        return monitored, unmonitored

    # ─────────────────────────────────────────────
    # 🔧 MONITORING CONTROL METHODS
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("monitor_or_unmonitor_series")
    def monitor_or_unmonitor_series(self, series_id: int, instance: str, monitored: bool = True) -> bool:
        """Update monitoring status for a single series."""
        data = self.sonarr_api._make_request(instance, f"series/{series_id}")
        if not data:
            self.logger.log_warning(f"⚠️ Unable to fetch series ID {series_id} for monitoring update.")
            return False

        data["monitored"] = monitored
        response = self.sonarr_api._make_request(instance, f"series/{series_id}", method="PUT", payload=data)

        if response:
            self.logger.log_info(f"{'✅' if monitored else '🚫'} Series {series_id} in {instance} is now {'monitored' if monitored else 'unmonitored'}.")
            return True
        else:
            self.logger.log_warning(f"❌ Failed to update monitoring for series {series_id} in {instance}.")
            return False

    @LoggerManager().log_function_entry
    @timeit("bulk_update_monitoring_status")
    def bulk_update_monitoring_status(self, instance: str, ids_to_monitor: list[int], ids_to_unmonitor: list[int]):
        """Bulk update monitoring status for two sets of IDs."""
        results = {"monitored": [], "unmonitored": [], "failed": []}

        for sid in ids_to_monitor:
            if self.monitor_or_unmonitor_series(sid, instance, monitored=True):
                results["monitored"].append(sid)
            else:
                results["failed"].append(sid)

        for sid in ids_to_unmonitor:
            if self.monitor_or_unmonitor_series(sid, instance, monitored=False):
                results["unmonitored"].append(sid)
            else:
                results["failed"].append(sid)

        self.logger.log_info(f"📊 Monitoring bulk update: {results}")
        return results

    # ─────────────────────────────────────────────
    # 💾 CACHE REFLECTION
    # ─────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_monitoring_data_pull")
    def run_monitoring_data_pull(self, instance):
        """Fetch and cache the current monitoring states from Sonarr."""
        arrapi_client = self.sonarr_api.get_api(instance)
        all_series = arrapi_client.all_series()

        monitored = [s for s in all_series if s.monitored]
        unmonitored = [s for s in all_series if not s.monitored]

        if not self.sonarr_cache:
            raise ValueError("❌ sonarr_cache is not defined in SonarrMonitoringSeriesManager")

        cache_key = self.sonarr_cache.format_cache_key(
            CacheKeyPaths.sonarr.MONITORED_SYNC, instance=instance
        )

        updated_cache = {
            "monitoredSeries": [s.id for s in monitored],
            "unmonitoredSeries": [s.id for s in unmonitored],
            "meta": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instance": instance,
                "monitoredCount": len(monitored),
                "unmonitoredCount": len(unmonitored)
            }
        }

        self.sonarr_cache.set_with_pretty_output(cache_key, updated_cache)
        self.logger.log_info(
            f"✅ Monitoring summary for {instance}: "
            f"{len(monitored)} monitored, {len(unmonitored)} unmonitored"
        )

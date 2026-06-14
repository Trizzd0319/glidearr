from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrMonitoringSpaceThresholdsManager(BaseManager, ComponentManagerMixin):
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

        self.logger.log_debug(f"📦 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def get_thresholds_for_instance(self, instance: str) -> dict:
        return (self.config.get("sonarr_thresholds") or {}).get(instance, {"critical": 5, "warning": 10})

    def classify_threshold(self, percent_free: float, instance: str = None) -> str:
        thresholds = self.get_thresholds_for_instance(instance or "default")
        critical = thresholds.get("critical", 5)
        warning = thresholds.get("warning", 10)

        if percent_free < critical:
            return "critical"
        elif percent_free < warning:
            return "warning"
        return "ok"

    @LoggerManager().log_function_entry
    @timeit("evaluate_all_instance_thresholds")
    def evaluate_all_instance_thresholds(self):
        evaluations = {}
        all_instances = self.sonarr_api.get_all_sonarr_apis()

        for instance, _ in all_instances.items():
            percent_free = self.sonarr_cache.get(f"sonarr/{instance}/storage/free_percent") or 100
            severity = self.classify_threshold(percent_free, instance)
            evaluations[instance] = {
                "percentFree": percent_free,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "instance": instance,
            }
            self.logger.log_info(f"📦 Instance {instance} free space: {percent_free:.2f}% → {severity.upper()}")

        return evaluations

    @LoggerManager().log_function_entry
    @timeit("store_threshold_evaluations")
    def store_threshold_evaluations(self, evaluations: dict):
        for instance, data in evaluations.items():
            cache_key = self.sonarr_cache.format_cache_key("sonarr/storage/thresholds", instance=instance)
            self.sonarr_cache.set(cache_key, data)
            self.logger.log_debug(f"📝 Stored threshold evaluation for {instance}: {data}")

    @LoggerManager().log_function_entry
    @timeit("run_threshold_audit")
    def run_threshold_audit(self):
        results = self.evaluate_all_instance_thresholds()
        self.store_threshold_evaluations(results)
        self.logger.log_info(f"✅ Completed Sonarr storage threshold audit across {len(results)} instances.")
        return results

    def get_critical_instances(self):
        data = self.evaluate_all_instance_thresholds()
        return [k for k, v in data.items() if v["severity"] == "critical"]

    def get_warning_instances(self):
        data = self.evaluate_all_instance_thresholds()
        return [k for k, v in data.items() if v["severity"] == "warning"]

    def get_ok_instances(self):
        data = self.evaluate_all_instance_thresholds()
        return [k for k, v in data.items() if v["severity"] == "ok"]

    def has_critical_thresholds(self):
        return bool(self.get_critical_instances())

    def has_warning_thresholds(self):
        return bool(self.get_warning_instances())

    def get_all_evaluation_data(self):
        return self.evaluate_all_instance_thresholds()

    def summarize_status(self):
        results = self.evaluate_all_instance_thresholds()
        summary = {
            "critical": len([i for i in results.values() if i["severity"] == "critical"]),
            "warning": len([i for i in results.values() if i["severity"] == "warning"]),
            "ok": len([i for i in results.values() if i["severity"] == "ok"])
        }
        self.logger.log_info(f"📊 Threshold Summary: {summary}")
        return summary

import json
import time

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache import make_json_safe
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrMonitoringSchedulerManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrMonitoring"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)

        self.logger = self.logger or getattr(parent, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.sonarr_cache = kwargs.get("cache_manager") or getattr(parent, "sonarr_cache", None)
        self.global_cache = kwargs.get("global_cache") or getattr(parent, "global_cache", None)

        self.tag_monitor = self.get_tag_monitor()

        if not self.logger:
            raise ValueError(f"❌ {self.__class__.__name__} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("schedule_monitoring_jobs")
    def schedule_monitoring_jobs(self):
        self.logger.log_info("📅 Scheduling monitoring jobs...")
        self.run_scheduled_checks()

    @LoggerManager().log_function_entry
    @timeit("run_scheduled_checks")
    def run_scheduled_checks(self, max_retries=3):
        self.logger.log_info("🔄 Running scheduled monitoring checks with retry logic...")
        attempts = 0
        success = False

        while attempts < max_retries and not success:
            try:
                summary = self.log_monitoring_status()
                self.log_execution_summary(summary)
                success = True
                self.logger.log_info("✅ Scheduled checks completed successfully.")
            except Exception as e:
                attempts += 1
                self.logger.log_warning(f"⚠️ Scheduled check attempt {attempts} failed: {e}")
                time.sleep(2 ** attempts)

        if not success:
            self.logger.log_error("❌ All scheduled check attempts failed. Triggering notification...")
            self.send_failure_notification()

    @LoggerManager().log_function_entry
    @timeit("log_monitoring_status")
    def log_monitoring_status(self):
        series_list = self.sonarr_api.get_all_series()
        monitored, unmonitored, anomalies = 0, 0, []

        for series in series_list:
            sid = series.get("id")
            title = series.get("title", "Unknown Title")

            if self.tag_monitor and self.tag_monitor.is_series_tagged_keep(sid):
                self.logger.log_debug(f"🔒 Skipping {title} (tagged 'keep')")
                continue

            if series.get("monitored"):
                monitored += 1
            else:
                unmonitored += 1
                if series.get("status") == "continuing":
                    anomalies.append(title)

        summary = {
            "monitored_count": monitored,
            "unmonitored_count": unmonitored,
            "anomalies": anomalies
        }

        self.logger.log_info(f"📊 Summary: {monitored} monitored, {unmonitored} unmonitored series.")
        if anomalies:
            self.logger.log_warning(f"⚠️ Unmonitored continuing series: {', '.join(anomalies)}")
        else:
            self.logger.log_info("✅ No anomalies detected in monitoring state.")
        return summary

    @LoggerManager().log_function_entry
    @timeit("log_execution_summary")
    def log_execution_summary(self, summary):
        summary_path = self.config.get("scheduler_summary_log_file", "scheduler_summary.json")
        try:
            with open(summary_path, "w") as f:
                json.dump(make_json_safe(summary), f, indent=2)
            self.logger.log_info(f"💾 Monitoring summary saved to {summary_path}")
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to write monitoring summary: {e}")

        if self.config.get("enable_metrics_push", False):
            self.push_metrics_to_monitoring_system(summary)

    def push_metrics_to_monitoring_system(self, summary):
        self.logger.log_info("📡 Pushing metrics to monitoring system (placeholder)")
        # Real integration (e.g., Prometheus push gateway) could go here

    def send_failure_notification(self):
        target = self.config.get("scheduler_failure_webhook")
        if target:
            self.logger.log_info(f"📨 Notifying failure via webhook: {target}")
            # Placeholder: integrate `requests.post` for real webhook
        else:
            self.logger.log_warning("⚠️ No failure webhook configured.")

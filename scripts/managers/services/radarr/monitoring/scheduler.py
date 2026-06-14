import json
import time

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.cache import make_json_safe
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrMonitoringSchedulerManager(BaseManager, ComponentManagerMixin):
    """
    Schedules and executes periodic monitoring checks for Radarr.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "RadarrMonitoringManager"
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

    def schedule_monitoring_jobs(self):
        self.logger.log_info("Scheduling movie monitoring jobs...")
        self.run_scheduled_checks()

    def run_scheduled_checks(self, instance: str = "default", max_retries: int = 3):
        self.logger.log_info("Running scheduled movie monitoring checks with retry logic...")
        resolved = self._resolve_instance(instance)
        attempts = 0
        success = False

        while attempts < max_retries and not success:
            try:
                summary = self.log_monitoring_status(resolved)
                self.log_execution_summary(summary)
                success = True
                self.logger.log_info("Scheduled movie checks completed successfully.")
            except Exception as e:
                attempts += 1
                self.logger.log_warning(f"Movie check attempt {attempts} failed: {e}")
                time.sleep(2 ** attempts)

        if not success:
            self.logger.log_error("All movie check attempts failed. Triggering notification...")
            self.send_failure_notification()

    def log_monitoring_status(self, instance: str) -> dict:
        resolved = self._resolve_instance(instance)
        if self.radarr_api is None:
            self.logger.log_warning("radarr_api not available — cannot fetch movies")
            return {}

        movies = self.radarr_api._make_request(resolved, "movie", fallback=[]) or []
        monitored_count = 0
        unmonitored_count = 0
        anomalies = []
        never_unmonitor = self.config.get("never_unmonitor_tags", [])

        for movie in movies:
            title = movie.get("title", "Unknown Title")
            tags = movie.get("tags", [])
            if any(tag in never_unmonitor for tag in tags):
                self.logger.log_debug(f"Skipping '{title}' (tagged keep)")
                continue

            if movie.get("monitored"):
                monitored_count += 1
            else:
                unmonitored_count += 1

            if movie.get("hasFile") and not movie.get("monitored") and not movie.get("qualityCutoffNotMet", True):
                anomalies.append(title)

        summary = {
            "monitored_count": monitored_count,
            "unmonitored_count": unmonitored_count,
            "anomalies": anomalies,
        }

        self.logger.log_info(f"Movie Summary: {monitored_count} monitored, {unmonitored_count} unmonitored.")
        if anomalies:
            self.logger.log_warning(f"Anomalies detected: downloaded but unmonitored → {', '.join(anomalies)}")
        else:
            self.logger.log_info("No anomalies detected in movie monitoring state.")

        return summary

    def log_execution_summary(self, summary: dict):
        summary_log_file = self.config.get("scheduler_summary_log_file", "scheduler_summary.json")
        try:
            with open(summary_log_file, "w") as f:
                json.dump(make_json_safe(summary), f, indent=2)
            self.logger.log_info(f"Movie execution summary written to {summary_log_file}")
        except Exception as e:
            self.logger.log_warning(f"Failed to write movie summary to file: {e}")

        if self.config.get("enable_metrics_push", False):
            self.push_metrics_to_monitoring_system(summary)

    def push_metrics_to_monitoring_system(self, summary: dict):
        self.logger.log_info("Pushing movie metrics to external monitoring system (placeholder)...")

    def send_failure_notification(self):
        notification_target = self.config.get("scheduler_failure_webhook", None)
        if notification_target:
            self.logger.log_info(f"Sending movie scheduler failure notification to: {notification_target}")
        else:
            self.logger.log_warning("No webhook configured for scheduler failure notifications.")

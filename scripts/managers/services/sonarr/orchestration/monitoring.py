from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationMonitoringManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrOrchestration"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.monitoring = kwargs.get("monitoring") or getattr(self.manager, "monitoring", None)

        if not self.monitoring:
            self.active = False
            self._inactive_reason = (
                "SonarrMonitoringManager unavailable — "
                "monitoring orchestration disabled."
            )
            return
        self.active = True

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("run_full_monitoring_audit")
    def run_full_monitoring_audit(self):
        return self.monitoring.audit.run_full_audit()

    @LoggerManager().log_function_entry
    @timeit("schedule_priority_tasks")
    def schedule_priority_tasks(self):
        return self.monitoring.priority_queue.schedule_priority_queue()

    @LoggerManager().log_function_entry
    @timeit("run_space_threshold_audit")
    def run_space_threshold_audit(self):
        return self.monitoring.space_thresholds.run_threshold_audit()

    @LoggerManager().log_function_entry
    @timeit("run_rule_sync")
    def run_rule_sync(self):
        return self.monitoring.rules.apply_rules()

    @LoggerManager().log_function_entry
    @timeit("run_backfill_routine")
    def run_backfill_routine(self):
        return self.monitoring.backfill.backfill_all()

    @LoggerManager().log_function_entry
    @timeit("update_episode_monitoring")
    def update_episode_monitoring(self):
        return self.monitoring.episodes.adjust_monitoring_by_episode_views()

    @LoggerManager().log_function_entry
    @timeit("update_series_monitoring")
    def update_series_monitoring(self):
        return self.monitoring.series.run_monitoring_data_pull(instance=None)

    def run_full_monitoring_audit(self):
        """Run complete storage threshold + rules + priority queue audit."""
        self.monitoring.space_thresholds.run_threshold_audit()
        self.monitoring.rules.evaluate_monitoring_rules()
        self.monitoring.priority_queue.run_priority_queue_logic()

    def run_monitoring_scheduler_update(self):
        """Rebuild monitoring schedule for all instances."""
        self.monitoring.scheduler.rebuild_priority_schedule()

    def run_monitoring_episode_backfill(self):
        """Trigger monitored episodes backfill (recent missing)."""
        self.monitoring.backfill.run_episode_backfill()

    def run_monitoring_enforce_space_pressure(self):
        """Unmonitor episodes or series under space pressure."""
        self.monitoring.rules.apply_space_pressure_rules()

    def run_monitoring_full_pipeline(self):
        """End-to-end run of all monitoring logic."""
        self.run_full_monitoring_audit()
        self.run_monitoring_scheduler_update()
        self.run_monitoring_episode_backfill()
        self.run_monitoring_enforce_space_pressure()
        self.logger.log_info("🎯 Completed full monitoring pipeline.")

    @LoggerManager().log_function_entry
    @timeit("run_all")
    def run_all(self):
        self.logger.log_info("🚀 Running full Sonarr monitoring orchestration pipeline...")
        self.run_space_threshold_audit()
        self.run_full_monitoring_audit()
        self.run_rule_sync()
        self.run_backfill_routine()
        self.update_episode_monitoring()
        self.update_series_monitoring()
        self.schedule_priority_tasks()
        self.logger.log_info("🎉 Monitoring orchestration complete.")

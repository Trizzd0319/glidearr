from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationRepairManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("SonarrOrchestrationRepairManager.__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, manager=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()
        self.manager = manager or self.registry.get("manager", self.parent_name)
        self.repair = getattr(self.manager, "repair", None)

        if not self.repair:
            self.active = False
            self._inactive_reason = (
                "SonarrRepairManager unavailable — "
                "repair orchestration disabled."
            )
            return
        self.active = True

        self.logger.log_debug(f"🔧 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def run_instance_repairs(self, **kwargs): self.repair.instance.run(**kwargs)
    def run_metadata_repairs(self, series_list): return self.repair.metadata.repair_missing_metadata(series_list)
    def run_filepaths_repairs(self): self.repair.filepaths.repair_root_folder_mappings(); self.repair.filepaths.cleanup_orphaned_folders(); self.repair.filepaths.purge_orphaned_cache_keys()
    def run_storage_repairs(self): self.repair.storage.run()
    def run_quality_repairs(self): self.repair.quality.repair_quality_definitions()
    def run_validator_checks(self, instance): self.repair.validator.validate_series_integrity(instance); self.repair.validator.validate_endpoint_health(instance)
    def run_tag_repairs(self): self.repair.tags.repair_ghost_tags(); self.repair.tags.deduplicate_tags()
    def run_series_repairs(self): self.repair.series.validate_series_fields(); self.repair.series.flag_unmonitored_series()
    def run_orphan_repairs(self): self.repair.orphans.remove_orphaned_series(); self.repair.orphans.cleanup_tagless_metadata()
    def run_file_repairs(self): self.repair.file.validate_episode_files()
    def run_cache_repairs(self): self.repair.repair_cache.purge_invalid_keys(); self.repair.repair_cache.refresh_all_entries()
    def run_anomaly_repairs(self): self.repair.anomaly.detect_unexpected_entries()
    def run_monitoring_repairs(self): self.repair.monitoring.sync_monitoring_flags()
    def run_history_repairs(self): self.repair.history.repair_missing_history()
    def run_episodes_repairs(self): self.repair.episodes.validate_episode_entries(); self.repair.episodes.fix_episode_status()

    def run_all_repairs(self, series_list=None, instance_name=None):
        self.logger.log_info("🧰 Running all repair operations...")
        self.run_instance_repairs()
        if instance_name:
            self.run_validator_checks(instance_name)
        self.run_filepaths_repairs()
        self.run_storage_repairs()
        self.run_quality_repairs()
        self.run_series_repairs()
        self.run_tag_repairs()
        self.run_orphan_repairs()
        self.run_file_repairs()
        self.run_cache_repairs()
        self.run_anomaly_repairs()
        self.run_monitoring_repairs()
        self.run_history_repairs()
        self.run_episodes_repairs()
        if series_list:
            self.run_metadata_repairs(series_list)
        self.logger.log_info("✅ All repair operations completed.")

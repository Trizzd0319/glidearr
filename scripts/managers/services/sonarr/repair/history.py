# managers/services/sonarr/repair/history.py

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrRepairHistoryManager(BaseManager, ComponentManagerMixin):
    """
    Handles Sonarr history-related repairs, such as:
    - Rebuilding missing or corrupt history entries
    - Re-synchronizing historical plays with Plex/Tautulli
    - Detecting gaps, timestamp inconsistencies, or API faults
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrRepair"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = kwargs.get("sonarr_api") or kwargs.get("api") or getattr(self.registry.get("manager", self.parent_name), "api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.registry.get("manager", self.parent_name), "instance_manager", None)
        self.manager = kwargs.get("manager")
        self.dry_run = getattr(self.manager, "dry_run", False)

        if not self.sonarr_api or not self.instance_manager:
            raise ValueError("❌ SonarrRepairHistoryManager missing API or instance manager references.")

        self.logger.log_debug(f"🛠️ Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("reconcile_history_gaps")
    def reconcile_history_gaps(self, instance_name):
        """Detects and reports missing history entries by comparing live and cached data."""
        resolved = self.instance_manager.resolve_instance(instance_name)
        self.logger.log_info(f"📚 Reconciling history gaps for instance: {resolved}")

        live_history = self.sonarr_api.get_all_sonarr_apis()[resolved].history.all()
        cached_key = self.global_cache.format_cache_key("sonarr.history", instance=resolved)
        cached = self.global_cache.get(cached_key, fallback=[]) or []

        live_ids = {entry.id for entry in live_history}
        cached_ids = {entry.get("id") for entry in cached if isinstance(entry, dict)}
        missing = cached_ids - live_ids

        if missing:
            self.logger.log_warning(f"⚠️ {len(missing)} missing history entries found for instance {resolved}.")
            for entry_id in sorted(missing):
                self.logger.log_debug(f" - Missing entry ID: {entry_id}")
        else:
            self.logger.log_info(f"✅ No history gaps detected for instance {resolved}.")

    @LoggerManager().log_function_entry
    @timeit("rebuild_history_for_tvdb")
    def rebuild_history_for_tvdb(self, instance_name, tvdb_id):
        """Triggers a stubbed re-pull or synthetic history build for a specific TVDB ID."""
        resolved = self.instance_manager.resolve_instance(instance_name)
        self.logger.log_info(f"♻️ Rebuilding history for TVDB ID {tvdb_id} on instance {resolved}")

        # 🔧 Stub for future integration with Plex/Tautulli history or external metadata
        self.logger.log_info(f"📌 Stub: Rebuild logic not yet implemented for TVDB ID: {tvdb_id}")

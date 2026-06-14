# sonarr/series/sync/orchestration.py

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationSeriesSyncManager(BaseManager, ComponentManagerMixin):
    """
    Orchestrates full synchronization tasks for Sonarr Series,
    including recent view analysis, tag syncing, and library profile adjustments.
    """

    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        self.series_sync = getattr(self.manager, "sync", None)
        self.retrieval = getattr(self.manager, "retrieval", None)

        if not self.series_sync:
            raise ValueError("❌ SonarrSeriesSyncManager is not initialized in parent manager.")
        if not self.retrieval:
            raise ValueError("❌ SonarrSeriesRetrievalManager is not initialized in parent manager.")

        self.logger.log_debug("🧰 SonarrOrchestrationSeriesSyncManager initialized.")

    @LoggerManager().log_function_entry
    @timeit("run_full_sync")
    def run_full_sync(self, instance: str = None, use_tautulli: bool = False, dry_run: bool = None, force_all: bool = False):
        """
        Run the full series synchronization flow using either Tautulli or history-based detection.
        """
        dry_run = dry_run if dry_run is not None else self.dry_run
        self.logger.log_info(f"🚦 Triggering full sync orchestration (dry_run={dry_run})...")

        self.series_sync.composite_sync_workflow(
            instance=instance,
            use_tautulli=use_tautulli,
            dry_run=dry_run,
            force_all=force_all
        )

    @LoggerManager().log_function_entry
    @timeit("run_full_enrichment_and_sync")
    def run_full_enrichment_and_sync(self, instance: str = None, force_all: bool = False):
        """
        Enrich series data first, then run the composite sync.
        """
        self.logger.log_info("📦 Starting full enrichment before sync...")
        self.retrieval.enrich.run_enrichment(instance=instance)

        self.logger.log_info("🔄 Enrichment complete. Running sync...")
        self.run_full_sync(instance=instance, use_tautulli=True, force_all=force_all)

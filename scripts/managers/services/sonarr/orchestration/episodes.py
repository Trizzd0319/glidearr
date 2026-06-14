from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationEpisodesManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrOrchestration"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        # All direct access to episode manager and submodules
        episodes = kwargs.get("episodes") or getattr(self.manager, "episodes", None)
        self.retrieval = kwargs.get("retrieval") or getattr(episodes, "retrieval", None)
        self.history = kwargs.get("history") or getattr(episodes, "history", None)
        self.monitoring = kwargs.get("monitoring") or getattr(episodes, "monitoring", None)
        self.file = kwargs.get("file") or getattr(episodes, "file", None)
        self.sharding = kwargs.get("sharding") or getattr(episodes, "sharding", None)

        self.logger.log_debug("🧰 Initialized SonarrOrchestrationEpisodesManager")

    # ─────────────────────────────────────────────────────────────
    # 🔁 Orchestration Methods
    # ─────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("run_full_episode_retrieval")
    def run_full_episode_retrieval(self):
        """Run full warmup of episode cache using fallback-aware loader."""
        self.logger.log_info("🚀 Running full episode cache warmup...")
        self.retrieval.episode_cache.warm_all_episodes_cache()
        self.logger.log_info("🎉 Full episode cache warmup completed.")

    @LoggerManager().log_function_entry
    @timeit("run_recent_episode_check")
    def run_recent_episode_check(self, instance: str, hours: int = 24):
        """Use Sonarr history to fetch recently updated episode IDs."""
        recent_ids = self.retrieval.fetch.get_recent_episode_ids(instance, hours=hours)
        self.logger.log_info(f"🕒 Found {len(recent_ids)} recent episodes in past {hours} hours.")
        return recent_ids

    @LoggerManager().log_function_entry
    @timeit("run_missing_file_audit")
    def run_missing_file_audit(self, instance: str):
        """Identify missing or blacklisted episodes across instances."""
        all_episodes = self.retrieval.fetch._get_cached_episodes_by_instance(instance)
        missing = self.retrieval.validate.identify_missing_episode_files(all_episodes)
        self.logger.log_info(f"📭 Detected {len(missing)} missing/blacklisted episodes.")
        return missing

    @LoggerManager().log_function_entry
    @timeit("run_episode_shard_plan")
    def run_episode_shard_plan(self, shard_size=10):
        """Generate a global sharding plan for batched updates."""
        plan = self.sharding.generate_global_shard_plan(shard_size=shard_size)
        self.logger.log_info(f"📦 Episode shard plan generated: {len(plan)} instances")
        return plan

    @LoggerManager().log_function_entry
    @timeit("run_orphaned_file_check")
    def run_orphaned_file_check(self, instance: str):
        """Audit orphaned files with no associated metadata."""
        return self.file.find_orphaned_episode_files(instance)

    @LoggerManager().log_function_entry
    @timeit("run_episode_enrichment")
    def run_episode_enrichment(self, instance: str):
        """Trigger enrichment routines (e.g., TVDB, cross-link)."""
        self.retrieval.enrich.run_tvdb_crosslink(instance)
        self.logger.log_info(f"🔗 TVDB enrichment completed for {instance}")

    @LoggerManager().log_function_entry
    @timeit("run_monitoring_toggle")
    def run_monitoring_toggle(self, instance: str, episode_id: int, enable: bool):
        """Toggle monitoring state for a specific episode."""
        self.monitoring.toggle_episode_monitoring(instance, episode_id, enable)


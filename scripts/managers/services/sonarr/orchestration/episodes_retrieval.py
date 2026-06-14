from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrOrchestrationEpisodeRetrievalManager(BaseManager, ComponentManagerMixin):
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

        episodes = kwargs.get("episodes") or getattr(self.manager, "episodes", None)
        self.retrieval = kwargs.get("retrieval") or getattr(episodes, "retrieval", None)
        self.sharding = kwargs.get("sharding") or getattr(episodes, "sharding", None)

        if not self.retrieval or not self.sharding:
            self.active = False
            self._inactive_reason = (
                "Retrieval or Sharding managers unavailable — "
                "episode retrieval orchestration disabled."
            )
            return
        self.active = True

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("orchestrate_episode_retrieval")
    def orchestrate_episode_retrieval(self, instance, shard_size=10, limit=None):
        """Run episode retrieval in shard batches for a given instance."""
        shard_plan = self.sharding.generate_shard_plan(instance, shard_size=shard_size)
        all_results = {}

        for shard_index, series_ids in shard_plan.items():
            if limit and shard_index >= limit:
                break

            self.logger.log_info(f"🔄 Processing shard {shard_index} with {len(series_ids)} series")
            shard_results = {}
            for sid in series_ids:
                try:
                    eps = self.retrieval.fetch.fetch_episodes_for_series(sid, instance)
                    shard_results[sid] = eps
                    self.logger.log_debug(f"📺 Retrieved {len(eps)} episodes for series {sid}")
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Failed to retrieve episodes for series {sid}: {e}")
            all_results.update(shard_results)

        self.logger.log_info(f"✅ Completed retrieval for {len(all_results)} series in {instance}")
        return all_results

    @LoggerManager().log_function_entry
    @timeit("warm_all_episodes_cache")
    def warm_all_episodes_cache(self, shard_size=10):
        """Run a full multi-instance episode cache warmup."""
        instances = list(self.sonarr_api.get_all_sonarr_apis().keys())
        if not instances:
            self.logger.log_warning("⚠️ No Sonarr instances available for cache warmup.")
            return

        for instance in instances:
            self.logger.log_info(f"🔥 Starting cache warmup for instance: {instance}")
            result = self.orchestrate_episode_retrieval(instance, shard_size=shard_size)
            episode_count = sum(len(eplist) for eplist in result.values())
            self.logger.log_info(f"🧊 Cached {episode_count} episodes from {instance}")

# sonarr/episodes/sharding.py

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesShardingManager(BaseManager, ComponentManagerMixin):
    """
    Handles the assignment of series/episode IDs to shard groups for caching, updating, or batch operations.
    """

    parent_name = "SonarrEpisodes"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager") or self.registry.get("manager", self.parent_name)

        # Dual-cache pattern
        self.global_cache = global_cache or getattr(parent, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(parent, "sonarr_cache", None)

        # API and manager references
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.manager = parent

        if not self.logger:
            raise ValueError("❌ SonarrEpisodesShardingManager could not initialize without logger")

        self.logger.log_debug(f"🧩 Initialized SonarrEpisodesShardingManager (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("compute_shards")
    def compute_shards(self, series_ids, shard_size=10):
        """
        Split series IDs into even shards for batching.
        """
        shards = [series_ids[i:i + shard_size] for i in range(0, len(series_ids), shard_size)]
        self.logger.log_info(f"📦 Computed {len(shards)} shards from {len(series_ids)} series.")
        return shards

    @LoggerManager().log_function_entry
    @timeit("get_series_ids_by_instance")
    def get_series_ids_by_instance(self, instance):
        """
        Get all series IDs from an instance.
        """
        try:
            series = self.sonarr_api.get_series(instance)
            series_ids = [s["id"] for s in series if s.get("id")]
            self.logger.log_debug(f"🔍 Retrieved {len(series_ids)} series IDs from instance '{instance}'.")
            return series_ids
        except Exception as e:
            self.logger.log_error(f"❌ Failed to get series from instance '{instance}': {e}")
            return []

    @LoggerManager().log_function_entry
    @timeit("generate_shard_plan")
    def generate_shard_plan(self, instance, shard_size=10):
        """
        Return a shard plan dict: {shard_index: [series_ids]} for a specific instance.
        """
        series_ids = self.get_series_ids_by_instance(instance)
        shards = self.compute_shards(series_ids, shard_size=shard_size)
        shard_plan = {idx: shard for idx, shard in enumerate(shards)}
        self.logger.log_info(f"🧹 Generated shard plan for instance '{instance}' with {len(shard_plan)} shards.")
        return shard_plan

    @LoggerManager().log_function_entry
    @timeit("generate_global_shard_plan")
    def generate_global_shard_plan(self, shard_size=10):
        """
        Generates a full multi-instance shard plan with structure:
        {
            'instance_name': {
                0: [...],
                1: [...],
                ...
            },
            ...
        }
        """
        if not self.sonarr_api or not hasattr(self.sonarr_api, "get_all_sonarr_apis"):
            self.logger.log_error("❌ API reference missing or invalid. Cannot generate global shard plan.")
            return {}

        shard_plan = {}
        for instance_name in self.sonarr_api.get_all_sonarr_apis():
            shard_plan[instance_name] = self.generate_shard_plan(instance_name, shard_size)

        self.logger.log_info(f"🚀 Global shard plan created across {len(shard_plan)} instances.")
        return shard_plan

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalCacheManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodes"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)

        # Dual-cache setup
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = kwargs.get("cache_manager") or getattr(self.manager, "sonarr_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_all_episode_data")
    def get_all_episode_data(self, instance, force_refresh=False):
        resolved = self.instance_manager.resolve_instance(instance)
        cache_key = CacheKeyPaths.sonarr.ALL_EPISODES.replace("<instance>", resolved)

        if force_refresh:
            self.global_cache.delete(cache_key)

        return self.global_cache.get_or_generate_cache(
            key=cache_key,
            generator_function=lambda: self.sonarr_api._make_request(resolved, "episodefile") or [],
            expiration_time=86400,
        )

    @LoggerManager().log_function_entry
    @timeit("get_all_episode_profiles")
    def get_all_episode_profiles(self, instance, force_refresh=False):
        resolved = self.instance_manager.resolve_instance(instance)
        cache_key = CacheKeyPaths.sonarr.EPISODE_PROFILES.replace("<instance>", resolved)

        if force_refresh:
            self.global_cache.delete(cache_key)

        return self.global_cache.get_or_generate_cache(
            key=cache_key,
            generator_function=lambda: self.sonarr_api._make_request(resolved, "qualityProfile") or [],
            expiration_time=86400,
        )

    @LoggerManager().log_function_entry
    @timeit("warm_cache_for_instance")
    def warm_cache_for_instance(self, instance):
        self.logger.log_info(f"🔥 Warming episode profiles cache for instance: {instance}")
        # Note: Sonarr's /episodefile endpoint requires ?seriesId= — bulk episode
        # warming is not supported. Episode files are fetched lazily per-series
        # via get_episodes_by_series_id when actually needed.
        self.get_all_episode_profiles(instance)

    @LoggerManager().log_function_entry
    @timeit("warm_cache_for_all_instances")
    def warm_cache_for_all_instances(self):
        instances = self.config.get_sonarr_instances()
        for name, cfg in instances.items():
            if name == "default_instance" or not isinstance(cfg, dict):
                continue
            try:
                self.warm_cache_for_instance(name)
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to warm cache for instance '{name}': {e}")

    @LoggerManager().log_function_entry
    @timeit("get_episodes_by_series_id")
    def get_episodes_by_series_id(self, series_id, instance, force_refresh=False,
                                  log_miss=True, log_expired=True):
        resolved = self.instance_manager.resolve_instance(instance)
        cache_key = CacheKeyPaths.sonarr.EPISODES_BY_SERIES.replace("<instance>", resolved).replace("<series_id>", str(series_id))

        if force_refresh:
            self.global_cache.delete(cache_key)

        return self.global_cache.get_or_generate_cache(
            key=cache_key,
            generator_function=lambda: self.sonarr_api._make_request(resolved, f"episode?seriesId={series_id}") or [],
            expiration_time=86400,
            log_miss=log_miss, log_expired=log_expired,
        )

    @LoggerManager().log_function_entry
    @timeit("get_last_modified_timestamp")
    def get_last_modified_timestamp(self, instance):
        resolved = self.instance_manager.resolve_instance(instance)
        all_eps = self.get_all_episode_data(instance)
        if not all_eps:
            return None

        latest = max((ep.get("dateAdded") for ep in all_eps if ep.get("dateAdded")), default=None)
        return latest

    @LoggerManager().log_function_entry
    @timeit("get_episode_by_id")
    def get_episode_by_id(self, episode_id, instance, fallback_title=None):
        resolved = self.instance_manager.resolve_instance(instance)
        all_eps = self.get_all_episode_data(instance)
        for ep in all_eps:
            if ep.get("id") == episode_id:
                return ep
        self.logger.log_warning(f"⚠️ Episode ID {episode_id} not found in cache for {resolved}")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_episode_count_by_series")
    def get_episode_count_by_series(self, series_id, instance, log_miss=True, log_expired=True):
        eps = self.get_episodes_by_series_id(series_id, instance, log_miss=log_miss, log_expired=log_expired)
        return len(eps)

    @LoggerManager().log_function_entry
    @timeit("force_refresh_all_cache_for_instance")
    def force_refresh_all_cache_for_instance(self, instance):
        self.logger.log_info(f"🔁 Force-refreshing cache for: {instance}")
        self.get_all_episode_data(instance, force_refresh=True)
        self.get_all_episode_profiles(instance, force_refresh=True)

    @LoggerManager().log_function_entry
    @timeit("warm_all_episodes_cache")
    def warm_all_episodes_cache(self):
        """Warm episode cache across all configured instances. Called by the orchestration layer."""
        self.warm_cache_for_all_instances()

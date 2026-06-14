from importlib import import_module

from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class CacheWarmupManager:
    _instance = None
    _instance_count = 0

    def __new__(cls, logger, cache, config, registry, validator):
        if cls._instance is None:
            cls._instance = super(CacheWarmupManager, cls).__new__(cls)
            cls._instance._initialized = False
            cls._instance_count += 1
        else:
            print(f"⚠️ Duplicate CacheWarmupManager instantiation detected! Current count: {cls._instance_count}")
        return cls._instance

    @classmethod
    def get_instance_count(cls):
        return cls._instance_count

    def __init__(self, logger, cache, config, registry, validator):
        if self._initialized:
            return
        if self._initialized:
            return
        self.logger = logger
        self.cache = cache
        self.config = config
        self.registry = registry
        self.validator = validator
        self.modules = [
            "managers.services.sonarr.episodes.file",
            "managers.services.sonarr.episodes.monitoring",
            "managers.services.sonarr.episodes.retrieval",
            "managers.services.sonarr.movies.retrieval",
            "managers.services.sonarr.storage.library",
            "managers.services.sonarr.storage.selection",
            "managers.services.sonarr.storage.space",
            "managers.services.sonarr.sync.folders",
            "managers.services.sonarr.sync.media_management",
            "managers.services.sonarr.instance",
        ]
        self._initialized = True

    @LoggerManager().log_function_entry
    @timeit("register_all_instances")
    def register_all_instances(self):
        self.logger.log_info("📌 Registering service instance...")
        from scripts.managers.services.sonarr.instance import SonarrInstanceRegistrar
        registrar = SonarrInstanceRegistrar(
            logger=self.logger,
            config=self.config,
            cache=self.cache,
            validator=self.validator,
            registry=self.registry,
            sonarr_api=self.registry.get("manager", "SonarrManager")
        )
        registrar.register_all()

    @LoggerManager().log_function_entry
    @timeit("warm_sonarr_cache")
    def warm_sonarr_cache(self):
        self.logger.log_info("🧊 Warming Sonarr cache...")
        sonarr = self.registry.get("manager", "SonarrManager")
        if not sonarr:
            self.logger.log_warning("⚠️ SonarrManager not registered. Skipping Sonarr cache warmup.")
            return
        try:
            instances = sonarr.api.get_instances()
        except Exception as e:
            self.logger.log_error(f"❌ Failed to fetch Sonarr instance: {e}")
            return
        for instance in instances:
            try:
                sonarr.quality.get_episode_profiles(instance)
                sonarr.episodes.future.get_future_episodes(instance)
                sonarr.monitoring.get_monitored_episodes(instance)
                sonarr.retrieval.get_all_series_episodes(instance)
                sonarr.library.get_library_series(instance)
                sonarr.sync.get_monitored_state(instance)
                sonarr.space.get_free_space_per_instance()
                sonarr.metadata.get_metadata_for_all_series(instance)
                sonarr.folders.get_instance_path_mapping(instance)
                self.logger.log_debug(f"✅ Warmed Sonarr cache for instance: {instance}")
            except Exception as e:
                self.logger.log_warning(f"⚠️ Failed to warm Sonarr cache for {instance}: {e}")

    @LoggerManager().log_function_entry
    @timeit("run_all")
    def run_all(self):
        self.logger.log_info(f"🔥 Running all service cache warmers... (CWM instance: {self.get_instance_count()})")
        self.register_all_instances()
        for path in self.modules:
            module_name = path.split(".")[-1]
            flag_key = f"sonarr.{module_name}.warm_cache"
            if not self.registry.get_flag(flag_key):
                self.logger.log_info(f"🔍 Skipping {path} because no warm_cache flag is set in registry.")
                continue
            try:
                module = import_module(path)
                if hasattr(module, "warm_cache") and callable(module.warm_cache):
                    module.warm_cache(logger=self.logger, cache=self.cache, config=self.config)
                    self.logger.log_info(f"✅ Warmed cache via {path}")
                else:
                    self.logger.log_warning(f"⚠️ No warm_cache() found in {path}")
            except Exception as e:
                self.logger.log_error(f"❌ Failed warming {path}: {e}")

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, config):
        from scripts.support.config.cache_keys import CacheKeyPaths
        from scripts.managers.services.sonarr.quality import SonarrQualityManager
        instance = config.get_default_sonarr_instance_name()
        manager = SonarrQualityManager(logger=logger, config=config, global_cache=cache)
        cache.get_or_generate_cache(
            key=CacheKeyPaths.sonarr.EPISODE_PROFILES,
            generator_function=lambda: manager.get_episode_profiles(instance),
            expiration_time=86400,
            category="sonarr",
            instance=instance
        )

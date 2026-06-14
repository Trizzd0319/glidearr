from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.cache.history import RadarrHistoryCacheManager
from scripts.managers.services.radarr.cache.instances import RadarrInstanceCacheManager
from scripts.managers.services.radarr.cache.monitoring import RadarrMonitoringCacheManager
from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager
from scripts.managers.services.radarr.cache.orchestration import RadarrOrchestrationCacheManager
from scripts.managers.services.radarr.cache.quality import RadarrQualityCacheManager
from scripts.managers.services.radarr.cache.relational import RadarrCacheRelationalManager
from scripts.managers.services.radarr.cache.tags import RadarrTagCacheManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrCacheManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrCacheManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.load_summary = {}
        all_critical_loaded = True

        parent = kwargs.get("manager")
        radarr_api = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.dry_run = dry_run  # must be set before init_kwargs so child managers can read it via getattr(manager, 'dry_run')

        all_component_classes = {
            "history":      RadarrHistoryCacheManager,
            "instance":     RadarrInstanceCacheManager,
            "monitoring":   RadarrMonitoringCacheManager,
            "movie_files":  RadarrCacheMovieFilesManager,
            "orchestration": RadarrOrchestrationCacheManager,
            "quality":      RadarrQualityCacheManager,
            "relational":   RadarrCacheRelationalManager,
            "tags":         RadarrTagCacheManager,
        }

        critical_keys = set(all_component_classes)

        init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "radarr_api":       radarr_api,
            "instance_manager": instance_manager,
            "manager":          self,
            "dry_run":          dry_run,
        }

        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs,
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.cache.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.cache.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.cache.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.cache.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("radarr.cache_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Radarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.monitoring.history import RadarrMonitoringHistoryManager
from scripts.managers.services.radarr.monitoring.movies import RadarrMonitoringMoviesManager
from scripts.managers.services.radarr.monitoring.rules import RadarrMonitoringRulesManager
from scripts.managers.services.radarr.monitoring.scheduler import RadarrMonitoringSchedulerManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrMonitoringManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrMonitoringManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        self.dry_run          = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        self.load_summary = {}
        all_critical_loaded = True

        init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "radarr_api":       self.radarr_api,
            "instance_manager": self.instance_manager,
            "manager":          self,
            "dry_run":          self.dry_run,
        }

        all_component_classes = {
            "history":   RadarrMonitoringHistoryManager,
            "movies":    RadarrMonitoringMoviesManager,
            "rules":     RadarrMonitoringRulesManager,
            "scheduler": RadarrMonitoringSchedulerManager,
        }

        critical_keys = {"history", "movies", "rules", "scheduler"}

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
                self.registry.set_flag(f"radarr.monitoring.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.monitoring.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.monitoring.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.monitoring.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("radarr.monitoring_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Radarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

    def get_monitoring_summary(self, instance: str) -> tuple:
        """Return (monitored_list, unmonitored_list) for the given instance."""
        monitored   = self.movies.get_monitored_movies(instance)
        unmonitored = self.movies.get_unmonitored_movies(instance)
        return monitored, unmonitored

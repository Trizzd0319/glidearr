from datetime import datetime, timezone

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.sonarr.monitoring.audit import SonarrMonitoringAuditManager
from scripts.managers.services.sonarr.monitoring.episodes import SonarrMonitoringEpisodesManager
from scripts.managers.services.sonarr.monitoring.rules import SonarrMonitoringRulesManager
from scripts.managers.services.sonarr.monitoring.scheduler import SonarrMonitoringSchedulerManager
from scripts.managers.services.sonarr.monitoring.series import SonarrMonitoringSeriesManager
from scripts.managers.services.sonarr.monitoring.backfill import SonarrMonitoringBackfillManager
from scripts.managers.services.sonarr.monitoring.priority_queue import SonarrMonitoringPriorityQueueManager
from scripts.managers.services.sonarr.monitoring.space_thresholds import SonarrMonitoringSpaceThresholdsManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrMonitoringManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(
        self,
        logger=None,
        config=None,
        global_cache=None,
        validator=None,
        registry=None,
        sonarr_api=None,
        cache_manager=None,
        **kwargs
    ):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.sonarr_api = sonarr_api
        self.sonarr_cache = cache_manager or getattr(kwargs.get("manager", {}), "sonarr_cache", None)
        self.dry_run = kwargs.get("dry_run", False)

        # ✅ Shared init args
        init_kwargs = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": self.sonarr_api,
            "cache_manager": self.sonarr_cache,
            "dry_run": self.dry_run
        }

        # ✅ Component map
        component_map = {
            "episodes": SonarrMonitoringEpisodesManager,
            "rules": SonarrMonitoringRulesManager,
            "scheduler": SonarrMonitoringSchedulerManager,
            "series": SonarrMonitoringSeriesManager,
            "audit": SonarrMonitoringAuditManager,
            "backfill": SonarrMonitoringBackfillManager,
            "priority_queue": SonarrMonitoringPriorityQueueManager,
            "space_thresholds": SonarrMonitoringSpaceThresholdsManager
        }

        critical_keys = set(component_map.keys())

        # ✅ Split components into critical / non-critical
        critical_components, noncritical_components = split_components(
            all_components=component_map,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_kwargs
        )

        # ✅ Load critical components
        self.load_summary = {}
        all_critical_loaded = True

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.monitoring.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.monitoring.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.monitoring.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.monitoring.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.monitoring_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )

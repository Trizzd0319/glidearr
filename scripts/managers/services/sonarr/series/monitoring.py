from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesMonitoringManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🔗 Inherit from parent or fallback to kwargs
        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.orchestration = kwargs.get("orchestration") or getattr(self.manager, "orchestration", None)

        # 🔧 Dual cache injection
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(self.manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)

        # 🔌 API & instance manager
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        if not self.logger:
            raise ValueError("❌ SonarrSeriesMonitoringManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized SonarrSeriesMonitoringManager (Parent: {self.parent_name})")

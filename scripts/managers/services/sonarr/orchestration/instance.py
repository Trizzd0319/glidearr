from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class SonarrOrchestrationInstanceManager(BaseManager, ComponentManagerMixin):
    """
    Manages Sonarr instance-level orchestration, including validation, repair, audit, and summaries.
    """

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual cache setup
        manager = kwargs.get("manager") or {}
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.register()

        # 🔗 Resolve parent context
        parent = manager or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.manager = parent or getattr(self, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))
        self.instance_manager = getattr(self.manager, "instance_manager", None)

        if not self.logger:
            raise ValueError(f"❌ {self.parent_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def run_instance_diagnostics(self):
        """
        Runs a full diagnostic summary for all Sonarr instances.
        """
        if not self.instance_manager:
            self.logger.log_warning("⚠️ No instance manager attached to orchestration layer.")
            return

        apis = self.instance_manager.get_all_sonarr_apis()
        summaries = {}

        for name, api in apis.items():
            try:
                sys_info = api.system_status()
                disk_space = api.disk_space()
                queues = api.queue()

                summaries[name] = {
                    "version": getattr(sys_info, "version", "unknown"),
                    "disk_free_gb": sum(d.freeSpace / 1e9 for d in disk_space if hasattr(d, "freeSpace")),
                    "queue_size": len(queues),
                }

                self.logger.log_info(f"📊 Instance Summary [{name}]: {summaries[name]}")

            except Exception as e:
                self.logger.log_error(f"❌ Failed diagnostics for {name}: {e}")
                summaries[name] = {"error": str(e)}

        return summaries

    def validate_all_instances(self):
        """
        Re-validates Sonarr APIs for all instances.
        """
        if not self.instance_manager:
            self.logger.log_error("❌ Instance manager not available.")
            return

        all_results = {}
        for name in self.instance_manager.get_all_instance_names():
            api = self.instance_manager.get_sonarr_api(name)
            try:
                api.system_status()
                all_results[name] = "✅ OK"
                self.logger.log_info(f"✅ {name} API validation succeeded.")
            except Exception as e:
                all_results[name] = f"❌ Failed: {e}"
                self.logger.log_warning(f"⚠️ {name} API validation failed.")

        return all_results

    def summarize_all_instances(self):
        """
        Provides quick metadata for all configured instances (name, version, health).
        """
        if not self.instance_manager:
            self.logger.log_error("❌ Instance manager not available.")
            return {}

        summaries = {}
        for name in self.instance_manager.get_all_instance_names():
            summary = self.instance_manager.summarize_instance(name)
            summaries[name] = summary

        return summaries

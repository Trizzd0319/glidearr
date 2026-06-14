from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager
from scripts.managers.services.radarr.repair.interface import RadarrRepairInterfaceManager
from scripts.managers.services.radarr.repair.manager import RadarrRepairManager
from scripts.managers.services.radarr.repair.metadata import RadarrRepairMetadataManager
from scripts.managers.services.radarr.repair.orphans import RadarrRepairOrphansManager
from scripts.managers.services.radarr.repair.quality import RadarrRepairQualityManager
from scripts.managers.services.radarr.repair.storage import RadarrRepairStorageManager
from scripts.managers.services.radarr.repair.tags import RadarrRepairTagsManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrRepairWrapperManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrRepairWrapperManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = __class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.load_summary = {}
        all_critical_loaded = True

        parent = kwargs.get("manager")
        self.radarr_api       = kwargs.get("radarr_api") or getattr(parent, "radarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(parent, "instance_manager", None)
        dry_run               = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        init_kwargs = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.global_cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "radarr_api":       self.radarr_api,
            "instance_manager": self.instance_manager,
            "manager":          self,
            "dry_run":          dry_run,
        }

        all_component_classes = {
            "anomaly":   RadarrRepairAnomalyManager,
            "interface": RadarrRepairInterfaceManager,
            "manager":   RadarrRepairManager,
            "metadata":  RadarrRepairMetadataManager,
            "orphans":   RadarrRepairOrphansManager,
            "quality":   RadarrRepairQualityManager,
            "storage":   RadarrRepairStorageManager,
            "tags":      RadarrRepairTagsManager,
        }

        critical_keys = {"interface", "manager", "anomaly", "metadata", "quality", "tags", "orphans", "storage"}

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
                self.registry.set_flag(f"radarr.repair.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.repair.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_kwargs)
                setattr(self, name, instance)
                self.registry.set_flag(f"radarr.repair.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"radarr.repair.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("radarr.repair_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Radarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded,
        )

    def _all_instances(self) -> list[str]:
        if self.instance_manager and hasattr(self.instance_manager, "get_all_radarr_apis"):
            try:
                return list(self.instance_manager.get_all_radarr_apis().keys())
            except Exception:
                pass
        if self.radarr_api and hasattr(self.radarr_api, "get_all_radarr_apis"):
            try:
                return list(self.radarr_api.get_all_radarr_apis().keys())
            except Exception:
                pass
        return []

    @LoggerManager().log_function_entry
    @timeit("run_all_repairs")
    def run(self, instance: str | None = None) -> dict:
        """Run all repair sub-managers across all instances (or a specific one)."""
        instances = [instance] if instance else self._all_instances()
        if not instances:
            self.logger.log_debug("[Repair] No instances configured — skipping repair run")
            return {}

        all_results: dict = {}
        for inst in instances:
            results: dict = {}
            for name in ("anomaly", "metadata", "quality", "tags", "orphans", "storage"):
                component = getattr(self, name, None)
                if component and hasattr(component, "run"):
                    try:
                        results[name] = component.run(inst)
                    except Exception as e:
                        self.logger.log_warning(f"[Repair] {name}.run() failed for '{inst}': {e}")
                        results[name] = {"error": str(e)}
            all_results[inst] = results
        return all_results

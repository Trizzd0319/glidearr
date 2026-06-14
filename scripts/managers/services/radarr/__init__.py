from typing import Optional

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.radarr.cache import RadarrCacheManager
from scripts.managers.services.radarr.instance import RadarrInstanceManager
from scripts.managers.services.radarr.monitoring import RadarrMonitoringManager
from scripts.managers.services.radarr.movies import RadarrMoviesManager
from scripts.managers.services.radarr.orchestration import RadarrOrchestrationManager
from scripts.managers.services.radarr.quality import RadarrQualityManager
from scripts.managers.services.radarr.repair import RadarrRepairWrapperManager
from scripts.managers.services.radarr.storage import RadarrStorageManager
from scripts.managers.services.radarr.sync import RadarrSyncManager
from scripts.managers.services.radarr.validator import RadarrValidatorManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class RadarrManager(BaseManager, ComponentManagerMixin):
    parent_name = "RadarrManager"

    instance_manager: Optional[RadarrInstanceManager] = None
    radarr_cache: Optional[RadarrCacheManager] = None
    key_builder: Optional[CacheKeyBuilder] = None

    validator_manager: Optional[RadarrValidatorManager] = None
    movies: Optional[RadarrMoviesManager] = None
    quality: Optional[RadarrQualityManager] = None
    monitoring: Optional[RadarrMonitoringManager] = None
    sync: Optional[RadarrSyncManager] = None
    storage: Optional[RadarrStorageManager] = None
    repair: Optional[RadarrRepairWrapperManager] = None
    orchestration: Optional[RadarrOrchestrationManager] = None

    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger=logger, config=config, global_cache=global_cache, validator=validator, registry=registry, **kwargs)
        self.cache = self.global_cache  # alias used in init_args below
        self.register()

        self.dry_run     = kwargs.get("dry_run", False)
        self.load_summary = {}
        self.key_builder  = CacheKeyBuilder()

        # --- Step 1: Define dependency graph ---
        self.component_dependencies = {
            "instance_manager": [],
            "radarr_cache":     ["instance_manager"],
            "storage":          ["radarr_cache", "instance_manager"],
            "movies":           ["radarr_cache", "instance_manager"],
            "quality":          ["movies", "instance_manager"],
            "monitoring":       ["movies", "instance_manager"],
            "sync":             ["instance_manager"],
            "repair":           ["movies", "storage", "instance_manager"],
            "orchestration":    ["movies", "storage", "quality", "monitoring"],
        }
        enabled_keys = set(self.component_dependencies.keys())

        # --- Step 2: Initialize instance manager (always first) ---
        self.instance_manager = RadarrInstanceManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            manager=self,
        )
        # radarr_api is an alias for instance_manager (like Sonarr pattern)
        self.radarr_api = self.instance_manager

        # --- Step 3: Initialize radarr_cache ---
        self.radarr_cache = RadarrCacheManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.cache,
            validator=self.validator,
            registry=self.registry,
            radarr_api=self.radarr_api,
            instance_manager=self.instance_manager,
            dry_run=self.dry_run,
        )

        # --- Step 4: Common kwargs for all downstream components ---
        self.init_args = {
            "logger":           self.logger,
            "config":           self.config,
            "global_cache":     self.cache,
            "validator":        self.validator,
            "registry":         self.registry,
            "dry_run":          self.dry_run,
            "key_builder":      self.key_builder,
            "radarr_api":       self.radarr_api,
            "instance_manager": self.instance_manager,
            "cache_manager":    self.radarr_cache,
            "parent_name":      self.parent_name,
            "manager":          self,
        }

        # --- Step 5: Component class mapping (filtered by enabled keys) ---
        full_components = {
            "validator_manager": RadarrValidatorManager,
            "movies":            RadarrMoviesManager,
            "monitoring":        RadarrMonitoringManager,
            "quality":           RadarrQualityManager,
            "storage":           RadarrStorageManager,
            "sync":              RadarrSyncManager,
            "repair":            RadarrRepairWrapperManager,
            "orchestration":     RadarrOrchestrationManager,
        }
        self.all_component_classes = {k: v for k, v in full_components.items() if k in enabled_keys}

        self.critical_keys = {
            "instance_manager",
            "movies",
            "quality",
            "storage",
            "orchestration",
        }

        self.critical_components, self.noncritical_components = split_components(
            all_components=self.all_component_classes,
            critical_keys=self.critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=self.init_args,
        )

        self.logger.log_debug(
            f"🧩 RadarrManager initialized with filtered components: {sorted(enabled_keys)}"
        )

    def _load_component(self, name: str, auto_load_deps: bool = True, log_dependencies: bool = True):
        if hasattr(self, name) and getattr(self, name) is not None:
            return getattr(self, name)
        existing = self.registry.get("manager", name)
        if existing:
            setattr(self, name, existing)
            return existing
        component_class = self.critical_components.get(name) or self.noncritical_components.get(name)
        if not component_class:
            self.load_summary[name] = "❌ unknown"
            return None
        for dep in self.component_dependencies.get(name, []):
            if not getattr(self, dep, None) and auto_load_deps:
                self._load_component(dep)
        try:
            instance = self._singleton(name, component_class, **self.init_args)
            setattr(self, name, instance)
            self.load_summary[name] = "✅"
            return instance
        except Exception as e:
            self.load_summary[name] = f"❌"
            self.logger.log_error(f"[{self.__class__.__name__}] ❌ {name}: {e}")
            return None

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        cls = self.__class__.__name__
        # Load all components. Components built eagerly in __init__ (instance_manager,
        # radarr_cache) bypass _load_component — which is the only thing that writes a
        # load_summary row — so mark them loaded here, else they render ❌ despite
        # being healthy and used by the whole pipeline.
        for name in self.component_dependencies:
            if getattr(self, name, None) is None:
                self._load_component(name)
            elif not str(self.load_summary.get(name, "")).startswith("✅"):
                self.load_summary[name] = "✅"
        # Prepare sub-components; a prepare() failure flips that component to ❌
        # (previously such failures were silently swallowed).
        failed = []
        for name in self.component_dependencies:
            component = getattr(self, name, None)
            if component and hasattr(component, "prepare"):
                try:
                    component.prepare()
                except Exception as e:
                    failed.append(name)
                    self.load_summary[name] = "❌"
                    self.logger.log_error(f"[{cls}] ❌ {name}.prepare(): {e}")
        # Colour-coded summary: green when all prepared, yellow listing failures.
        names = list(self.component_dependencies.keys())
        n_ok  = sum(1 for n in names if str(self.load_summary.get(n, '')).startswith('✅'))
        if failed:
            self.logger.log_warning(
                f"[{cls}] {n_ok}/{len(names)} components prepared; failed: {', '.join(failed)}")
        else:
            self.logger.log_debug(f"[{cls}] {len(names)}/{len(names)} components prepared")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        cls = self.__class__.__name__
        results = {}
        for name in self.component_dependencies:
            component = getattr(self, name, None) or self._load_component(name)
            if component and hasattr(component, "run"):
                try:
                    component.run()
                    results[name] = "✅"
                except Exception as e:
                    results[name] = f"❌"
                    self.logger.log_error(f"[{cls}] ❌ {name}.run(): {e}")
        self.load_summary.update(results)
        all_ok = all(str(v).startswith("✅") for v in results.values())
        self.log_filtered_component_summary(
            service_name="Radarr", component_label=cls,
            critical_components=results.keys(), noncritical_components=[],
            all_critical_loaded=all_ok,
        )

from typing import Optional

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.cache.key_builder import CacheKeyBuilder
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.factories.mixins.ordered_components import topo_order
from scripts.managers.services.sonarr.cache import SonarrCacheManager
from scripts.managers.services.sonarr.episodes import SonarrEpisodesManager
from scripts.managers.services.sonarr.instance import SonarrInstanceManager
from scripts.managers.services.sonarr.monitoring import SonarrMonitoringManager
from scripts.managers.services.sonarr.orchestration import SonarrOrchestrationManager
from scripts.managers.services.sonarr.quality import SonarrQualityManager
from scripts.managers.services.sonarr.repair import SonarrRepairManager
from scripts.managers.services.sonarr.series import SonarrSeriesManager
from scripts.managers.services.sonarr.storage import SonarrStorageManager
from scripts.managers.services.sonarr.sync import SonarrSyncManager
from scripts.managers.services.sonarr.validator import SonarrValidatorManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrManager"

    instance_manager: Optional[SonarrInstanceManager] = None
    sonarr_cache: Optional[SonarrCacheManager] = None
    key_builder: Optional[CacheKeyBuilder] = None

    validator_manager: Optional[SonarrValidatorManager] = None
    episodes: Optional[SonarrEpisodesManager] = None
    series: Optional[SonarrSeriesManager] = None
    quality: Optional[SonarrQualityManager] = None
    monitoring: Optional[SonarrMonitoringManager] = None
    sync: Optional[SonarrSyncManager] = None
    storage: Optional[SonarrStorageManager] = None
    repair: Optional[SonarrRepairManager] = None
    orchestration: Optional[SonarrOrchestrationManager] = None

    @LoggerManager().log_function_entry
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        # BaseManager's param is 'global_cache' (NOT 'cache') — passing cache= left
        # self.global_cache=None on this manager. self.cache stays as an alias for the
        # init_args/child constructions below.
        self.cache = global_cache
        super().__init__(logger=logger, config=config, global_cache=global_cache, validator=validator, registry=registry, **kwargs)

        self.dry_run = kwargs.get("dry_run", False)
        self.load_summary = {}
        self.key_builder = CacheKeyBuilder()

        # --- Step 1: Define dependencies (active subset) ---
        # {component: [names that must load before it]}. prepare()/run() iterate
        # this through topo_order(), so the order honours these declared deps
        # regardless of the dict's insertion order — explicit and reorder-proof.
        # prepare() loads + prepares every entry; run() invokes only the entries
        # that actually define a run().
        self.component_dependencies = {
            "instance_manager": ["manager"],
            # sonarr_cache is initialised manually in __init__ and is not a
            # loadable component — "cache" (GlobalCacheManager) is intentionally
            # absent here to prevent prepare() from invoking GlobalCacheManager.prepare().
            "storage": ["instance_manager"],
            "series": ["instance_manager"],
            "episodes": ["series", "instance_manager"],
            # monitoring / repair / validator must load before orchestration so
            # SonarrOrchestrationManager can find them on self when it initialises
            "monitoring": ["instance_manager", "series", "episodes"],
            "repair": ["instance_manager"],
            "validator_manager": ["instance_manager"],
            "orchestration": ["series", "episodes", "storage"],
        }
        enabled_keys = set(self.component_dependencies.keys())

        # --- Step 2: Initialize instance manager (always first) ---
        self.instance_manager = SonarrInstanceManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,
            manager=self
        )
        self.instance_manager.sonarr_api = self.instance_manager
        self.sonarr_api = self.instance_manager
        self.instance_manager.set_sonarr_cache(self.sonarr_cache)

        # --- Step 3: Initialize sonarr_cache (Sonarr-specific manager) ---
        self.sonarr_cache = SonarrCacheManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.cache,
            validator=self.validator,
            registry=self.registry,
            dry_run=self.dry_run,  # without this the cache (and its episode-file
                                   # ops: acquisition, sync, JIT) ran LIVE even in
                                   # dry_run=True sessions
        )
        self.sonarr_cache.initialize_cache_structure(include_optionals=True)

        # --- Step 4: Common kwargs for all downstream components ---
        self.init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.cache,
            "validator": self.validator,
            "registry": self.registry,
            "dry_run": self.dry_run,
            "key_builder": self.key_builder,
            "sonarr_api": self.sonarr_api,
            "instance_manager": self.instance_manager,
            "cache_manager": self.sonarr_cache,
            "parent_name": self.parent_name,
            "manager": self
        }

        # --- Step 4: Component class mapping (filtered by enabled keys) ---
        full_components = {
            "validator_manager": SonarrValidatorManager,
            "series": SonarrSeriesManager,
            "episodes": SonarrEpisodesManager,
            "monitoring": SonarrMonitoringManager,
            "quality": SonarrQualityManager,
            "storage": SonarrStorageManager,
            "sync": SonarrSyncManager,
            "repair": SonarrRepairManager,
            "orchestration": SonarrOrchestrationManager
        }
        self.all_component_classes = {k: v for k, v in full_components.items() if k in enabled_keys}

        self.critical_keys = {
            "instance_manager",
            "series",
            "episodes",
            "quality",
            "storage",
            "monitoring",
            "repair",
            "validator_manager",
            "orchestration",
        }

        self.critical_components, self.noncritical_components = split_components(
            all_components=self.all_component_classes,
            critical_keys=self.critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=self.init_args
        )

        self.logger.log_debug(f"🧩 SonarrManager initialized with filtered components: {sorted(enabled_keys)}")

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
            self.load_summary[name] = "❌"
            self.logger.log_error(f"[{self.__class__.__name__}] ❌ {name}: {e}")
            return None

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        cls = self.__class__.__name__
        # Explicit, dependency-respecting order (reorder-proof — see topo_order).
        order = topo_order(self.component_dependencies)
        # Components built eagerly in __init__ (instance_manager) bypass
        # _load_component — the only thing that writes a load_summary row — so mark
        # them loaded here, else they render ❌ despite being healthy.
        for name in order:
            if getattr(self, name, None) is None:
                self._load_component(name)
            elif not str(self.load_summary.get(name, "")).startswith("✅"):
                self.load_summary[name] = "✅"
        # Prepare sub-components; a prepare() failure flips that component to ❌
        # (previously such failures were silently swallowed).
        failed = []
        for name in order:
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
        order = topo_order(self.component_dependencies)
        for name in order:
            component = getattr(self, name, None) or self._load_component(name)
            if component and hasattr(component, "run"):
                try:
                    component.run()
                    results[name] = "✅"
                except Exception as e:
                    results[name] = "❌"
                    self.logger.log_error(f"[{cls}] ❌ {name}.run(): {e}")
        self.load_summary.update(results)
        all_ok = all(str(v).startswith("✅") for v in results.values())
        self.log_filtered_component_summary(
            service_name="Sonarr", component_label=cls,
            critical_components=results.keys(), noncritical_components=[],
            all_critical_loaded=all_ok,
        )

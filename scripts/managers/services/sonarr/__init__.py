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
            # GLD-SON-01 - quality and sync were declared in full_components and
            # ABSENT here, so `all_component_classes` filtered them out and neither
            # loaded, prepared nor ran. Radarr wires both
            # (`"quality": ["movies", "instance_manager"]`, `"sync":
            # ["instance_manager"]`), so the two services were configured
            # ASYMMETRICALLY: Radarr's custom formats, naming, folders and tags
            # came from glidearr while Sonarr's were whatever had been set by hand.
            #
            # Dependencies mirror Radarr's: quality needs the library loaded to
            # reason about profiles; sync only needs an API handle.
            #
            # SYNC WRITES TO SONARR. It has no run() - it is __init__ plus five
            # sub-managers (custom_formats, folders, media_management, naming,
            # tags) that push configuration INTO the service on prepare(). They
            # receive `dry_run` through init_args, so a disarmed pass previews the
            # whole change set. Run it disarmed FIRST; this is a config rewrite of
            # a live service, not a read.
            "quality": ["instance_manager", "series", "episodes"],
            "sync": ["instance_manager"],
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

        # GLD-SON-13 (detector) - a component declared here but absent from
        # component_dependencies is filtered out above and NEVER loads, prepares
        # or runs. Nothing said so: the prepare summary counts
        # len(component_dependencies), so it reported "8/8 prepared" while two
        # built subsystems sat disconnected. A count that can only ever equal its
        # own denominator is not a check.
        #
        # Recorded at construction so both prepare() and run() can surface it,
        # and so the FIX for GLD-SON-01 is verifiable rather than asserted: when
        # quality/sync are wired, this set empties and the warning stops.
        self.unwired_components = sorted(set(full_components) - enabled_keys)
        if self.unwired_components:
            self.logger.log_warning(
                f"[Sonarr] {len(self.unwired_components)} component(s) declared but NOT WIRED: "
                f"{', '.join(self.unwired_components)} - present in full_components, absent "
                f"from component_dependencies, so they never load, prepare or run "
                f"(GLD-SON-01/13).")

        # GLD-SON-11 - critical_keys must be a SUBSET of what can actually load.
        # It listed "quality", which is filtered out, so that entry matched
        # nothing and noncritical_components came out EMPTY - reading as a
        # guarantee that quality loads when it is precisely the thing that does
        # not. Intersected rather than silently trusted, and the discrepancy is
        # reported rather than swallowed.
        _declared_critical = {
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
        _loadable = set(self.all_component_classes) | {"instance_manager"}
        _phantom = sorted(_declared_critical - _loadable)
        if _phantom:
            self.logger.log_warning(
                f"[Sonarr] critical_keys names {len(_phantom)} component(s) that cannot load: "
                f"{', '.join(_phantom)} - dropped from the critical set so the split is "
                f"honest (GLD-SON-11).")
        self.critical_keys = _declared_critical & _loadable

        # GLD-SON-12 - the INVARIANT, asserted rather than merely repaired above.
        # `GLD-SON-11` was a critical_keys entry naming a component that could never
        # load; the intersection fixes that instance silently. This makes the class
        # of bug impossible to introduce again without noticing: critical_keys must
        # be a subset of what can actually load, and any drift is a startup-time
        # error rather than a runtime mystery.
        #
        # It does NOT raise. A hard failure here would take down the whole Sonarr
        # subsystem over a bookkeeping mistake, which is a worse outcome than the
        # bug it guards. The log line is the alarm; the intersection above is the
        # repair.
        assert self.critical_keys <= _loadable, (
            f"critical_keys must be a subset of loadable components; "
            f"{sorted(self.critical_keys - _loadable)} are not")

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

    def _verify_dry_run_propagation(self) -> None:
        """GLD-SON-03 - every loaded component must agree with this manager's dry_run.

        THIS GUARDS A BUG THAT ALREADY HAPPENED. The `SonarrCacheManager`
        construction above carries the scar in a comment: *"without this the cache
        (and its episode-file ops: acquisition, sync, JIT) ran LIVE even in
        dry_run=True sessions"*. It was fixed by adding one kwarg, by hand, and
        nothing has verified since that it stays passed - or that the next
        component added to `init_args` gets it.

        A component that MISSED the flag reports `dry_run=False` while the run is
        disarmed, and writes. That is the most expensive failure this subsystem
        has: the operator believes they are previewing.

        Distinct from `GLD-SON-20`, which was the opposite half - components that
        RECEIVED `dry_run`, stored it, and never read it. Received-and-ignored is
        a code bug in the leaf; never-received is a wiring bug here. Both produce
        writes during a preview, so both are checked.

        WARNS, never raises: aborting a whole run over a propagation mismatch is
        worse than the mismatch when the run is ARMED (where True==True anyway).
        The loud case is exactly the dangerous one - disarmed manager, armed
        component - and it is called out separately.
        """
        expected = bool(getattr(self, "dry_run", False))
        mismatched, unset = [], []
        for name in self.component_dependencies:
            comp = getattr(self, name, None)
            if comp is None:
                continue                      # not loaded; GLD-SON-14 reports that
            if not hasattr(comp, "dry_run"):
                # No attribute at all - it never received the kwarg AND never
                # defaulted it. Cannot be disarmed by anything.
                unset.append(name)
                continue
            if bool(getattr(comp, "dry_run")) != expected:
                mismatched.append((name, bool(getattr(comp, "dry_run"))))

        if unset:
            self.logger.log_warning(
                f"[Sonarr] {len(unset)} component(s) carry NO dry_run attribute: "
                f"{', '.join(sorted(unset))} - they cannot be disarmed and will write "
                f"during a preview (GLD-SON-03).")
        for name, actual in mismatched:
            if expected and not actual:
                # The dangerous direction: run is disarmed, component is ARMED.
                self.logger.log_error(
                    f"[Sonarr] ❌ '{name}' has dry_run=False while the run is DISARMED - "
                    f"it will WRITE during this preview (GLD-SON-03).")
            else:
                self.logger.log_warning(
                    f"[Sonarr] '{name}' dry_run={actual} but the manager is {expected} "
                    f"- propagation mismatch (GLD-SON-03).")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        cls = self.__class__.__name__
        results = {}
        # GLD-SON-03 - verify before anything executes. A component that never
        # received dry_run would otherwise write during a preview and only be
        # noticed afterwards, if at all.
        self._verify_dry_run_propagation()
        order = topo_order(self.component_dependencies)
        for name in order:
            component = getattr(self, name, None) or self._load_component(name)
            if component is None:
                # GLD-SON-14 - a component that FAILS TO LOAD used to fall through
                # this branch with no entry in `results`, so `all(results.values())`
                # was computed over a dict that never contained the failure and the
                # summary came back green. A load failure at run time is the single
                # most important thing this method can report, and it was the one
                # thing it could not *(P-D)*.
                results[name] = "❌"
                self.logger.log_error(
                    f"[{cls}] ❌ {name} did not load - not run. Every component that"
                    f" reached run() is expected to be constructible; treat this as a"
                    f" failed run, not a skipped optional.")
                continue
            if not hasattr(component, "run"):
                # Legitimately runless (prepare-only components). Recorded as such so
                # "has no run()" and "failed to load" are distinguishable in the
                # summary rather than both reading as absence.
                results[name] = "– no run()"
                continue
            try:
                component.run()
                results[name] = "✅"
            except Exception as e:
                results[name] = "❌"
                self.logger.log_error(f"[{cls}] ❌ {name}.run(): {e}")
        self.load_summary.update(results)
        # Only ❌ is a failure. A runless component is not one, so it must not drag
        # the verdict down - but it must still APPEAR, which is why it gets a marker
        # rather than being omitted.
        all_ok = not any(str(v).startswith("❌") for v in results.values())
        if len(results) != len(order):
            # Belt and braces: the loop above now records every name, so this can only
            # fire if someone adds an early `continue`. Cheap, and it fails loudly.
            self.logger.log_error(
                f"[{cls}] ❌ run() accounted for {len(results)} of {len(order)} "
                f"component(s) - the summary below is incomplete.")
            all_ok = False
        self.log_filtered_component_summary(
            service_name="Sonarr", component_label=cls,
            critical_components=results.keys(), noncritical_components=[],
            all_critical_loaded=all_ok,
        )

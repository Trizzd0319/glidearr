# beta/managers/factories/base_manager.py
import inspect
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

from scripts.managers.factories.config.__Init__ import ConfigManager
from scripts.managers.factories.registry import RegistryManager
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit


class BaseManager:
    _instances = {}  # 🔁 Global singleton instance registry
    _singleton_instances = {}
    _singleton_lock = Lock()

    # Set once per process by _preview_cache_keys() — see its docstring
    # (`GLD-MGR-11`). Class-level because the read runs from every manager's
    # __init__, so a per-instance warning would emit ~40 times a run.
    _warned_missing_cache_memory = False

    def __new__(cls, *args, **kwargs):
        key = kwargs.get("singleton_key")
        inst_key = (cls, key)

        if inst_key not in cls._instances:
            instance = super().__new__(cls)
            cls._instances[inst_key] = instance
            # print(f"[Singleton] Creating new instance of {cls.__name__} with key={key} id={id(instance)}")
        else:
            instance = cls._instances[inst_key]
            # print(f"[Singleton] Reusing instance of {cls.__name__} with key={key} id={id(instance)}")

        return instance

    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.name = self.__class__.__name__
        self.logger = logger or LoggerManager()
        self.config = config or ConfigManager(logger=self.logger)
        self.global_cache = global_cache
        self.validator = validator
        self.registry = registry or RegistryManager()

        self.cli_flags = kwargs.get("cli_flags", {})
        self.timestamp = datetime.now().isoformat()

        # ── dry_run: resolved ONCE, here, for every manager ───────────────────
        # Historically BaseManager did NOT capture dry_run, so ~40 managers each
        # re-implemented the capture and a manager that forgot silently defaulted
        # to FALSE — i.e. LIVE. That footgun has already fired: see the inline
        # record in sonarr/__init__.py, where the episode-file cache "ran LIVE
        # even in dry_run=True sessions".
        #
        # PRECEDENCE, and why this order:
        #   1. explicit kwarg   — the caller said so. Must win outright, because
        #                         __new__ is a SINGLETON registry: __init__ can run
        #                         a second time on an existing instance, and a
        #                         later explicit False has to be able to override
        #                         an earlier True.
        #   2. a value this instance already carries — a subclass that resolved
        #                         dry_run BEFORE calling super() (e.g.
        #                         radarr/repair/anomaly.py, which walks its own
        #                         manager chain). Never clobber a deliberate
        #                         upstream resolution.
        #   3. the CONSTRUCTING manager (kwargs["manager"]) — NOT the same thing as
        #                         the registry parent below. parent_name has
        #                         several semantics across the codebase and the
        #                         registry lookup can miss, while the manager that
        #                         actually built this object is always right there
        #                         in kwargs. This rung is what makes the old
        #                         per-subclass `kwargs.get("dry_run", getattr(
        #                         parent, "dry_run", False))` lines redundant.
        #   4. the registry parent — the remaining case: built without a manager
        #                         kwarg but linked by name. Previously the parent
        #                         link below copied logger/config/global_cache/
        #                         validator and simply omitted dry_run.
        #   5. False             — today's behaviour, kept so this change is
        #                         byte-identical for every manager that already
        #                         passes the kwarg explicitly.
        #
        # Step 4 is applied in the parent block further down, once `parent` is
        # known; steps 1-3 are resolved here so the value exists before any
        # subclass code runs.
        _dry_run = kwargs.get("dry_run")
        if _dry_run is None:
            _dry_run = getattr(self, "dry_run", None)
        if _dry_run is None:
            # getattr tolerates both None and the `{}` null-object some managers
            # pass for a missing parent.
            _dry_run = getattr(kwargs.get("manager"), "dry_run", None)
        self.dry_run = None if _dry_run is None else bool(_dry_run)

        # Auto-resolve parent name
        self.parent_name = kwargs.get("parent_name") or self._infer_parent_from_path()

        # Try to grab some cache summary without recursion.
        # Delegates to _preview_cache_keys() -- this block used to be a second,
        # byte-identical copy of it (P-E), which is why the wrong attribute name
        # below had to be fixed in two places (`GLD-MGR-11`).
        cache_keys = self._preview_cache_keys()

        self.dep_versions = {
            "config_version": getattr(self.config, 'version', 'n/a'),
            "cache_keys": cache_keys
        }

        self.logger.log_debug(f"🔧 Initializing {self.name}")
        self._log_init_summary()

        try:
            self.registry.register("manager", self.name, self)

            self.registry.auto_hot_swap_from_config(self.config.raw_data)

            parent = self.registry.get("manager", self.parent_name)

            if parent:
                self.logger.log_debug(f"🔗 Linking {self.name} to parent: {parent.__class__.__name__}")
                self.logger = getattr(parent, "logger", self.logger)
                self.config = getattr(parent, "config", self.config)
                # canonical attr is 'global_cache' (matches line ~191 and the deferred path);
                # `or self.global_cache` so a parent whose cache is None never clobbers a good one.
                self.global_cache = getattr(parent, "global_cache", None) or self.global_cache
                self.validator = getattr(parent, "validator", self.validator)
                self.manager = getattr(parent, "manager", parent)
                # dry_run inherits the same way — but ONLY when nothing more
                # specific supplied it (see the precedence note above). This is the
                # line whose absence made dry_run a distributed invariant: every
                # other shared field was already inherited here.
                if self.dry_run is None:
                    _p = getattr(parent, "dry_run", None)
                    if _p is not None:
                        self.dry_run = bool(_p)
            else:
                # self.logger.log_debug(f"⚠️ No parent found for {self.name}; standalone init")
                pass
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to register {self.name} with RegistryManager: {e}")

        # Debug-only dump, deliberately OUTSIDE the block above (GLD-REG-03).
        # print_tree_view did not exist on RegistryManager until now, and the call
        # sat between registration and parent LINKING inside one try/except -- so
        # switching this flag on would have raised AttributeError before the link
        # ran and cost every manager its inherited logger, config, global_cache,
        # validator AND dry_run, surfacing only as "failed to register". A debug
        # print must not be able to disarm dry_run, so it gets its own guard and
        # runs after linking is complete.
        if kwargs.get("print_registry_tree", False):
            try:
                self.registry.print_tree_view(category="manager")
            except Exception as e:
                self.logger.log_warning(f"⚠️ Could not print registry tree: {e}")

        # Always attempt deferred link if initial link failed
        self._resolve_deferred_parent()

        # ── dry_run, final step ───────────────────────────────────────────────
        # Nothing supplied one: kwargs silent, no pre-super value, no parent (or a
        # parent that carries none). Fall back to False — today's behaviour for
        # every manager, so this whole block is a no-op wherever dry_run was
        # already being passed.
        #
        # NOT a raise. sonarr/series/space_pressure.py is right to refuse to
        # construct without an explicit value, but that is a judgement a manager
        # that MOVES FILES gets to make; a base class shared by read-only managers
        # cannot. Managers with a destructive blast radius should keep their own
        # stricter resolution on top of this.
        #
        # KNOWN LIMITATION — a subclass that runs
        #     self.dry_run = kwargs.get("dry_run", False)
        # AFTER super().__init__() still overwrites a parent-inherited True with
        # False. This block fixes managers that never captured dry_run at all; it
        # cannot fix ones that actively overwrite. Those lines should be DELETED
        # now that the base class resolves it. Known sites: services/writeback,
        # services/calendar, radarr/quality/selector (all two-level, defaulting
        # False) — see GLD-WB-03 / GLD-CAL-01 / GLD-RQ-11.
        if self.dry_run is None:
            self.dry_run = False

    def _init_summary_data(self):
        pass  # superseded by BaseInstanceManager._finalize

    def _register_with_registry(self, **kwargs):
        pass  # superseded by BaseManager.__init__ registration block

    @LoggerManager().log_function_entry
    @timeit("_log_init_summary")
    def _log_init_summary(self):
        pass  # intentionally silent — summary emitted by _finalize / prepare()

    def format_cache_key(self, key: str, instance: str = None, user: str = None) -> str:
        return (
            key.replace("<instance>", instance or "default")
               .replace("<user>", user or "default")
        )

    def _get_last_timestamp(self, cache_key: str, instance: str) -> Optional[str]:
        cached = self.global_cache.get(cache_key) if self.global_cache else None
        if cached and isinstance(cached, dict):
            meta = cached.get("meta", {})
            if isinstance(meta, dict):
                return meta.get("timestamp")
        return None

    def resolve_instance(self, instance):
        if isinstance(instance, str):
            return instance
        if hasattr(instance, "name"):
            return instance.name
        raise ValueError(f"Cannot resolve instance from: {instance}")

    def _singleton(self, name, cls, **kwargs):
        key = (self.__class__.__name__, name)
        if key not in self._singleton_instances:
            with self._singleton_lock:
                if key not in self._singleton_instances:
                    instance = cls(**kwargs)
                    self._singleton_instances[key] = instance
                    self.logger.log_debug(f"🔧 Created singleton for {name}: {cls.__name__}")
        return self._singleton_instances[key]


    @LoggerManager().log_function_entry
    def prepare(self):
        """Load all critical subcomponents silently, emit one summary line."""
        cls = self.__class__.__name__
        critical_keys = getattr(self, "critical_keys", []) or []
        if not critical_keys:
            return
        for name in critical_keys:
            if getattr(self, name, None) is None:
                self._load_component(name)
        load_summary = getattr(self, "load_summary", {})
        parts = "  ".join(
            f"{n}{'✅' if str(load_summary.get(n, '')).startswith('✅') else '❌'}"
            for n in critical_keys
        )
        all_ok = all(str(load_summary.get(n, '')).startswith('✅') for n in critical_keys)
        self.logger.log_info(
            f"[{cls}] {'✅' if all_ok else '⚠️'} "
            f"{sum(str(load_summary.get(n,'')).startswith('✅') for n in critical_keys)}/{len(critical_keys)}: {parts}"
        )

    @LoggerManager().log_function_entry
    def run(self):
        """Base no-op run; override in orchestration-driven managers."""
        self.logger.log_debug(f"[{self.name}] run() — no orchestration configured, no-op.")

    def _preview_cache_keys(self):
        """The first few live cache keys, for the init summary. `[]` when there is none.

        ⚠️ Reads `memory`, NOT `memory_cache`. This asked `global_cache` for
        `memory_cache` -- an attribute `GlobalCacheManager` has never had, since
        the `MemoryManager` is exposed as `.memory` -- and the `{}` default
        turned that miss into an empty result. So `dep_versions["cache_keys"]`
        and every init summary have reported an EMPTY CACHE on every manager
        since the field was written, and could not have reported anything else.
        Absent conflated with empty (P-C), in the one field whose entire job is
        to say what the cache is holding.

        A missing `memory` now WARNS once rather than returning a silent `[]`,
        because it would mean the cache contract changed -- which is exactly the
        condition that went unnoticed here. An empty MemoryManager still returns
        `[]`, and that answer is now trustworthy (`GLD-MGR-11`).
        """
        if not self.global_cache or self.name == "GlobalCacheManager":
            return []

        memory = getattr(self.global_cache, "memory", None)
        if memory is None:
            # Once per process: this runs from __init__, i.e. ~40 times a run.
            if not BaseManager._warned_missing_cache_memory:
                BaseManager._warned_missing_cache_memory = True
                self.logger.log_warning(
                    "⚠️ global_cache exposes no `memory` MemoryManager — cache-key previews "
                    "will be empty. That is a CONTRACT CHANGE, not an empty cache "
                    "(GLD-MGR-11)."
                )
            return []

        try:
            return list(memory.keys())[:5]
        except Exception as e:
            self.logger.log_warning(f"⚠️ Could not read cache keys during init: {e}")
            return []

    def _infer_parent_from_path(self):
        try:
            path = Path(inspect.getfile(self.__class__)).resolve()
            folder = path.parent.name if path.name != "__init__.py" else path.parent.parent.name
            service_path = [p for p in path.parts if p in {"sonarr", "radarr", "tautulli", "trakt"}]
            service_prefix = service_path[-1].capitalize() if service_path else ""

            # Fix duplicate like "SonarrSonarr"
            if folder.lower() == service_prefix.lower():
                return f"{service_prefix}Manager"

            return f"{service_prefix}{folder.capitalize()}"
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to auto-detect parent_name: {e}")
            return self.__class__.__name__

    def _resolve_deferred_parent(self):
        if not getattr(self, "manager", None) and self.parent_name:
            try:
                parent = self.registry.get("manager", self.parent_name)
                if parent and parent is not self:
                    self.logger = getattr(parent, "logger", self.logger)
                    self.config = getattr(parent, "config", self.config)
                    # `or self.global_cache`: a parent whose cache is None must not clobber a good one
                    self.global_cache = getattr(parent, "global_cache", None) or self.global_cache
                    self.validator = getattr(parent, "validator", self.validator)
                    self.manager = getattr(parent, "manager", parent)
                    self.logger.log_info(f"🔗 Deferred linking: {self.name} → {self.parent_name}")
            except Exception as e:
                self.logger.log_warning(f"⚠️ Deferred linking failed for {self.name}: {e}")

    @LoggerManager().log_function_entry
    @timeit("_load_component")
    def _load_component(self, name):
        """
        Safely load a component by name from the registry if available.
        This supports late-binding of subcomponents like 'orchestration'.
        """
        if not hasattr(self, "registry") or not self.registry:
            self.logger.log_warning(f"⚠️ No registry available to load component '{name}'")
            return

        component = self.registry.get("manager", name)
        if component:
            setattr(self, name, component)
            self.logger.log_debug(f"🔗 Loaded component '{name}' from registry into {self.name}")
        else:
            self.logger.log_warning(f"⚠️ Component '{name}' not found in registry for {self.name}")

    def get_tag_monitor(self):
        """
        Resolve the Sonarr keep-tag monitor (SonarrSyncTagsManager) — the object
        exposing ``is_series_tagged_keep(series_id)`` used by series-sync and
        monitoring to protect 'keep'-tagged series.

        Resolves it from the registry and lazily creates it (a BaseManager
        singleton) with this manager's context if absent. Returns ``None`` if it
        cannot be resolved/created (e.g. called outside a Sonarr context). The
        keep set itself is populated lazily inside ``is_series_tagged_keep``, so
        this is safe to call at init time, before caches are warm.
        """
        try:
            tm = self.registry.get("manager", "SonarrSyncTagsManager") if self.registry else None
        except Exception:
            tm = None
        if tm is not None:
            return tm
        try:
            # Lazy import to avoid a base→service import cycle; only hit in
            # Sonarr context where the class is importable.
            from scripts.managers.services.sonarr.sync.tags import SonarrSyncTagsManager
            return SonarrSyncTagsManager(
                logger=self.logger,
                config=self.config,
                global_cache=self.global_cache,
                validator=self.validator,
                registry=self.registry,
                manager=self,
                sonarr_api=getattr(self, "sonarr_api", None),
                instance_manager=getattr(self, "instance_manager", None),
            )
        except Exception as e:
            try:
                self.logger.log_debug(f"[tag_monitor] unavailable: {e}")
            except Exception:
                pass
            return None

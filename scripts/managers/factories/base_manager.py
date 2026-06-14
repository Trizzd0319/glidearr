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

        # Auto-resolve parent name
        self.parent_name = kwargs.get("parent_name") or self._infer_parent_from_path()

        # Try to grab some cache summary without recursion
        cache_keys = []
        if self.global_cache and self.name != "GlobalCacheManager":
            try:
                cache_keys = list(getattr(self.global_cache, 'memory_cache', {}).keys())[:5]
            except Exception as e:
                self.logger.log_warning(f"⚠️ Could not read cache keys during init: {e}")

        self.dep_versions = {
            "config_version": getattr(self.config, 'version', 'n/a'),
            "cache_keys": cache_keys
        }

        self.logger.log_debug(f"🔧 Initializing {self.name}")
        self._log_init_summary()

        try:
            self.registry.register("manager", self.name, self)

            if kwargs.get("print_registry_tree", False):
                self.registry.print_tree_view(category="manager")

            self.registry.auto_hot_swap_from_config(self.config.raw_data)

            parent = self.registry.get("manager", self.parent_name)

            if parent:
                self.logger.log_debug(f"🔗 Linking {self.name} to parent: {parent.__class__.__name__}")
                self.logger = getattr(parent, "logger", self.logger)
                self.config = getattr(parent, "config", self.config)
                self.global_cache = getattr(parent, "cache", self.global_cache)
                self.validator = getattr(parent, "validator", self.validator)
                self.manager = getattr(parent, "manager", parent)
            else:
                # self.logger.log_debug(f"⚠️ No parent found for {self.name}; standalone init")
                pass
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to register {self.name} with RegistryManager: {e}")

        # Always attempt deferred link if initial link failed
        self._resolve_deferred_parent()

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
        if not self.global_cache or self.name == "GlobalCacheManager":
            return []
        try:
            return list(getattr(self.global_cache, 'memory_cache', {}).keys())[:5]
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
                    self.global_cache = getattr(parent, "global_cache", self.global_cache)
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

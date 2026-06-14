from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin

from scripts.managers.services.sonarr.cache.episode_files import SonarrCacheEpisodeFilesManager
from scripts.managers.services.sonarr.cache.episodes import SonarrCacheEpisodesManager
from scripts.managers.services.sonarr.cache.history import SonarrCacheHistoryManager
from scripts.managers.services.sonarr.cache.instances import SonarrCacheInstanceManager
from scripts.managers.services.sonarr.cache.monitoring import SonarrCacheMonitoringManager
from scripts.managers.services.sonarr.cache.quality import SonarrCacheQualityManager
from scripts.managers.services.sonarr.cache.series import SonarrCacheSeriesManager
from scripts.managers.services.sonarr.cache.tags import SonarrCacheTagManager
from scripts.managers.services.sonarr.orchestration.cache import SonarrOrchestrationCacheManager

from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.managers.component_splitter import split_components


class SonarrCacheManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrCacheManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, sonarr_api=None, **kwargs):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # Store sonarr_api as a direct attribute so child components can find
        # it via getattr(manager, "sonarr_api", None).  May be None when
        # SonarrManager does not pass it down (episode_files handles that via
        # a registry fallback in its own __init__).
        self.sonarr_api = sonarr_api
        # dry_run must be stored on self BEFORE init_args is built so that
        # child components reading getattr(manager, 'dry_run') get the right value.
        self.dry_run = kwargs.get("dry_run", False)

        self.load_summary = {}
        all_critical_loaded = True

        # 🔧 Submanager classes to load
        # NOTE: episode_files is intentionally excluded from this map.
        # split_components temp-instantiates every non-critical entry to inspect
        # parent_name, which (a) causes a double-init side-effect and (b) would
        # silently drop the component if parent_name doesn't match the magic
        # string "SonarrCacheManager".  We initialise it manually below instead.
        all_component_classes = {
            "episodes": SonarrCacheEpisodesManager,
            "history": SonarrCacheHistoryManager,
            "instance": SonarrCacheInstanceManager,
            "monitoring": SonarrCacheMonitoringManager,
            "orchestration": SonarrOrchestrationCacheManager,
            "quality": SonarrCacheQualityManager,
            "series": SonarrCacheSeriesManager,
            "tags": SonarrCacheTagManager,
        }

        critical_keys = set(all_component_classes.keys())

        init_args = {
            "logger": self.logger,
            "config": self.config,
            "global_cache": self.global_cache,
            "validator": self.validator,
            "registry": self.registry,
            "manager": self,
            "sonarr_api": sonarr_api,  # pass None rather than self; components must not use SonarrCacheManager as an API proxy
            "sonarr_cache": self,
            "dry_run": self.dry_run,
        }

        # 🧩 Load and register components
        critical_components, noncritical_components = split_components(
            all_components=all_component_classes,
            critical_keys=critical_keys,
            parent_name_match=self.parent_name,
            logger=self.logger,
            logger_context=self.__class__.__name__,
            init_kwargs=init_args
        )

        for name, cls in critical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.cache.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.cache.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"
                all_critical_loaded = False

        for name, cls in noncritical_components.items():
            try:
                instance = cls(**init_args)
                setattr(self, name, instance)
                self.registry.set_flag(f"sonarr.cache.{name}_initialized", True)
                self.load_summary[name] = "✅ Loaded"
            except Exception as e:
                self.registry.set_flag(f"sonarr.cache.{name}_initialized", False)
                self.load_summary[name] = f"❌ Failed: {e}"

        # ── episode_files: manual init — bypasses split_components so the
        # component is not temp-instantiated (side-effect: double registry
        # registration) and not silently dropped by the parent_name check.
        try:
            self.episode_files = SonarrCacheEpisodeFilesManager(**init_args)
            self.registry.set_flag("sonarr.cache.episode_files_initialized", True)
            self.load_summary["episode_files"] = "✅ Loaded"
            self.logger.log_debug("🎬 SonarrCacheEpisodeFilesManager loaded (ML enrichment layer)")
        except Exception as e:
            self.episode_files = None
            self.registry.set_flag("sonarr.cache.episode_files_initialized", False)
            self.load_summary["episode_files"] = f"❌ Failed: {e}"
            self.logger.log_warning(
                f"⚠️ SonarrCacheEpisodeFilesManager failed to load — "
                f"episode file enrichment disabled: {e}"
            )

        self.all_components_loaded = all_critical_loaded
        self.registry.set_flag("sonarr.cache_manager_initialized", all_critical_loaded)

        self.log_filtered_component_summary(
            service_name="Sonarr",
            component_label=self.__class__.__name__,
            critical_components=critical_components.keys(),
            noncritical_components=noncritical_components.keys(),
            all_critical_loaded=all_critical_loaded
        )

    # ---------- Cache Proxy Utilities ----------

    @property
    def cache_root(self):
        return self.global_cache.cache_root

    def get(self, *args, **kwargs): return self.global_cache.get(*args, **kwargs)
    def get_cache(self, *args, **kwargs): return self.global_cache.get_or_generate_cache(*args, **kwargs)
    def set(self, *args, **kwargs): return self.global_cache.set(*args, **kwargs)
    def set_with_pretty_output(self, *args, **kwargs): return self.global_cache.set_with_pretty_output(*args, **kwargs)
    def deduplicate_entries(self, *args, **kwargs): return self.global_cache.deduplicate_entries(*args, **kwargs)
    def format_cache_key(self, *args, **kwargs): return self.global_cache.format_cache_key(*args, **kwargs)
    def get_or_generate_cache(self, *args, **kwargs): return self.global_cache.get_or_generate_cache(*args, **kwargs)
    def update_timestamp(self, *args, **kwargs): return self.global_cache.update_timestamp(*args, **kwargs)
    def build_cache_path(self, *args, **kwargs): return self.global_cache.build_cache_path(*args, **kwargs)
    def delete(self, *args, **kwargs): return self.global_cache.delete(*args, **kwargs)
    def exists(self, *args, **kwargs): return self.global_cache.exists(*args, **kwargs)

    # ---------- Initialization Helper ----------

    def initialize_cache_structure(self, include_optionals: bool = False):
        try:
            instances = self.instance.get_all_instance_names()
        except Exception as e:
            self.logger.log_warning(f"⚠️ Failed to retrieve instance names for cache generation: {e}")
            return

        cache_tree = {
            "series": ["retrieval", "sync", "quality", "monitoring"],
            "episodes": ["file", "monitoring", "history", "sharding", "deletion"],
            "monitoring": ["rules", "series", "scheduler"],
            "quality": ["selector", "custom_formats", "file_sizes", "adjustments"],
            "repair": ["cache", "series", "episodes", "tags"],
            "sync": ["tags"],
            "storage": ["space"],
            "cache": ["timestamps", "fallback"],
            "instances": ["status"],
            "history": [""],
            "tags": [""],
            "orchestration": ["state"]
        }

        optional_keys = [
            ("metadata", ""),
            ("errors", ""),
            ("stats", ""),
            ("pipeline", "")
        ]

        created, skipped = 0, 0

        for category, subtypes in cache_tree.items():
            for subtype in subtypes:
                for instance in instances:
                    suffix = f"_{subtype}" if subtype else ""
                    key = f"sonarr/{instance}/{category}{suffix}.json"
                    if not self.global_cache.exists(key):
                        self.global_cache.set(key, {"meta": {}, "data": []})
                        created += 1
                    else:
                        skipped += 1

        if include_optionals:
            for category, subtype in optional_keys:
                for instance in instances:
                    suffix = f"_{subtype}" if subtype else ""
                    key = f"sonarr/{instance}/{category}{suffix}.json"
                    if not self.global_cache.exists(key):
                        self.global_cache.set(key, {"meta": {}, "data": []})
                        created += 1
                    else:
                        skipped += 1

        self.logger.log_debug(f"📦 Sonarr cache structure initialized ({created} created, {skipped} existed).")

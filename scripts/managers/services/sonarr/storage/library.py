import shutil

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.config.cache_keys import CacheKeyPaths as Paths
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrStorageLibraryManager(BaseManager, ComponentManagerMixin):
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = "SonarrStorage"
        class_name = self.__class__.__name__

        if class_name.endswith("Manager"):
            self.parent_name = class_name.replace("Manager", "")
        else:
            self.parent_name = class_name

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent = self.registry.get("manager", self.parent_name)
        self.sonarr_api = kwargs.get("sonarr_api") or getattr(parent, "sonarr_api", None)
        self.logger = self.logger or getattr(parent, "logger", None)
        self.manager = kwargs.get("manager") or getattr(parent, "manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(self.manager, "dry_run", False))

        if not self.logger:
            raise ValueError(f"❌ {class_name} could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {class_name} (Parent: {self.parent_name})")

    @LoggerManager().log_function_entry
    @timeit("get_series_cache")
    def get_series_cache(self, instance: str) -> dict:
        resolved_instance = self.manager.resolve_instance(instance)
        key = f"{Paths.sonarr.SONARR_LIBRARY}.{resolved_instance}"
        data = self.global_cache.load_cache(key) or {}
        self.logger.log_debug(f"📦 Loaded series cache for {resolved_instance}: {len(data)} entries")
        return data

    @LoggerManager().log_function_entry
    @timeit("get_series_by_tvdb")
    def get_series_by_tvdb(self, tvdb_id: int, instance: str) -> dict | None:
        library = self.get_series_cache(instance)
        for series in library.values():
            if str(series.get("tvdbId")) == str(tvdb_id):
                self.logger.log_debug(f"✅ Found series with TVDB ID {tvdb_id} in {instance}")
                return series
        self.logger.log_debug(f"❌ Series with TVDB ID {tvdb_id} not found in {instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_series_by_title")
    def get_series_by_title(self, instance: str, title: str) -> dict | None:
        """
        Case-insensitive title lookup, argument order standardised to
        (instance, title) across the codebase.

        Delegates to the canonical letter-bucket series cache
        (SonarrCacheSeriesManager.get_series_by_title) when reachable, falling
        back to this manager's legacy SONARR_LIBRARY cache dict otherwise.
        """
        # Canonical source of truth: the letter-bucketed series cache.
        canon = None
        for _src in (getattr(self, "sonarr_cache", None),
                     getattr(self.manager, "sonarr_cache", None)):
            _series = getattr(_src, "series", None) if _src else None
            if _series and hasattr(_series, "get_series_by_title"):
                canon = _series
                break
        if canon is None and self.registry:
            _reg = self.registry.get("manager", "SonarrCacheSeries")
            if _reg and hasattr(_reg, "get_series_by_title"):
                canon = _reg
        if canon is not None:
            return canon.get_series_by_title(self.manager.resolve_instance(instance), title)

        # Fallback: legacy global SONARR_LIBRARY cache dict.
        library = self.get_series_cache(instance)
        title_lower = str(title or "").lower()
        for series in library.values():
            if str(series.get("title", "")).lower() == title_lower:
                self.logger.log_debug(f"✅ Found series with title '{title}' in {instance}")
                return series
        self.logger.log_debug(f"❌ Series with title '{title}' not found in {instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("is_series_in_library")
    def is_series_in_library(self, tvdb_id: int, instance: str) -> bool:
        exists = self.get_series_by_tvdb(tvdb_id, instance) is not None
        self.logger.log_debug(f"📍 Series TVDB ID {tvdb_id} present in {instance}: {exists}")
        return exists

    @LoggerManager().log_function_entry
    @timeit("list_series_by_tag")
    def list_series_by_tag(self, tag: str, instance: str) -> list:
        library = self.get_series_cache(instance)
        result = [s for s in library.values() if tag.lower() in [t.lower() for t in s.get("tags", [])]]
        self.logger.log_debug(f"🏷️ Found {len(result)} series with tag '{tag}' in {instance}")
        return result

    @LoggerManager().log_function_entry
    @timeit("get_all_series_ids")
    def get_all_series_ids(self, instance: str) -> list[int]:
        library = self.get_series_cache(instance)
        ids = [s["id"] for s in library.values() if "id" in s]
        self.logger.log_debug(f"🧾 Retrieved {len(ids)} series IDs from {instance}")
        return ids

    @LoggerManager().log_function_entry
    @timeit("get_title_by_series_id")
    def get_title_by_series_id(self, series_id: int, instance: str) -> str | None:
        library = self.get_series_cache(instance)
        for series in library.values():
            if int(series.get("id", -1)) == int(series_id):
                return series.get("title")
        return None

    @LoggerManager().log_function_entry
    @timeit("has_episode_file")
    def has_episode_file(self, series_id: int, season: int, episode: int, instance: str) -> bool:
        key = f"{Paths.sonarr.EPISODE_FILE_MAP}.{instance}"
        ep_files = self.global_cache.load_cache(key) or {}
        series_key = f"{series_id}_{season}_{episode}"
        found = series_key in ep_files
        self.logger.log_debug(f"🔎 Episode S{season}E{episode} for Series {series_id} in {instance} found: {found}")
        return found

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, instance=None):
        key = f"{Paths.sonarr.SONARR_LIBRARY}.{instance or 'default'}"
        data = cache.get(key)
        if data:
            logger.log_debug(f"📦 Warmed cache key: {key} ({len(data)} entries)")
        else:
            logger.log_warning(f"⚠️ Cache key {key} is empty or missing")

    @LoggerManager().log_function_entry
    @timeit("record_filesystem_prompt")
    def record_filesystem_prompt(self, instance: str):
        root_folders = self.sonarr_api.get_root_folders(instance)
        fs_prompted = False
        fs_shared = True
        total_size_gb = None
        results = []

        for folder in root_folders:
            path = folder.get("path")
            if not path:
                continue

            try:
                usage = shutil.disk_usage(path)
                total = usage.total
            except Exception:
                if not fs_prompted:
                    response = input(f"❓ Could not determine total space for path '{path}'. Enter total size (GB): ")
                    total_size_gb = float(response.strip())
                    fs_prompted = True
                total = int(total_size_gb * (1024 ** 3))

            results.append({"path": path, "totalSpace": total})

        # Cache these results
        cache_key = f"sonarr/manual_fs_total/{instance}"
        self.global_cache.set_with_pretty_output(cache_key, results)
        self.logger.log_info(f"💾 Cached manually entered FS totals for {instance}")

    @LoggerManager().log_function_entry
    @timeit("get_cached_total_space")
    def get_cached_total_space(self, instance: str, path: str) -> int | None:
        cache_key = f"sonarr/manual_fs_total/{instance}"
        data = self.global_cache.load_cache(cache_key) or []
        for entry in data:
            if entry.get("path") == path:
                return entry.get("totalSpace")
        return None

    @LoggerManager().log_function_entry
    @timeit("compute_percent_free")
    def compute_percent_free(self, path: str, instance: str) -> float | None:
        try:
            usage = shutil.disk_usage(path)
            return usage.free / usage.total * 100
        except Exception:
            cached_total = self.get_cached_total_space(instance, path)
            if not cached_total:
                self.logger.log_warning(f"❌ Unable to determine total space for '{path}' even via cache.")
                return None
            try:
                free_bytes = shutil.disk_usage(path).free
                return free_bytes / cached_total * 100
            except Exception:
                self.logger.log_error(f"❌ Could not determine free space for '{path}' at all.")
                return None

    @LoggerManager().log_function_entry
    @timeit("get_critical_root_folder_status")
    def get_critical_root_folder_status(self, instance: str, floor_percent: float = 15.0):
        root_folders = self.sonarr_api.get_root_folders(instance)
        below_floor = []

        for folder in root_folders:
            path = folder.get("path")
            if not path:
                continue
            percent_free = self.compute_percent_free(path, instance)
            if percent_free is None:
                continue
            if percent_free < floor_percent:
                below_floor.append((path, percent_free))

        return below_floor

    @staticmethod
    def is_pilot_episode(season: int, episode: int) -> bool:
        return season == 1 and episode == 1
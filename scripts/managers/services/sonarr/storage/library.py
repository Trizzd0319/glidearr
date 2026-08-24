import gzip
import json
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
        """``{series_id: series_record}`` for one instance — GLD-SON-27.

        ⚠️ THIS METHOD NEVER WORKED. It previously read::

            key  = f"{Paths.sonarr.SONARR_LIBRARY}.{resolved_instance}"
            data = self.global_cache.load_cache(key) or {}

        and both halves were broken:

        1. **``load_cache`` does not exist.** ``GlobalCacheManager``'s surface is
           ``get`` / ``get_json`` / ``set_json`` / ``json_exists`` /
           ``get_or_generate_cache`` / ``format_cache_key``, and ``BaseManager``
           defines no ``load_cache`` and no ``__getattr__`` delegation. Every call
           raised ``AttributeError``, and every caller here wraps in a broad
           ``except`` or a ``or {}``, so it degraded to "library is empty" in silence.
        2. **The ``<instance>`` placeholder was never substituted.** The template is
           ``"sonarr/<instance>/library"`` and this appended ``.{instance}`` instead of
           replacing, yielding ``"sonarr/<instance>/library.sonarr-720"``. Every other
           call site uses ``.replace("<instance>", instance)`` — compare
           ``key_builder.build_future_episodes_cache_key``.

        Found because the deletion archive (``GLD-RST-08``) joined pid/tvdbId through
        here and wrote 271 rows with both fields absent from every one.

        **THE LIBRARY IS SHARDED, so there is no single key to read.** It is stored one
        key per title-initial — ``sonarr/<instance>/library/<letter>`` — which lands on
        disk as ``library/a.json.gz``, ``library/z.json.gz`` and so on. The cache layer
        does no sharding of its own and ``json_handler.load_json`` opens plain text, so
        a compressed shard cannot be read through ``global_cache.get`` at all. This
        unions the shards directly, handling both ``.json`` and ``.json.gz``.

        Keyed by ``str(id)``. Every consumer in this module iterates ``.values()`` and
        none does a key lookup, so the key type is free; a string keeps it JSON-safe.
        One bad shard is skipped with a warning rather than failing the whole read —
        a partial library is far better than none for the guard paths that use it.
        """
        resolved_instance = self.manager.resolve_instance(instance)
        base_key = Paths.sonarr.SONARR_LIBRARY.replace("<instance>", resolved_instance)

        data: dict = {}
        root = None
        try:
            root = self.global_cache.key_builder.get_base_directory().joinpath(*base_key.split("/"))
        except Exception as e:
            self.logger.log_warning(f"⚠️ Could not resolve series-cache directory for "
                                    f"{resolved_instance}: {e}")

        if root is not None and root.is_dir():
            for shard in sorted(root.iterdir()):
                if not shard.is_file() or shard.name.endswith(".last_updated"):
                    continue
                try:
                    if shard.suffix == ".gz":
                        with gzip.open(shard, "rt", encoding="utf-8") as fh:
                            payload = json.load(fh)
                    elif shard.suffix == ".json":
                        with open(shard, "r", encoding="utf-8") as fh:
                            payload = json.load(fh)
                    else:
                        continue
                except Exception as e:
                    self.logger.log_warning(f"⚠️ Skipping unreadable series shard "
                                            f"{shard.name} for {resolved_instance}: {e}")
                    continue
                records = payload.values() if isinstance(payload, dict) else (payload or [])
                for series in records:
                    if isinstance(series, dict) and series.get("id") is not None:
                        data[str(series["id"])] = series

        if not data:
            # Legacy single-key layout, for any deployment that never sharded.
            try:
                legacy = self.global_cache.get(base_key)
                if isinstance(legacy, dict) and legacy:
                    data = legacy
            except Exception:
                pass

        if not data:
            self.logger.log_warning(
                f"⚠️ Series cache for {resolved_instance} is EMPTY — looked in "
                f"{root}. Guards and joins that depend on series records will "
                f"degrade silently; see GLD-SON-27.")
        else:
            self.logger.log_debug(
                f"📦 Loaded series cache for {resolved_instance}: {len(data)} entries")
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
        """Whether Sonarr holds a file for this episode — GLD-SON-27.

        Previously read ``load_cache(f"{Paths.sonarr.EPISODE_FILE_MAP}.{instance}")``,
        which was broken twice over: ``load_cache`` does not exist on
        ``GlobalCacheManager`` (see :meth:`get_series_cache`), and ``EPISODE_FILE_MAP``
        is not among the declared ``CacheKeyPaths.sonarr`` constants either. It could
        only ever raise, be swallowed by the caller, and report **False for every
        episode in the library**.

        Re-pointed at the per-series episode cache, which is plain JSON at a key that
        resolves cleanly and carries both ``hasFile`` and ``episodeFileId``. Prefers
        ``hasFile``; falls back to a non-zero ``episodeFileId`` for records written
        before that flag was populated.
        """
        try:
            resolved = self.manager.resolve_instance(instance)
            raw = self.global_cache.get(
                f"sonarr/{resolved}/episodes/by_series/{int(series_id)}")
            records = raw.values() if isinstance(raw, dict) else (raw or [])
            for e in records:
                if not isinstance(e, dict):
                    continue
                try:
                    if (int(e.get("seasonNumber", -1)) == int(season)
                            and int(e.get("episodeNumber", -1)) == int(episode)):
                        found = bool(e.get("hasFile")) or bool(e.get("episodeFileId"))
                        self.logger.log_debug(
                            f"🔎 Episode S{season}E{episode} for Series {series_id} "
                            f"in {instance} found: {found}")
                        return found
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            self.logger.log_debug(
                f"🔎 Episode lookup failed for S{season}E{episode} / {series_id} "
                f"in {instance}: {e}")
        return False

    @staticmethod
    @LoggerManager().log_function_entry
    @timeit("warm_cache")
    def warm_cache(logger, cache, instance=None):
        """Report whether the series library is populated — GLD-SON-27.

        Two prior bugs: the ``<instance>`` placeholder was never substituted (so the
        key named a literal ``<instance>`` directory), and the library is SHARDED, so
        no single ``cache.get`` could confirm it regardless. This counts shards on
        disk, which is what "warm" actually means for this cache.
        """
        inst = instance or "default"
        base = Paths.sonarr.SONARR_LIBRARY.replace("<instance>", inst)
        shards = []
        try:
            root = cache.key_builder.get_base_directory().joinpath(*base.split("/"))
            if root.is_dir():
                shards = [p for p in root.iterdir()
                          if p.is_file() and p.suffix in (".gz", ".json")]
        except Exception as e:
            logger.log_warning(f"⚠️ Could not inspect cache key {base}: {e}")
            return
        if shards:
            logger.log_debug(f"📦 Warmed cache key: {base} ({len(shards)} shard(s))")
        else:
            logger.log_warning(f"⚠️ Cache key {base} is empty or missing")

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
        # GLD-SON-27: was load_cache(), which does not exist. The key itself is fine
        # (no <instance> template) and set_with_pretty_output writes to the same one.
        data = self.global_cache.get(cache_key) or []
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
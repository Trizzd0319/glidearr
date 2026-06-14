from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesRetrievalFetchManager(BaseManager, ComponentManagerMixin):
    """
    Handles series retrieval operations from Sonarr, including live API pulls
    and cache-based lookups. Supports global and instance-aware (Sonarr) cache access.
    """

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        # 🔧 Dual-cache support
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(manager, "instance_manager", None)
        self.dry_run = kwargs.get("dry_run", getattr(manager, "dry_run", False))

        # Access Sonarr letter-bucketed series cache
        self.series_cache = getattr(self.sonarr_cache, "series", None)
        if hasattr(self.series_cache, "cache"):
            self.series_cache = self.series_cache.cache

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (with dual-cache and registry)")

    @LoggerManager().log_function_entry
    @timeit("get_all_series")
    def get_all_series(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        all_series = []
        for letter in list("abcdefghijklmnopqrstuvwxyz0123456789_"):
            all_series.extend(self.series_cache.load_letter_cache(resolved_instance, letter))

        if all_series:
            self.logger.log_info(f"📚 Loaded {len(all_series)} series from letter-bucketed cache for {resolved_instance}")
            return all_series

        self.logger.log_info(f"🌐 Fetching all series from live API for {resolved_instance}")
        return self.sonarr_api._make_request(resolved_instance, "series", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("get_all_series_chunked")
    def get_all_series_chunked(self, instance, chunk_size=200):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        all_series = []
        page = 1

        while True:
            endpoint = f"series?page={page}&pageSize={chunk_size}"
            response = self.sonarr_api._make_request(resolved_instance, endpoint, method="GET", retries=1, fallback=[])
            if not response:
                break
            all_series.extend(response)
            if len(response) < chunk_size:
                break
            page += 1

        self.logger.log_info(f"📆 Chunked fetch returned {len(all_series)} series in {resolved_instance}")
        return all_series

    @LoggerManager().log_function_entry
    @timeit("get_series_by_id")
    def get_series_by_id(self, series_id, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        for letter in list("abcdefghijklmnopqrstuvwxyz0123456789_"):
            series_list = self.series_cache.load_letter_cache(resolved_instance, letter)
            for s in series_list:
                if str(s.get("id")) == str(series_id):
                    return s
        self.logger.log_warning(f"⚠️ Series ID {series_id} not found in cache for {resolved_instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_series_by_tvdb")
    def get_series_by_tvdb(self, tvdb_id, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        response = self.sonarr_api._make_request(resolved_instance, f"series?tvdbId={tvdb_id}", fallback=[])
        if response:
            return response[0]

        self.logger.log_warning(f"⚠️ Direct TVDB lookup failed. Falling back to full scan in {resolved_instance}")
        return self.get_series_by_tvdb_id(tvdb_id, resolved_instance)

    @LoggerManager().log_function_entry
    @timeit("get_series_by_tvdb_id")
    def get_series_by_tvdb_id(self, tvdb_id: int, instance: str):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        for letter in list("abcdefghijklmnopqrstuvwxyz0123456789_"):
            series_list = self.series_cache.load_letter_cache(resolved_instance, letter)
            for s in series_list:
                if str(s.get("tvdbId")) == str(tvdb_id):
                    return s
        self.logger.log_info(f"❌ Series with TVDB ID {tvdb_id} not found in {resolved_instance}")
        return None

    @LoggerManager().log_function_entry
    @timeit("get_series_by_title")
    def get_series_by_title(self, instance, title):
        """
        Case-insensitive title lookup from the letter-bucketed series cache.

        Canonical argument order is (instance, title) — matching
        SonarrCacheSeriesManager.get_series_by_title, the single source of truth
        this delegates to. Standardised across the codebase to avoid the
        title/instance arg-swap footgun.
        """
        resolved_instance = self.instance_manager.resolve_instance(instance)

        # Canonical implementation (also (instance, title)).
        if self.series_cache and hasattr(self.series_cache, "get_series_by_title"):
            return self.series_cache.get_series_by_title(resolved_instance, title)

        # Fallback: scan the letter buckets directly.
        title_lower = str(title or "").lower()
        for letter in list("abcdefghijklmnopqrstuvwxyz0123456789_"):
            for s in self.series_cache.load_letter_cache(resolved_instance, letter):
                if str(s.get("title", "")).lower() == title_lower:
                    return s
        self.logger.log_warning(
            f"⚠️ Series with title '{title}' not found in cache for {resolved_instance}"
        )
        return None

    @LoggerManager().log_function_entry
    @timeit("get_metadata")
    def get_metadata(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved_instance, "metadata") or []

    @LoggerManager().log_function_entry
    @timeit("get_series_history")
    def get_series_history(self, instance):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        return self.sonarr_api._make_request(resolved_instance, "history", fallback=[])

    @LoggerManager().log_function_entry
    @timeit("_fetch_series_by_id")
    def _fetch_series_by_id(self, instance, series_id):
        resolved_instance = self.instance_manager.resolve_instance(instance)
        endpoint = f"series/{series_id}"
        return self.sonarr_api._make_request(resolved_instance, endpoint)

    @LoggerManager().log_function_entry
    @timeit("get_series_tags_map")
    def get_series_tags_map(self, instance):
        all_series = self.get_all_series(instance)
        return {s["id"]: s.get("tags", []) for s in all_series}

    @LoggerManager().log_function_entry
    @timeit("refresh_all_series")
    def refresh_all_series(self, instance: str = None, force: bool = False):
        """
        Fetches all series from Sonarr and synchronises the letter-bucketed cache.

        Returns
        -------
        tuple[list, bool]
            ``(series_list, from_cache)`` — ``from_cache`` is ``True`` when the
            result came entirely from disk (no live API call was made), and
            ``False`` when a live API call was performed.  Callers can use this
            flag to skip redundant validation when the cache is demonstrably fresh.

        Behaviour
        ---------
        Fresh cache (< 24 h)
            The live API call is skipped entirely; the cached library is loaded
            from disk and returned with ``from_cache=True``.  Pass
            ``force=True`` to bypass.

        Stale cache (≥ 24 h) with existing data
            Performs a **smart delta**: the full series list is fetched from the
            API (Sonarr v3 has no "changed since date" endpoint — a full fetch is
            unavoidable), then compared against the on-disk cache *per letter
            bucket*.  Only the buckets where series were added, removed, or moved
            (title-change → different bucket) are rewritten.  Returns
            ``from_cache=False``.

        No prior cache
            A full rebuild is performed — all letter bucket files are written
            from scratch.  Returns ``from_cache=False``.

        After every successful API sync the freshness timestamp is updated.
        """
        SERIES_CACHE_MAX_AGE = 86400  # 24 hours

        resolved = self.instance_manager.resolve_instance(instance)
        ts_handler = getattr(self.global_cache, "timestamp_handler", None)
        cache_mgr = getattr(self.manager, "series_cache", None)

        # ── Freshness gate: skip API call entirely when cache is recent ─────────
        if not force and ts_handler:
            try:
                is_fresh = ts_handler.is_fresh("sonarr", resolved, "series_library", SERIES_CACHE_MAX_AGE)
                if is_fresh:
                    series_cache = getattr(self.sonarr_cache, "series", None)
                    cached_count = series_cache.get_series_count(resolved) if series_cache else 0
                    if cached_count > 0:
                        age = ts_handler.get_age_seconds("sonarr", resolved, "series_library") or 0
                        age_h = age // 3600
                        age_m = (age % 3600) // 60
                        self.logger.log_info(
                            f"✅ Series cache for '{resolved}' is fresh "
                            f"({cached_count} series, age {age_h}h {age_m}m). "
                            f"Loading from disk — skipping live API call."
                        )
                        return list(series_cache.iter_all_series(resolved)), True
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Freshness check failed for '{resolved}', proceeding with API sync: {e}"
                )

        # ── Live fetch ──────────────────────────────────────────────────────────
        # Note: Sonarr v3 /series always returns the full library — there is no
        # "modified since" filter.  The delta logic below keeps disk I/O minimal.
        self.logger.log_info(f"🔄 Syncing series library from live API for '{resolved}'…")
        self.logger.log_info(
            f"🌐 Requesting full series list from '{resolved}' "
            f"— large libraries (10k+ series) may take 30–60 s, please wait…"
        )

        series = self.sonarr_api._make_request(resolved, "series", fallback=[])
        self.logger.log_info(f"📚 Retrieved {len(series)} series from '{resolved}'")

        if not series:
            self.logger.log_warning(f"⚠️ No series returned from '{resolved}' — skipping cache update.")
            return series, False

        if not cache_mgr:
            self.logger.log_warning(
                f"⚠️ No cache manager available for '{resolved}' — series fetched but not persisted."
            )
            return series, False

        # ── Choose delta vs full rebuild ────────────────────────────────────────
        if hasattr(cache_mgr, "get_all_series_ids"):
            cached_ids = cache_mgr.get_all_series_ids(resolved)
        else:
            cached_ids = set()

        if cached_ids and hasattr(cache_mgr, "delta_rebuild_series_cache"):
            # Prior cache exists → smart delta (only touch changed buckets)
            self.logger.log_info(
                f"🔀 Delta sync: {len(series)} live series vs "
                f"{len(cached_ids)} cached for '{resolved}'…"
            )
            stats = cache_mgr.delta_rebuild_series_cache(resolved, series)
            self.logger.log_info(
                f"✅ Delta complete for '{resolved}': "
                f"{stats['rewritten']} bucket(s) updated, {stats['skipped']} unchanged — "
                f"+{stats['added']} series added, -{stats['removed']} removed"
            )
        else:
            # No prior cache → full rebuild
            self.logger.log_info(
                f"📂 No prior cache found for '{resolved}' — performing full rebuild…"
            )
            cache_mgr.rebuild_bucketed_series_cache(resolved, series)
            self.logger.log_info(f"💾 Full rebuild complete for '{resolved}'")

        # ── Write freshness timestamp ────────────────────────────────────────────
        if ts_handler:
            try:
                ts_handler.update_timestamp("sonarr", resolved, "series_library")
                self.logger.log_info(f"⏱ Series library timestamp updated for '{resolved}'")
            except Exception as e:
                self.logger.log_warning(
                    f"⚠️ Failed to write series library timestamp for '{resolved}': {e}"
                )

        return series, False

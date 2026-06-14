from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesRetrievalCacheManager:
    """
    Letter-bucketed Sonarr series cache for the retrieval pipeline.

    This is a thin facade over the canonical ``SonarrCacheSeriesManager``
    (``sonarr_cache.series``). Every letter-bucket operation — load / save /
    rebuild / delta / lookup / verify — delegates to it, so there is a SINGLE
    implementation and a SINGLE in-memory bucket memo. Previously this class
    duplicated all of that logic, which drifted out of sync (different delta
    semantics, no memo) and meant writes here could leave the canonical memo
    stale.

    Only the methods that are unique to retrieval, or that need extra instance
    resolution, are defined locally below; everything else is forwarded by
    ``__getattr__``.
    """

    def __init__(self, manager=None, **kwargs):
        self.manager = manager
        self.logger = getattr(manager, "logger", None)
        self.global_cache = getattr(manager, "global_cache", None)
        self.sonarr_cache = getattr(manager, "sonarr_cache", None)

    # ── Canonical resolution / delegation ─────────────────────────────────────
    def _resolve_canon(self):
        """Resolve (and cache) the canonical SonarrCacheSeriesManager."""
        canon = self.__dict__.get("_canon_ref")
        if canon is not None:
            return canon

        sc = self.__dict__.get("sonarr_cache")
        canon = getattr(sc, "series", None) if sc is not None else None
        if canon is None or not hasattr(canon, "load_letter_cache"):
            mgr = self.__dict__.get("manager")
            reg = getattr(mgr, "registry", None) if mgr is not None else None
            if reg is None and sc is not None:
                reg = getattr(sc, "registry", None)
            if reg is not None:
                try:
                    canon = reg.get("manager", "SonarrCacheSeriesManager") or canon
                except Exception:
                    pass

        if canon is not None:
            self.__dict__["_canon_ref"] = canon
        return canon

    # Exactly the letter-bucket / lookup surface this facade forwards to the
    # canonical manager. Anything NOT in this set raises AttributeError, so the
    # facade behaves like the original plain class for everything else (no
    # accidental delegation of BaseManager lifecycle/introspection methods).
    _DELEGATED_METHODS = frozenset({
        "_library_dir", "_letter_file",
        "get_series_bucket_letter", "list_cached_letters", "clear_letter_cache",
        "load_letter_cache", "save_series_to_letter_file",
        "rebuild_bucketed_series_cache", "get_all_series_ids",
        "get_cached_series_by_id", "deduplicate_series_data",
        "summarize_cache_statistics", "iter_all_series", "get_all_series",
        "get_series_count", "get_all_titles", "get_series_by_title",
        "get_series_by_tvdb_id", "get_title_by_series_id", "remove_series",
    })

    def __getattr__(self, name):
        # Only fires for attributes not defined on this class.
        if name not in self._DELEGATED_METHODS:
            raise AttributeError(name)
        canon = self._resolve_canon()
        if canon is not None and hasattr(canon, name):
            return getattr(canon, name)
        raise AttributeError(
            f"{type(self).__name__!r}: cannot delegate {name!r} — no canonical "
            f"SonarrCacheSeriesManager available"
        )

    # ── Methods kept locally ──────────────────────────────────────────────────
    @LoggerManager().log_function_entry
    @timeit("delta_rebuild_series_cache")
    def delta_rebuild_series_cache(self, instance: str, live_series: list) -> dict:
        """
        Delegate to the canonical (content-aware) delta, but preserve the legacy
        ``"checked"`` key this manager's callers historically expected.
        """
        canon = self._resolve_canon()
        stats = canon.delta_rebuild_series_cache(instance, live_series) if canon else {}
        if isinstance(stats, dict):
            stats.setdefault("checked", stats.get("rewritten", 0) + stats.get("skipped", 0))
        return stats

    @LoggerManager().log_function_entry
    @timeit("rebuild_individual_series_caches")
    def rebuild_individual_series_caches(self, instance: str):
        """Rebuild the bucket cache from the enriched series DataFrame."""
        df = self.manager.load_enriched_series_dataframe(instance)
        canon = self._resolve_canon()
        for _, row in df.iterrows():
            canon.save_series_to_letter_file(instance, row.to_dict())
        self.logger.log_info(
            f"🔁 Rebuilt bucketed letter cache from enriched dataframe ({len(df)} series)"
        )

    @LoggerManager().log_function_entry
    @timeit("persist_letter_cache")
    def persist_letter_cache(self, instance: str):
        """
        Resolve the instance (callers may pass ``None``), then verify the
        persisted cache via the canonical manager.
        """
        instance_mgr = getattr(self.manager, "instance_manager", None)
        if instance_mgr:
            instance = instance_mgr.resolve_instance(instance)
        if not instance:
            self.logger.log_warning("⚠️ persist_letter_cache: no resolvable instance — skipping.")
            return
        canon = self._resolve_canon()
        if canon is not None:
            canon.persist_letter_cache(instance)

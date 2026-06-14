from arrapi import RadarrAPI as ArrapiRadarrAPI

from scripts.managers.factories.base_instance_manager import BaseInstanceManager
from scripts.managers.services.radarr.instance.updater import RadarrInstanceUpdaterManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class RadarrInstanceManager(BaseInstanceManager):

    # ── BaseInstanceManager interface ─────────────────────────────────────────
    def _api_class(self):           return ArrapiRadarrAPI
    def _config_key(self) -> str:   return "radarr_instances"
    def _apis_attr(self) -> str:    return "radarr_apis"
    def _service_name(self) -> str: return "Radarr"

    # ── Init ──────────────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.radarr_apis  = {}
        self.load_summary = {}

        # dry_run arrives as a kwarg from RadarrManager, but BaseManager doesn't
        # capture it — store it here so the updater child below inherits it
        # (otherwise it defaults to dry_run=False and could write live).
        self.dry_run = kwargs.get("dry_run", False)

        self.updater = RadarrInstanceUpdaterManager(
            logger=self.logger, config=self.config,
            global_cache=self.global_cache, validator=self.validator,
            registry=self.registry, manager=self, dry_run=self.dry_run,
        )

        raw_instances      = self.config.get("radarr_instances", {})
        corrected          = self.updater.apply_corrections(
            {n: "success" for n in raw_instances}
        )

        for name, cfg in corrected.items():
            if name == "default_instance":
                continue
            result = self._process_instance(name, cfg)
            if result == "recovered":
                self._confirm_and_clear_failed_flag(name, cfg)

        # Expose as the canonical API reference used by all Radarr managers
        self.radarr_api = self

        self._finalize(
            service_name="Radarr",
            flag_key="radarr.instance_manager_initialized",
        )

    # ── Instance resolution ───────────────────────────────────────────────────

    def get_all_radarr_apis(self) -> dict:
        return self.radarr_apis

    def get_radarr_api(self, instance_name: str):
        return self.radarr_apis.get(instance_name)

    def get_default_instance(self) -> dict:
        # Self-contained resolution mirroring SonarrInstanceManager.get_default_instance.
        # (Radarr is multi-instance — standard/ultra — so the configured default is
        # honoured directly rather than relying on dict-iteration order.) The previous
        # `self.config.resolve_default_instance("radarr")` call hit a method that does
        # not exist (AttributeError), silently swallowed by resolve_instance's except.
        instances    = self.config.get("radarr_instances", {})
        default      = instances.get("default_instance")
        default_name = default.get("name") if isinstance(default, dict) else default
        if isinstance(default_name, str) and default_name:
            return {"name": default_name}
        if self.radarr_apis:
            return {"name": next(iter(self.radarr_apis))}
        for name, cfg in instances.items():
            if name != "default_instance" and isinstance(cfg, dict):
                return {"name": name}
        return {}

    def resolve_instance(self, name=None) -> str:
        if isinstance(name, str) and name and name in self.radarr_apis:
            return name
        try:
            default = self.get_default_instance()
            default_name = default.get("name") if default else None
            if default_name and default_name in self.radarr_apis:
                return default_name
        except Exception:
            pass
        if self.radarr_apis:
            return next(iter(self.radarr_apis))
        return name or "default"

    def get_client(self, instance_name: str):
        return self.radarr_apis.get(self.resolve_instance(instance_name))

    # ── Freshness-gated movie library ─────────────────────────────────────────

    @timeit("get_movie_library")
    def get_movie_library(self, instance, max_age_s: int = 0, global_cache=None):
        """
        Full movie list for ``instance``, preferring the persistent on-disk snapshot
        (``radarr.movies.{instance}.full`` + a "movie_library" timestamp) when it is
        younger than ``max_age_s``, else a fresh live GET /movie.

        This is the single freshness decision for the whole run: it warms the
        in-process collection memo, so every later bare GET /movie (the repair scans
        and the orchestration enrichment, which all funnel through _make_request)
        transparently reuses the same snapshot. So warming this once — from disk when
        fresh, from a live fetch when stale (which also re-stamps for next run) —
        governs the entire run with one decision.

        ``max_age_s=0`` (or no global_cache / timestamp handler) always fetches live,
        i.e. exactly today's behavior. Read-only w.r.t. Radarr → identical in dry_run;
        the cache layer is strictly best-effort (a cache error never breaks the fetch).
        Mirrors Sonarr's series_library freshness gate (series/retrieval/fetch.py).
        """
        resolved = self._resolve_instance_name(instance)
        service  = self._service_name()
        key      = f"radarr.movies.{resolved}.full"

        # 1. In-process memo (within-run dedup / an earlier warm). Returns a copy.
        hit, gen0 = self._collection_cache_lookup(service, resolved, "movie")
        if hit is not None:
            return hit

        gc = global_cache if global_cache is not None else getattr(self, "global_cache", None)
        ts = getattr(gc, "timestamp_handler", None) if gc is not None else None

        # 2. Persistent on-disk snapshot, if stamped fresh enough.
        if max_age_s and gc is not None and ts is not None:
            try:
                if ts.is_fresh("radarr", resolved, "movie_library", max_age_s):
                    cached = gc.get(key)
                    if isinstance(cached, list) and cached:
                        # Warm the memo so repair + orchestration reuse this snapshot.
                        self._collection_cache_store(service, resolved, "movie", cached, gen0)
                        age = ts.get_age_seconds("radarr", resolved, "movie_library") or 0
                        self.logger.log_info(
                            f"[Radarr] movie library '{resolved}' fresh on disk "
                            f"(age {age // 60}m {age % 60}s ≤ {max_age_s}s) — "
                            f"skipped live GET /movie ({len(cached)} movies)"
                        )
                        return list(cached)
            except Exception as e:
                self.logger.log_debug(f"[Radarr] movie cache freshness check failed: {e}")

        # 3. Stale / missing / disabled → live fetch (memo-stored inside _make_request),
        #    then refresh the persistent snapshot + stamp it for the next run.
        movies = self._make_request(instance, "movie", fallback=[]) or []
        if movies and gc is not None:
            try:
                gc.set(key, movies)
                if ts is not None:
                    ts.update_timestamp("radarr", resolved, "movie_library")
            except Exception as e:
                self.logger.log_debug(f"[Radarr] movie cache persist/stamp failed: {e}")
        return movies

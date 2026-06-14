from collections import defaultdict

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrSeriesSyncTautulliManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeries"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None,
                 cache_manager=None, tautulli_cache=None, manager=None, **kwargs):

        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = manager or self.registry.get("manager", self.parent_name)

        # ✅ Dual-cache support
        self.global_cache = global_cache or getattr(self.manager, "global_cache", None)
        self.sonarr_cache = cache_manager or getattr(self.manager, "sonarr_cache", None)
        self.tautulli_cache = tautulli_cache or getattr(self.manager, "tautulli_cache", None)

        self.sonarr_api = kwargs.get("sonarr_api") or getattr(self.manager, "sonarr_api", None)
        self.instance_manager = kwargs.get("instance_manager") or getattr(self.manager, "instance_manager", None)

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def _resolve_tautulli_manager(self):
        """Resolve or initialize the TautulliManager."""
        tautulli = self.registry.get("manager", "TautulliManager")
        if tautulli:
            return tautulli

        from scripts.managers.services.tautulli import TautulliManager
        self.logger.log_info("🔄 Initializing TautulliManager...")

        tautulli = TautulliManager(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            registry=self.registry
        )
        tautulli.check_instances()
        tautulli.process_all_data()
        self.registry.set("manager", "TautulliManager", tautulli)
        return tautulli

    @LoggerManager().log_function_entry
    @timeit("fetch_recent_titles")
    def _fetch_recent_titles_from_tautulli(self, max_entries=1000):
        tautulli = self._resolve_tautulli_manager()
        recent_titles = defaultdict(lambda: {"count": 0, "last_played": None, "libraries": set()})

        for instance_name in tautulli.instances.get_instance_names():
            api = tautulli.instances.get_api(instance_name)
            if not api:
                continue

            response = api.get_history(length=max_entries)
            entries = (response.get("response") or {}).get("data", []) if response else []

            for entry in entries:
                if entry.get("media_type") != "episode":
                    continue
                title = entry.get("grandparent_title")
                played_at = entry.get("date")
                library = entry.get("library_name")

                if title:
                    recent_titles[title]["count"] += 1
                    if played_at:
                        prev = recent_titles[title]["last_played"]
                        recent_titles[title]["last_played"] = max(prev, played_at) if prev else played_at
                    if library:
                        recent_titles[title]["libraries"].add(library)

        # Convert sets to lists for JSON serialization
        for title, data in recent_titles.items():
            data["libraries"] = list(data["libraries"])

        return recent_titles

    @LoggerManager().log_function_entry
    @timeit("update_sync_caches")
    def update_sonarr_sync_caches_from_tautulli(self, sonarr_instance_name: str) -> dict:
        recent_titles = self._fetch_recent_titles_from_tautulli()
        all_sonarr_titles = self.sonarr_cache.series.get_all_titles(sonarr_instance_name)

        viewed, missing, rewatch = {}, [], {}

        for title, meta in recent_titles.items():
            if title in all_sonarr_titles:
                viewed[title] = meta
                if meta["count"] >= 3:
                    rewatch[title] = meta
            else:
                missing.append(title)

        # Save to SonarrCache
        self.sonarr_cache.set(f"sonarr/{sonarr_instance_name}/sync/tautulli_viewed", viewed)
        self.sonarr_cache.set(f"sonarr/{sonarr_instance_name}/sync/tautulli_missing", missing)
        self.sonarr_cache.set(f"sonarr/{sonarr_instance_name}/sync/tautulli_rewatches", rewatch)

        self.logger.log_info(
            f"✅ Synced Tautulli data to Sonarr [{sonarr_instance_name}]: "
            f"{len(viewed)} viewed, {len(missing)} missing, {len(rewatch)} rewatch candidates."
        )
        return {
            "viewed": len(viewed),
            "missing": len(missing),
            "rewatch": len(rewatch)
        }

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.managers.services.trakt.api       import TraktAPIManager
from scripts.managers.services.trakt.instances import TraktInstanceManager
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent       = kwargs.get("manager")
        self.dry_run = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)

        # Optional cross-service handles (injected by caller, never instantiated here)
        self.sonarr_apis  = kwargs.get("sonarr_apis", {})
        self.ml_manager   = kwargs.get("ml_manager")
        self.tautulli_api = kwargs.get("tautulli_api")
        self.plex_api     = kwargs.get("plex_api")

        base_kwargs = dict(
            logger=self.logger,
            config=self.config,
            global_cache=self.global_cache,
            validator=self.validator,
            registry=self.registry,
            manager=self,
            dry_run=self.dry_run,
        )

        # ── Instance manager (validates credentials / token) ──────────────
        self.instance_manager = TraktInstanceManager(**base_kwargs)
        if not self.instance_manager.register_and_validate():
            raise RuntimeError("[TraktManager] Instance validation failed — aborting setup.")

        # ── API manager (HTTP layer + all sub-managers) ───────────────────
        self.trakt_api = self._singleton(
            "trakt_api_manager",
            lambda: TraktAPIManager(**base_kwargs),
        )

        self.logger.log_debug("[TraktManager] Initialized successfully.")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @LoggerManager().log_function_entry
    @timeit("prepare")
    def prepare(self):
        self.logger.log_debug("[TraktManager] No components to pre-load at this time.")

    @LoggerManager().log_function_entry
    @timeit("run")
    def run(self):
        self.logger.log_info("[TraktManager] Running system-wide sync...")

        self.trakt_api.history.get_full_watch_history()
        self.trakt_api.history.get_full_movie_history_cached()
        self.trakt_api.ratings.get_user_ratings()
        self.trakt_api.recommendations.get_recommendations_shows()
        self.trakt_api.recommendations.get_recommendations_movies()
        self.trakt_api.watchlist.get_watchlist_shows()

        # Fetch progress once, then reuse it for auto-rating (avoids a second
        # round of ~150 per-show API calls inside auto_rate_watched_shows).
        progress = self.trakt_api.progress.get_combined_progress_watched()
        self.trakt_api.ratings.auto_rate_watched_shows(progress_map=progress)

        self.logger.log_info("[TraktManager] System sync complete.")

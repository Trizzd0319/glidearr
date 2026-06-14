from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktAnalyticsManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent         = kwargs.get("manager")
        self.dry_run   = kwargs.get("dry_run", getattr(parent, "dry_run", False) if parent else False)
        self.trakt_api = kwargs.get("trakt_api")

    # ── Stats / summaries ─────────────────────────────────────────────────

    def fetch_user_stats(self):
        return self._get("users/stats")

    def fetch_user_history_summary(self):
        return self._get("sync/history")

    def fetch_user_watchlist_summary(self):
        return self._get("sync/watchlist")

    # ── Genre / actor analysis ────────────────────────────────────────────

    def analyze_genres(self) -> dict:
        self.logger.log_info("[TraktAnalytics] Analyzing genres...")
        genre_counts: dict = {}
        history = self._history_manager()
        if not history:
            return genre_counts

        latest_watched = history.get_history_grouped_by_series()
        for tvdb_id in latest_watched:
            metadata = self._get(f"search/tvdb/{tvdb_id}?type=show")
            if metadata and isinstance(metadata, list) and metadata:
                show_meta = metadata[0].get("show", {})
                for genre in show_meta.get("genres", []):
                    genre_counts[genre] = genre_counts.get(genre, 0) + 1

        top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)
        for genre, count in top_genres:
            self.logger.log_debug(f"[TraktAnalytics] Genre: {genre} — {count} shows")
        return dict(top_genres)

    def analyze_actors(self) -> dict:
        self.logger.log_info("[TraktAnalytics] Analyzing actors...")
        actor_counts: dict = {}
        history = self._history_manager()
        if not history:
            return actor_counts

        for tvdb_id in history.get_history_grouped_by_series():
            people = self._get(f"search/tvdb/{tvdb_id}/people")
            if not people:
                continue
            for actor in (people.get("cast") or []):
                name = (actor.get("person") or {}).get("name")
                if name:
                    actor_counts[name] = actor_counts.get(name, 0) + 1

        top_actors = sorted(actor_counts.items(), key=lambda x: x[1], reverse=True)
        for actor, count in top_actors:
            self.logger.log_debug(f"[TraktAnalytics] Actor: {actor} — {count} appearances")
        return dict(top_actors)

    # ── Private ───────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params=None):
        if not self.trakt_api:
            return None
        return self.trakt_api._make_request(endpoint, params=params)

    def _history_manager(self):
        """Return the sibling TraktHistoryManager via the shared TraktAPIManager."""
        if self.trakt_api and hasattr(self.trakt_api, "history"):
            return self.trakt_api.history
        return None

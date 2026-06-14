from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktLookupManager(BaseManager, ComponentManagerMixin):
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

    # ── ID lookups ────────────────────────────────────────────────────────

    def lookup_trakt_id_from_tvdb(self, tvdb_id):
        return self._get(f"search/tvdb/{tvdb_id}")

    def get_metadata_from_tvdb(self, tvdb_id):
        return self._get(f"search/tvdb/{tvdb_id}?type=show")

    def search_show_by_title_and_year(self, title: str, year=None):
        params = {"query": title}
        if year:
            params["year"] = year
        return self._get("search/show", params=params)

    # ── Show lookups ──────────────────────────────────────────────────────

    def get_aliases_by_show(self, show_id):
        return self._get(f"shows/{show_id}/aliases")

    def get_seasons_by_show(self, show_id):
        return self._get(f"shows/{show_id}/seasons")

    def get_episodes_by_season(self, show_id, season_number: int):
        return self._get(f"shows/{show_id}/seasons/{season_number}")

    def get_related_shows(self, show_id):
        return self._get(f"shows/{show_id}/related")

    def get_trending_shows(self):
        return self._get("shows/trending")

    def get_popular_shows(self):
        return self._get("shows/popular")

    def get_anticipated_shows(self):
        return self._get("shows/anticipated")

    # ── People / crew lookups ─────────────────────────────────────────────

    def lookup_people_by_show(self, show_id):
        return self._get(f"shows/{show_id}/people")

    def lookup_directors_by_show(self, show_id):
        crew = self.lookup_people_by_show(show_id)
        return [p for p in (((crew or {}).get("crew") or {}).get("directing") or [])
                if p.get("job") == "Director"]

    def lookup_producers_by_show(self, show_id):
        crew = self.lookup_people_by_show(show_id)
        return [p for p in (((crew or {}).get("crew") or {}).get("production") or [])
                if p.get("job") == "Producer"]

    def lookup_composers_by_show(self, show_id):
        crew = self.lookup_people_by_show(show_id)
        return [p for p in (((crew or {}).get("crew") or {}).get("sound") or [])
                if p.get("job") == "Composer"]

    def get_writers_by_show(self, show_id):
        crew = self.lookup_people_by_show(show_id)
        return ((crew or {}).get("crew") or {}).get("writing") or []

    # ── User lookups ──────────────────────────────────────────────────────

    def get_user_lists(self, username: str):
        return self._get(f"users/{username}/lists")

    def get_list_items(self, username: str, list_slug: str):
        return self._get(f"users/{username}/lists/{list_slug}/items")

    def get_user_collections(self, username: str):
        return self._get(f"users/{username}/collection/shows")

    def get_user_followers(self, username: str):
        return self._get(f"users/{username}/followers")

    def get_user_following(self, username: str):
        return self._get(f"users/{username}/following")

    def get_user_watch_history(self, username: str):
        return self._get(f"users/{username}/history")

    def get_user_ratings(self, username: str):
        return self._get(f"users/{username}/ratings")

    # ── Engagement ────────────────────────────────────────────────────────

    def get_watchers_by_show(self, show_id):
        return self._get(f"shows/{show_id}/watching")

    def get_comments_by_show(self, show_id):
        return self._get(f"shows/{show_id}/comments")

    # ── Private ───────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params=None):
        if not self.trakt_api:
            return None
        return self.trakt_api._make_request(endpoint, params=params)

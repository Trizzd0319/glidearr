import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class SonarrEpisodesRetrievalTVDBManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrEpisodesRetrieval"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        self.manager = kwargs.get("manager") or self.registry.get("manager", self.parent_name)
        self.logger = self.logger or getattr(self.manager, "logger", None)
        self.token = self._load_token()
        self.base_url = "https://api4.thetvdb.com/v4"

        if not self.logger:
            raise ValueError("❌ SonarrEpisodesRetrievalTVDBManager could not initialize without logger")

        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def _load_token(self):
        token = (self.config.get("tvdb") or {}).get("token")
        if not token:
            self.logger.log_warning("⚠️ Missing TVDB token in config. TVDB enrichment will be skipped.")
        return token

    def _make_request(self, endpoint, params=None):
        if not self.token:
            return {}

        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            url = f"{self.base_url}/{endpoint}"
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json().get("data", {})
        except Exception as e:
            self.logger.log_warning(f"⚠️ TVDB request to '{endpoint}' failed: {e}")
            return {}

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_series")
    def fetch_tvdb_series(self, tvdb_id=None, fallback_title=None):
        if tvdb_id:
            return self._make_request(f"series/{tvdb_id}")

        if fallback_title:
            search = self._make_request("search", params={"q": fallback_title, "type": "series"})
            if isinstance(search, list) and search:
                best = search[0]
                return self._make_request(f"series/{best.get('id')}")

        self.logger.log_warning("⚠️ Unable to retrieve TVDB series data: no ID or title fallback.")
        return {}

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_episode")
    def fetch_tvdb_episode(self, episode_id):
        return self._make_request(f"episodes/{episode_id}")

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_episodes_by_series")
    def fetch_tvdb_episodes_by_series(self, tvdb_id):
        return self._make_request(f"series/{tvdb_id}/episodes") or []

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_artworks")
    def fetch_tvdb_artworks(self, tvdb_id):
        return self._make_request(f"series/{tvdb_id}/artworks") or []

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_season_episodes")
    def fetch_tvdb_season_episodes(self, tvdb_id, season_type="default"):
        params = {"seasonType": season_type}
        return self._make_request(f"series/{tvdb_id}/episodes", params=params) or []

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_series_extended")
    def fetch_tvdb_series_extended(self, tvdb_id):
        return self._make_request(f"series/{tvdb_id}/extended") or {}

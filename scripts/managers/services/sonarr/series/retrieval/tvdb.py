import requests

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager
from scripts.support.utilities.registry import RegistryHelper


class SonarrSeriesRetrievalTVDBManager(BaseManager, ComponentManagerMixin):
    parent_name = "SonarrSeriesRetrieval"

    def __init__(self, logger=None, config=None, global_cache=None, validator=None, registry=None, **kwargs):
        self.parent_name = self.__class__.__name__.replace("Manager", "")
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)

        # 🔧 Dual-cache setup
        manager = kwargs.get("manager") or {}
        self.manager = manager
        self.sonarr_cache = kwargs.get("sonarr_cache") or getattr(manager, "sonarr_cache", None)
        self.global_cache = global_cache or getattr(manager, "global_cache", None)

        self.logger = self.logger or getattr(manager, "logger", LoggerManager())
        self.config = self.config or getattr(manager, "config", {})

        self.token = (self.config.get("tvdb") or {}).get("token")
        self.base_url = "https://api4.thetvdb.com/v4"
        self.session = requests.Session()

        if self.token:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

        self.register()
        self.logger.log_debug(f"🧰 Initialized {self.__class__.__name__} (Parent: {self.parent_name})")

    def _safe_logger(self, msg, logger=None):
        logger = logger or getattr(self.manager, "logger", None) or self.logger
        try:
            logger.log_warning(msg)
        except Exception:
            print(f"[TVDB] {msg}")

    def _safe_request(self, endpoint, params=None):
        try:
            url = f"{self.base_url}/{endpoint}"
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json().get("data", {})
        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:
                self._safe_logger("🔒 Unauthorized. TVDB token may be expired.")
            elif response.status_code == 429:
                self._safe_logger("⏳ Rate limit exceeded. Consider backoff strategy.")
            else:
                self._safe_logger(f"❌ HTTP error: {e}")
        except Exception as e:
            self._safe_logger(f"⚠️ Failed request to {endpoint}: {e}")
        return {}

    @LoggerManager().log_function_entry
    @timeit("fetch_tvdb_data")
    def fetch_tvdb_data(self, tvdb_id=None, fallback_title=None, token=None, logger=None):
        token = token or self.token
        if not token:
            self._safe_logger("⚠️ TVDB token missing.", logger)
            return {}

        cache_key = f"tvdb/{tvdb_id or fallback_title}.json"
        if self.global_cache and self.global_cache.exists(cache_key):
            cached = self.global_cache.get(cache_key)
            self.logger.log_debug(f"📦 Using cached TVDB data for: {tvdb_id or fallback_title}")
            return cached

        # Fallback search
        if not tvdb_id and fallback_title:
            search_data = self._safe_request("search", params={"q": fallback_title, "type": "series"})
            if isinstance(search_data, list) and search_data:
                best = search_data[0]
                tvdb_id = best.get("id")
                self.logger.log_info(f"🔁 Fallback resolved '{fallback_title}' → TVDB ID {tvdb_id}")
            else:
                self._safe_logger(f"❌ No results for title fallback: {fallback_title}", logger)
                return {}

        if not tvdb_id:
            self._safe_logger("❌ Cannot resolve a valid TVDB ID.", logger)
            return {}

        # Pull core and extended data
        series_data = self._safe_request(f"series/{tvdb_id}")
        if not series_data:
            self._safe_logger(f"❌ Could not retrieve core series data for ID {tvdb_id}", logger)
            return {}

        extended_data = self._safe_request(f"series/{tvdb_id}/extended")
        seasons = self._safe_request(f"series/{tvdb_id}/episodes/official")

        result = {
            "tvdb_id": tvdb_id,
            "tvdb_name": series_data.get("name"),
            "tvdb_slug": series_data.get("slug"),
            "tvdb_overview": series_data.get("overview"),
            "tvdb_first_aired": series_data.get("firstAired"),
            "tvdb_last_aired": series_data.get("lastAired"),
            "tvdb_year": series_data.get("year"),
            "tvdb_image": series_data.get("image"),
            "tvdb_status": (series_data.get("status") or {}).get("name"),
            "tvdb_runtime": series_data.get("averageRuntime"),
            "tvdb_country": series_data.get("originalCountry"),
            "tvdb_language": series_data.get("originalLanguage"),
            "tvdb_aliases": series_data.get("aliases", []),
            "tvdb_genres": extended_data.get("genres", []),
            "tvdb_studios": extended_data.get("studios", []),
            "tvdb_airs_day": (series_data.get("airs") or {}).get("day"),
            "tvdb_airs_time": (series_data.get("airs") or {}).get("time"),
            "tvdb_airs_timezone": (series_data.get("airs") or {}).get("timeZone"),
            "tvdb_seasons": seasons
        }

        if self.global_cache:
            self.global_cache.set(cache_key, result)

        return result

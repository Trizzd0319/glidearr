from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktSyncManager(BaseManager, ComponentManagerMixin):
    """
    TraktSyncManager
    ================
    Provides Trakt collection / watched data fetching.
    """

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

    # ── Trakt data ────────────────────────────────────────────────────────

    def get_collection(self) -> list:
        self.logger.log_info("[TraktSync] Fetching Trakt collection...")
        return self._get("sync/collection/shows") or []

    def get_watched(self) -> list:
        self.logger.log_info("[TraktSync] Fetching Trakt watched history...")
        return self._get("sync/watched/shows") or []

    def get_watched_episodes(self, show_trakt_id) -> dict:
        return self._get(
            f"shows/{show_trakt_id}/progress/watched",
            params={"hidden": False, "specials": False},
        )

    # ── History helpers ───────────────────────────────────────────────────

    def last_watched_within_threshold(self, tvdb_id, days: int = 90) -> bool:
        last = self.get_last_watched_episode(tvdb_id)
        if not last or not last.get("watched_at"):
            return False
        try:
            watched_at = datetime.fromisoformat(last["watched_at"].replace("Z", "+00:00"))
            delta = datetime.now(watched_at.tzinfo) - watched_at
            return delta.days <= days
        except Exception:
            return False

    def get_last_watched_episode(self, tvdb_id) -> dict | None:
        history = self._get_episode_history(tvdb_id)
        if not history:
            return None
        try:
            return sorted(history, key=lambda x: x.get("watched_at", ""), reverse=True)[0]
        except Exception:
            return None

    # ── Private ───────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params=None):
        if not self.trakt_api:
            return None
        return self.trakt_api._make_request(endpoint, params=params)

    def _get_episode_history(self, tvdb_id) -> list:
        if self.trakt_api and hasattr(self.trakt_api, "history") and self.trakt_api.history:
            grouped = self.trakt_api.history.get_history_grouped_by_series()
            return grouped.get(tvdb_id, [])
        return []

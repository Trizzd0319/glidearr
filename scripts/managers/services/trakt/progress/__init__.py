from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

_PROGRESS_TTL = 86_400   # 24 hours in seconds


class TraktProgressManager(BaseManager, ComponentManagerMixin):
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

        trakt_cfg  = (self.config.get("trakt", {}) if self.config else {})
        self.user  = trakt_cfg.get("username", "default")

    # ── Per-show progress ─────────────────────────────────────────────────

    def get_progress_watched(self, show_id):
        return self._get(
            f"shows/{show_id}/progress/watched",
            params={"hidden": "false", "specials": "false", "count_specials": "true"},
        )

    def get_progress_collected(self, show_id):
        return self._get(
            f"shows/{show_id}/progress/collected",
            params={"hidden": "false", "specials": "false", "count_specials": "true"},
        )

    # ── Bulk progress (cached) ────────────────────────────────────────────

    def get_combined_progress_watched(self) -> dict:
        """
        Return watched progress for every show in the user's Trakt watched list.

        Result is cached for 24 hours so the ~150 per-show API calls only fire
        once per day.  Force a refresh by calling invalidate_progress_cache()
        before this method.
        """
        if self.global_cache:
            return self.global_cache.get_or_generate_cache(
                key=f"trakt/{self.user}/progress/watched_combined",
                generator_function=self._fetch_combined_progress_watched,
                expiration_time=_PROGRESS_TTL,
            )
        return self._fetch_combined_progress_watched()

    def get_combined_progress_collected(self) -> dict:
        """
        Return collected progress for every show in the user's Trakt collection.
        Cached for 24 hours.
        """
        if self.global_cache:
            return self.global_cache.get_or_generate_cache(
                key=f"trakt/{self.user}/progress/collected_combined",
                generator_function=self._fetch_combined_progress_collected,
                expiration_time=_PROGRESS_TTL,
            )
        return self._fetch_combined_progress_collected()

    def invalidate_progress_cache(self) -> None:
        """Force the next get_combined_progress_watched() call to re-fetch from Trakt."""
        if self.global_cache:
            self.global_cache.invalidate_cache_key(
                f"trakt/{self.user}/progress/watched_combined"
            )
            self.logger.log_debug("[TraktProgress] Progress cache invalidated.")

    # ── Recent show IDs ───────────────────────────────────────────────────

    def get_recent_watched_show_ids(self, days: int = 30) -> set:
        cutoff     = datetime.utcnow() - timedelta(days=days)
        watched    = self._get_user_watched_shows()
        recent_ids: set = set()
        for item in watched:
            last_watched = item.get("last_watched_at")
            show_id      = ((item.get("show") or {}).get("ids") or {}).get("slug")
            if show_id and last_watched:
                try:
                    last_time = datetime.strptime(last_watched, "%Y-%m-%dT%H:%M:%S.%fZ")
                    if last_time > cutoff:
                        recent_ids.add(show_id)
                except ValueError:
                    pass
        return recent_ids

    # ── Private ───────────────────────────────────────────────────────────

    def _fetch_combined_progress(self, shows: list, getter) -> dict:
        """
        Fetch per-show progress concurrently.

        The per-show GETs are independent, so they run in a small thread pool
        instead of a ~150-call sequential loop. Trakt rate limiting is enforced
        centrally and thread-safely by TraktAPIManager._throttle().
        """
        show_ids = [
            ((s.get("show") or {}).get("ids") or {}).get("slug") for s in shows
        ]
        show_ids = [sid for sid in show_ids if sid]

        out: dict = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {executor.submit(getter, sid): sid for sid in show_ids}
            for future in as_completed(future_map):
                sid = future_map[future]
                try:
                    out[sid] = future.result()
                except Exception as e:
                    self.logger.log_warning(
                        f"[TraktProgress] progress fetch failed for '{sid}': {e}"
                    )
        return out

    def _fetch_combined_progress_watched(self) -> dict:
        all_watched = self._fetch_combined_progress(
            self._get_user_watched_shows(), self.get_progress_watched
        )
        self.logger.log_info(
            f"[TraktProgress] Fetched watched progress for {len(all_watched)} shows."
        )
        return all_watched

    def _fetch_combined_progress_collected(self) -> dict:
        all_collected = self._fetch_combined_progress(
            self._get_collected_shows(), self.get_progress_collected
        )
        self.logger.log_info(
            f"[TraktProgress] Fetched collected progress for {len(all_collected)} shows."
        )
        return all_collected

    def _get(self, endpoint: str, params=None):
        if not self.trakt_api:
            return None
        return self.trakt_api._make_request(endpoint, params=params)

    def _get_user_watched_shows(self) -> list:
        return self._get("sync/watched/shows") or []

    def _get_collected_shows(self) -> list:
        return self._get("sync/collection/shows") or []

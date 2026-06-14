from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.machine_learning.affinity.genre_affinity import (
    aggregate_affinity,
    per_user_affinity,
)


class TautulliUsersManager(BaseManager):
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.tautulli_api = kwargs.get("tautulli_api")

    def get_all_users(self) -> list:
        """Return list of user dicts from Tautulli."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_users()
        users = ((resp or {}).get("response") or {}).get("data", []) or []
        self.logger.log_info(f"[TautulliUsers] {len(users)} users retrieved.")
        return users

    def get_user_watch_time_stats(self, user_id) -> list:
        """Real-time watch time stats for a single user."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_user_watch_time_stats(user_id=user_id)
        return ((resp or {}).get("response") or {}).get("data", []) or []

    def get_user_player_stats(self, user_id) -> list:
        """Real-time player stats for a single user."""
        if not self.tautulli_api:
            return []
        resp = self.tautulli_api.get_user_player_stats(user_id=user_id)
        return ((resp or {}).get("response") or {}).get("data", []) or []

    def _affinity_half_life(self):
        """Optional recency half-life (days) for affinity decay — config
        ``scoring.affinity_half_life_days``. None/0 = legacy raw counts (default)."""
        return ((self.config or {}).get("scoring", {}) or {}).get("affinity_half_life_days")

    def _compute_affinity_from_entries(
        self, history_entries: list, metadata_index: dict
    ) -> dict:
        """Core affinity computation. Delegates to the brain
        (machine_learning.affinity.genre_affinity.aggregate_affinity) — kept as a
        thin method for internal / back-compat callers. Pure."""
        return aggregate_affinity(history_entries, metadata_index,
                                  half_life_days=self._affinity_half_life())

    def compute_genre_affinity(self, history_entries: list, metadata_index: dict) -> dict:
        """Household genre/actor/director affinity from pre-fetched history and
        metadata. The COMPUTATION lives in the brain (genre_affinity.aggregate_affinity);
        the service keeps FETCH + this summary log + the cache-write (TautulliManager)."""
        result = aggregate_affinity(history_entries, metadata_index,
                                    half_life_days=self._affinity_half_life())
        self.logger.log_info(
            f"[TautulliUsers] Genre affinity: "
            f"{len(result.get('genres', {}))} genres, "
            f"{len(result.get('actors', {}))} actors, "
            f"{len(result.get('directors', {}))} directors."
        )
        return result

    def compute_per_user_genre_affinity(
        self,
        history_entries: list,
        metadata_index: dict,
        user_list: list,
    ) -> dict:
        """Per-user affinity matrices — one signal per Tautulli account. The
        grouping + computation live in the brain
        (genre_affinity.per_user_affinity); the service keeps the logging.

        Returns a dict keyed by username (users with zero matching history entries
        are omitted)::

            {"Trizzd": {"genres": {...}, "actors": {...}, ...}, "Aiden": {...}}
        """
        result = per_user_affinity(history_entries, metadata_index, user_list,
                                   half_life_days=self._affinity_half_life())
        for username, affinity in result.items():
            self.logger.log_debug(
                f"[TautulliUsers] Per-user affinity for '{username}': "
                f"{len(affinity.get('genres', {}))} genres."
            )
        self.logger.log_info(
            f"[TautulliUsers] Per-user affinity computed for {len(result)} user(s)."
        )
        return result

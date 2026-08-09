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
        ``scoring.affinity_half_life_days``. None/0 = legacy raw counts (default).

        ⚠️  SETTING THIS IS AN AXIS TRANSLATION — IT IS NOT A LOCAL CHANGE.

        Decay reweights every affinity contribution by ``exp(-age_days/half_life)``,
        which shifts the whole ``watchability_score`` distribution downward (older
        watches stop counting at full weight). ``likelihood`` consumes that score at
        GAIN 1.0, so every boundary calibrated against the current distribution moves
        out from under itself the moment this is non-zero.

        The last time the axis translated — Group D v2 replacing a near-constant +12
        bonus with a transcode-risk penalty — it cost a coordinated re-anchor:

          * the DELETE family 20 -> 17 (``tv_delete_ceiling``, ``series_demote``, and
            the movie delete floor) — see machine_learning/thresholds/registry.py;
          * ``likelihood.untouched_base`` 12 -> 25, after untouched titles reaching
            1080p collapsed 456 -> 8 (-98.2%);
          * the monitor threshold deliberately LEFT at 35, because it is crossed only
            by file-owning series and stubs never reach it either way (the asymmetry
            is documented in sonarr/series/quality.py — do not "fix" it).

        That collapse was caught by manual measurement, NOT by any check: there is
        still no axis-drift detector (GLD-LIK-01). So before enabling this:

          1. re-measure the score distribution with decay on;
          2. re-anchor the delete family and ``untouched_base`` against it;
          3. prefer ``thresholds`` percentile mode where available — it is immune to
             translation (measured 0.0% / +4.9% drift vs -98.2% for absolute cutoffs).

        Evidence for the half-life itself is thin (n~931, n_pos<<100), so an
        aggressive value discards most of the signal it is meant to weight. See D32.
        """
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

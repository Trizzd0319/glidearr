"""
trakt/lists — the operator's own Trakt lists. DEAD, and its summary is half-empty.
================================================================================
Constructed by nothing (removed from trakt/api's sub-manager map, session 94).
Unlike sync/universe/analytics this is NOT purely redundant — see "worth keeping"
below — but nothing reaches it and one method returns a partly-unpopulated shape.

🔴 ``_generate_unified_summary`` DECLARES FIVE FIELDS AND WRITES THREE.
``title`` ("") and ``in_library`` (False) are set in the defaultdict factory and
then never assigned by anything — so every entry reports an empty title and claims
the show is not in the library, whatever the truth. A caller trusting either would
be reading a constant, not data. Only ``lists``, ``episodes_watched`` and
``last_watched`` are real.

⚠️ It also returns ``lists`` as a ``set()``, which is not JSON-serialisable —
caching the result would depend on ``make_json_safe`` coercing it.

⚠️ And the summary quietly includes EVERY watched series, not just list members:
``show_data`` is a defaultdict, so ``show_data[tvdb_id]`` in the history loop CREATES
an entry for a series that is on no list at all (with ``lists`` left empty).
Defensible for a "unified" view, but it means most rows are watch-stats-only.

WORTH KEEPING (the reason this is documented rather than simply deleted):
``get_user_lists`` / ``get_list_items`` read the operator's OWN Trakt lists, and
nothing else in the repo does. The enrich daemon's ``lists`` scope is a different
thing (which lists a TITLE appears on); acquisition sources are watchlist +
recommendations only; the playlist universes come from mdblist. "Acquire from my
Trakt list X" is a real gap this could fill.

SUPERSEDED, though: ``get_collected_shows`` duplicates writeback/trakt_collection's
own collection fetch, and ``get_user_watched`` duplicates trakt/history.

See GLD-TRK-20.
"""
from collections import defaultdict
from datetime import datetime

from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager


class TraktListsManager(BaseManager, ComponentManagerMixin):
    parent_name = "TraktManager"

    @LoggerManager().log_function_entry
    @timeit("__init__")
    def __init__(self, logger=None, config=None, global_cache=None,
                 validator=None, registry=None, **kwargs):
        self.parent_name = "TraktManager"
        super().__init__(logger, config, global_cache, validator, registry, **kwargs)
        self.register()

        parent         = kwargs.get("manager")
        # dry_run resolved by BaseManager. Fifteenth instance of the local clobber.
        self.trakt_api = kwargs.get("trakt_api")

        # Optional Sonarr integration — injected externally if needed
        self.sonarr_api = kwargs.get("sonarr_api")

        trakt_cfg = (self.config.get("trakt", {}) if self.config else {})
        self.user = trakt_cfg.get("username", "me")

    # ── User lists ────────────────────────────────────────────────────────

    def get_user_lists(self) -> list:
        return self._get(f"users/{self.user}/lists") or []

    def get_list_items(self, list_slug: str) -> list:
        return self._get(f"users/{self.user}/lists/{list_slug}/items") or []

    def get_collected_shows(self) -> list:
        return self._get(f"users/{self.user}/collection/shows") or []

    def get_user_watched(self, media_type: str = "shows") -> list:
        return self._get(f"sync/watched/{media_type}") or []

    # ── Summary ───────────────────────────────────────────────────────────

    def process_user_lists_summary(self) -> dict:
        """Build an index of all user lists → TVDB IDs with watch stats."""
        lists = self.get_user_lists()
        index = self._build_list_index(lists)
        history_grouped = self._get_history_grouped()
        return self._generate_unified_summary(history_grouped, index)

    # ── Private ───────────────────────────────────────────────────────────

    def _build_list_index(self, lists: list) -> dict:
        index: dict = {}
        for lst in lists:
            slug = (lst.get("ids") or {}).get("slug")
            if not slug:
                continue
            items   = self.get_list_items(slug)
            tvdb_ids = [
                ((item.get("show") or {}).get("ids") or {}).get("tvdb")
                for item in items
                if isinstance(item, dict)
            ]
            index[slug] = [i for i in tvdb_ids if i]
        return index

    def _get_history_grouped(self) -> dict:
        """Group watch history by TVDB ID using the sibling TraktAPIManager.history."""
        if self.trakt_api and hasattr(self.trakt_api, "history") and self.trakt_api.history:
            return self.trakt_api.history.get_history_grouped_by_series()
        return {}

    def _generate_unified_summary(self, history_by_series: dict, list_index: dict) -> dict:
        show_data: dict = defaultdict(lambda: {
            "title":            "",
            "in_library":       False,
            "lists":            set(),
            "episodes_watched": 0,
            "last_watched":     None,
        })

        for list_slug, tvdb_ids in list_index.items():
            for tvdb_id in tvdb_ids:
                show_data[tvdb_id]["lists"].add(list_slug)

        for tvdb_id, history in history_by_series.items():
            show_data[tvdb_id]["episodes_watched"] = len(history)
            dates = [e.get("watched_at") for e in history if e.get("watched_at")]
            if dates:
                latest = max(dates)
                try:
                    show_data[tvdb_id]["last_watched"] = datetime.fromisoformat(
                        latest.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        self.logger.log_info(
            f"[TraktLists] Summary built: {len(show_data)} unique shows across {len(list_index)} lists."
        )
        return dict(show_data)

    def _get(self, endpoint: str, params=None):
        if not self.trakt_api:
            return None
        return self.trakt_api._make_request(endpoint, params=params)

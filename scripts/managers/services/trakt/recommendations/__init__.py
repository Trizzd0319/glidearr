"""
trakt/recommendations — Trakt's algorithmic "you might like this" feed.
================================================================================
Personalised, but NOT a statement of intent — see the module note at the bottom of this
file for why these deliberately do not feed Group-A5.

FROZEN-CACHE BUG (fixed): both getters called ``get_or_generate_cache`` with no
``expiration_time`` and no ``regenerate_on_expiry``. That method's contract is
serve-stale-forever in that configuration — once the file existed it was returned from
disk on every subsequent call and the fetcher never ran again. On this install the two
files had gone 26 days without refreshing. They now carry a real TTL and opt in to
regeneration, so an expired copy is actually re-fetched.
"""
from scripts.managers.factories.base_manager import BaseManager
from scripts.managers.factories.mixins.component_manager import ComponentManagerMixin
from scripts.support.utilities.decorators.timing import timeit
from scripts.support.utilities.logger.logger import LoggerManager

#: 24 hours — the SAME TTL ``trakt/history`` uses (86_400), and the cadence Trakt itself
#: recomputes recommendations on. Chosen for consistency with the project's other Trakt
#: fetches rather than invented: history is 86_400, progress carries ``_PROGRESS_TTL``,
#: and nothing in this app polls Trakt more often than daily. ``regenerate_on_expiry`` is
#: the half that actually matters — without it the TTL only changes the log line, because
#: ``get_or_generate_cache`` falls through to the on-disk copy anyway. A rate-limited or
#: failed fetch returning None still serves the last-good copy (that method's contract),
#: so a Trakt outage degrades to stale rather than to empty.
_RECOMMENDATIONS_TTL_S = 86_400


class TraktRecommendationsManager(BaseManager, ComponentManagerMixin):
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

        trakt_cfg = (self.config.get("trakt", {}) if self.config else {})
        self.user = trakt_cfg.get("username", "default")

    # ── Recommendations ───────────────────────────────────────────────────

    def get_recommendations_shows(self, limit: int = 10) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/recommendations/shows",
            generator_function=lambda: self._fetch_shows(limit),
            expiration_time=_RECOMMENDATIONS_TTL_S,
            regenerate_on_expiry=True,
        ) if self.global_cache else self._fetch_shows(limit)

    def get_recommendations_movies(self, limit: int = 10) -> list:
        return self.global_cache.get_or_generate_cache(
            key=f"trakt/{self.user}/recommendations/movies",
            generator_function=lambda: self._fetch_movies(limit),
            expiration_time=_RECOMMENDATIONS_TTL_S,
            regenerate_on_expiry=True,
        ) if self.global_cache else self._fetch_movies(limit)

    def summarize_recommendations(self) -> dict:
        shows  = self.get_recommendations_shows()  or []
        movies = self.get_recommendations_movies() or []

        self.logger.log_info(f"[TraktRec] Recommended shows: {len(shows)}")
        for show in shows:
            self.logger.log_debug(f"  - {show.get('title')} ({show.get('year')})")

        self.logger.log_info(f"[TraktRec] Recommended movies: {len(movies)}")
        for movie in movies:
            self.logger.log_debug(f"  - {movie.get('title')} ({movie.get('year')})")

        return {"shows": shows, "movies": movies}

    # ── Private ───────────────────────────────────────────────────────────

    # A FAILED REQUEST RETURNS None, NOT []. This distinction did not matter while the
    # cache was frozen — the generator never ran. It matters now: ``get_or_generate_cache``
    # serves the last-good copy when a generator returns None, but treats [] as a
    # legitimately-empty result and WRITES it. Coercing a rate-limited or errored Trakt call
    # to [] would therefore let one bad request blank a good recommendations file on TTL
    # expiry. ``self.trakt_api`` being absent (Trakt not configured) IS an honest empty, so
    # that branch still returns [].
    def _fetch_shows(self, limit: int):
        if not self.trakt_api:
            return []
        return self.trakt_api._make_request("recommendations/shows", params={"limit": limit})

    def _fetch_movies(self, limit: int):
        if not self.trakt_api:
            return []
        return self.trakt_api._make_request("recommendations/movies", params={"limit": limit})


# ── WHY RECOMMENDATIONS DO NOT FEED GROUP-A5 ─────────────────────────────────────────
# Decided, not overlooked — recorded here so it is not re-litigated every time somebody
# notices ``INTENT_SOURCE_STRENGTH`` already ranks ``trakt_recommendations`` at 0.65.
#
#   1. THEY ARE NOT A STATEMENT OF INTENT. A5's whole claim is that it is the scorecard's
#      one EXPLICIT-intent term: every other group infers desire from behaviour, A5 reads
#      somebody saying "I want to watch this". A recommendation is an ALGORITHM saying
#      "you might like this" — which is precisely what Groups B/C/E already compute from
#      this household's own history, and better, because they are computed from OUR watch
#      data rather than from Trakt's collaborative filter. Folding it into A5 would put an
#      inference in the one slot reserved for a declaration.
#   2. THE STALENESS TERM IS BLIND TO THEM. Watchlist rows carry ``listed_at`` and MAL
#      rows carry ``list_status.updated_at``; ``recommendations/{shows,movies}`` carry NO
#      per-item timestamp at all. They would enter the index UNDATED, i.e. scored at full
#      strength forever — the exact "fabricated freshness" failure the Plex-union note in
#      ``next_watch.build_intent_index`` refuses to commit.
#   3. THEY ARE MOSTLY ALREADY OWNED. ~70% of the current feed is titles this library
#      already holds, so as a KEEP signal it would mostly hand points (and, via the shield,
#      deletion immunity) to things on the strength of Trakt having noticed we own them.
#
# Where they DO belong is ACQUISITION — deciding what to ADD is exactly the question an
# algorithmic suggestion is qualified to answer, and ``services/acquisition/scorer`` already
# grades them there at 65. This module keeps feeding that path.

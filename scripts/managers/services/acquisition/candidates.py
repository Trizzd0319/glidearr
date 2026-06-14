"""
candidates.py — gather + normalize add-candidates from the enabled sources.
================================================================================
Trakt recommendations and watchlist (shows → Sonarr, movies → Radarr) and, when
the MAL service is present, anime candidates (→ Sonarr). Watchlist is gathered
first so its stronger intent wins on de-duplication.
"""
from __future__ import annotations

# How many of the household's top-affinity people seed the co-cast proposer. A prolific
# actor's filmography can be hundreds of titles, so cap the seed set (and the proposal
# count via `limit`) to keep the source from flooding the acquisition queue.
_PEOPLE_TOP_K = 25


class CandidateGatherer:
    def __init__(self, trakt, mal, logger, sources_cfg: dict, limit: int = 20, plex=None,
                 global_cache=None):
        self.trakt = trakt
        self.mal = mal
        self.plex = plex
        self.logger = logger
        self.sources = sources_cfg or {}
        self.limit = limit
        self.global_cache = global_cache

    def gather(self) -> list:
        out: list = []
        # Plex watchlist FIRST — strongest explicit intent wins on de-duplication
        # (the union is already id-resolved by the fetcher, so its tmdb/tvdb/imdb
        # de-dupes cleanly against Trakt/MAL rather than double-adding).
        if self.sources.get("plex_watchlist", True) and self.plex is not None:
            out += self._plex()
        if self.sources.get("trakt_watchlist", True):
            out += self._trakt("watchlist")
        if self.sources.get("trakt_recommendations", True):
            out += self._trakt("recommendations")
        if self.sources.get("mal", True) and self.mal is not None:
            out += self._mal()
        # Co-cast proposer LAST (default-off): weakest intent, so on de-dup an explicit
        # watchlist/recommendation for the same title always wins over a co-cast hit.
        if self.sources.get("people_cooccurrence", False) and self.global_cache is not None:
            out += self._people()
        return self._dedup(out)

    # ── Plex watchlist (union, already GUID-resolved by the fetcher) ─────────
    def _plex(self) -> list:
        wl = getattr(self.plex, "watchlist", None)
        fn = getattr(wl, "acquisition_candidates", None)
        if not callable(fn):
            return []
        try:
            union = list(fn() or [])
        except Exception as e:
            self.logger.log_warning(f"[acquire] Plex watchlist fetch failed: {e}")
            return []
        return [self._norm_plex(it) for it in union if isinstance(it, dict) and it.get("title")]

    @staticmethod
    def _norm_plex(item: dict) -> dict:
        ids = item.get("ids", {}) or {}
        return {
            "title": item.get("title"),
            "year": item.get("year"),
            "type": "show" if item.get("type") == "show" else "movie",
            "ids": {
                "trakt": None,
                "tvdb": ids.get("tvdb"),
                "tmdb": ids.get("tmdb"),
                "imdb": ids.get("imdb"),
            },
            "genres": [],
            "rating": None,
            "votes": None,
            "runtime": None,
            "source": "plex_watchlist",
            "is_anime": False,
        }

    # ── Trakt ──────────────────────────────────────────────────────────────
    def _trakt(self, which: str) -> list:
        api = getattr(self.trakt, "trakt_api", None)
        if not api:
            return []
        try:
            if which == "watchlist":
                shows = api.watchlist.get_watchlist_shows() or []
                movies = api.watchlist.get_watchlist_movies() or []
                src = "trakt_watchlist"
            else:
                shows = api.recommendations.get_recommendations_shows(self.limit) or []
                movies = api.recommendations.get_recommendations_movies(self.limit) or []
                src = "trakt_recommendations"
        except Exception as e:
            self.logger.log_warning(f"[acquire] Trakt {which} fetch failed: {e}")
            return []
        return ([self._norm(s, "show", src) for s in shows]
                + [self._norm(m, "movie", src) for m in movies])

    # ── MAL (populated once the MAL service is wired) ────────────────────────
    def _mal(self) -> list:
        fn = getattr(self.mal, "acquisition_candidates", None)
        if not callable(fn):
            return []
        try:
            return list(fn() or [])
        except Exception as e:
            self.logger.log_warning(f"[acquire] MAL candidate fetch failed: {e}")
            return []

    # ── People co-occurrence ("more of the people you watch"; default-off) ──────
    def _people(self) -> list:
        """Propose titles that share cast/crew with what the household watches, drawn
        from the people_matrix (machine_learning) the build step cached. Library-scoped:
        the matrix only covers daemon-enriched *arr titles, so this surfaces UNOWNED
        (monitored-but-not-grabbed) titles by people-overlap — the resolver dedups the
        owned ones via in_library. Emits minimal id-keyed candidates; the resolver
        enriches title/genres/year from the *arr lookup."""
        gc = self.global_cache
        fwd_raw = (gc.get("people_matrix/forward") or {}) if gc else {}
        aff_raw = (gc.get("people_matrix/affinity") or {}) if gc else {}
        if not fwd_raw or not aff_raw:
            return []
        try:
            from scripts.managers.machine_learning.people_matrix import (
                deserialize_forward, invert_forward,
            )
            fwd = deserialize_forward(fwd_raw)
            person_index = invert_forward(fwd)
            weights = {int(k): float(v) for k, v in aff_raw.items()}
        except Exception as e:
            self.logger.log_warning(f"[acquire] people-matrix candidate load failed: {e}")
            return []
        if not weights:
            return []

        # Accumulate a per-title weight = sum of the household-affinity of the top-K
        # people who appear in it, then propose the highest-weighted titles.
        top_people = sorted(weights, key=weights.get, reverse=True)[:_PEOPLE_TOP_K]
        proposed: dict = {}
        for pid in top_people:
            w = weights[pid]
            for key in person_index.get(pid, ()):
                proposed[key] = proposed.get(key, 0.0) + w
        ranked = sorted(proposed, key=proposed.get, reverse=True)[:max(0, self.limit)]
        return [self._norm_people(medium, ext_id) for (medium, ext_id) in ranked]

    @staticmethod
    def _norm_people(medium: str, ext_id: int) -> dict:
        is_show = medium == "show"
        return {
            "title": None,
            "year": None,
            "type": "show" if is_show else "movie",
            "ids": {
                "trakt": None,
                "tvdb": ext_id if is_show else None,
                "tmdb": None if is_show else ext_id,
                "imdb": None,
            },
            "genres": [],
            "rating": None,
            "votes": None,
            "runtime": None,
            "source": "people_cooccurrence",
            "is_anime": False,
        }

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _norm(item: dict, kind: str, source: str) -> dict:
        if not isinstance(item, dict):
            item = {}
        base = item.get(kind) if isinstance(item.get(kind), dict) else item
        ids = base.get("ids", {}) or {}
        return {
            "title": base.get("title"),
            "year": base.get("year"),
            "type": kind,
            "ids": {
                "trakt": ids.get("trakt"),
                "tvdb": ids.get("tvdb"),
                "tmdb": ids.get("tmdb"),
                "imdb": ids.get("imdb"),
            },
            "genres": base.get("genres", []) or [],
            "rating": base.get("rating"),
            "votes": base.get("votes"),
            "runtime": base.get("runtime"),
            "source": source,
            "is_anime": False,
        }

    @staticmethod
    def _dedup(cands: list) -> list:
        seen: set = set()
        out: list = []
        for c in cands:
            ids = c.get("ids", {}) or {}
            # Key on the id-space the RESOLVER routes by for this type (tvdb for shows,
            # tmdb for movies — resolver._lookup / id_field) so the same title from two
            # sources collapses even when they carry different id coverage. Without this,
            # a tvdb-only co-cast show hit would NOT dedup against a tmdb-carrying
            # watchlist show hit, and the "explicit intent wins" promise would break for
            # shows. Movies are unaffected (tmdb-first either way).
            if c.get("type") == "show":
                primary = ids.get("tvdb") or ids.get("tmdb") or ids.get("imdb") or c.get("title")
            else:
                primary = ids.get("tmdb") or ids.get("tvdb") or ids.get("imdb") or c.get("title")
            key = (c.get("type"), str(primary))
            if primary and key not in seen:
                seen.add(key)
                out.append(c)
        return out

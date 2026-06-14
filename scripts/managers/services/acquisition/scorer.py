"""
scorer.py — explainable acquisition scoring.
================================================================================
Produces a 0–100 ``total`` plus a per-component ``matrix`` so every add decision
is transparent (the matrix is rendered in the acquisition summary table and at
debug). Components degrade gracefully: any signal that's unavailable for a
candidate is marked "n/a" and dropped from the weighted average rather than
counted as zero.

Signals:
  genre_affinity     — candidate genres vs household genre affinity (Tautulli)
  source             — explicit intent (watchlist/plan-to-watch) > suggestions
  trakt_rating       — community rating, when present on the item
  popularity         — vote volume (log-scaled), when present
  recency            — newer titles score a little higher
"""
from __future__ import annotations

import math

_WEIGHTS = {
    "genre_affinity": 0.35,
    "source": 0.25,
    "trakt_rating": 0.15,
    "popularity": 0.10,
    "recency": 0.15,
    # people_affinity (cast/crew overlap with household taste) ships at weight 0.0:
    # the signal is COMPUTED + shown in the matrix for transparency but contributes
    # nothing to the total (a 0.0 weight adds 0 to both num and den in _weighted), so
    # acquisition scores are byte-identical until a behavior-change PR rebalances it.
    "people_affinity": 0.0,
}

_SOURCE_SCORE = {
    "plex_watchlist": 100,        # top explicit-intent tier (the household literally said "watch this")
    "trakt_watchlist": 100,
    "mal_plantowatch": 100,
    "trakt_recommendations": 65,
    "mal_suggestions": 65,
    "plex_playlist": 60,          # deferred feed (default-off)
    "people_cooccurrence": 60,    # co-cast proposer (default-off): shares cast/crew with watched titles
    "plex_hubs": 58,              # deferred feed (default-off)
    "mal_seasonal": 55,
}

_CURRENT_YEAR = 2026  # repo "today" is 2026-06-06; recency is a soft signal only.


class AcquisitionScorer:
    def __init__(self, global_cache, logger):
        self.gc = global_cache
        self.logger = logger
        self._genre_weights = None
        self._people = None            # (forward_map, {person_id: weight}) lazy-loaded

    def _people_data(self):
        """Lazy-load the people_matrix forward map + household person-affinity from the
        cache the people-matrix build wrote. ``({}, {})`` when the feature has never run
        (so the people_affinity signal stays absent → byte-identical)."""
        if self._people is None:
            fwd_raw = (self.gc.get("people_matrix/forward") if self.gc else None) or {}
            aff_raw = (self.gc.get("people_matrix/affinity") if self.gc else None) or {}
            if fwd_raw and aff_raw:
                from scripts.managers.machine_learning.people_matrix import deserialize_forward
                self._people = (deserialize_forward(fwd_raw),
                                {int(k): float(v) for k, v in aff_raw.items()})
            else:
                self._people = ({}, {})
        return self._people

    def _affinity(self) -> dict:
        if self._genre_weights is None:
            aff = (self.gc.get("tautulli/affinity") if self.gc else None) or {}
            genres = aff.get("genres", aff) if isinstance(aff, dict) else {}
            numeric = {str(k).lower(): float(v) for k, v in genres.items()
                       if isinstance(v, (int, float))}
            top = max(numeric.values()) if numeric else 0.0
            self._genre_weights = {k: (v / top) for k, v in numeric.items()} if top else {}
        return self._genre_weights

    def score(self, cand: dict) -> dict:
        matrix: dict = {}

        # genre affinity
        weights = self._affinity()
        cand_genres = [str(g).lower() for g in (cand.get("genres") or [])]
        hits = [weights[g] for g in cand_genres if g in weights]
        matrix["genre_affinity"] = round(100 * (sum(hits) / len(hits)), 1) if hits else None

        # source intent
        matrix["source"] = _SOURCE_SCORE.get(cand.get("source"), 50)

        # community rating (0–10 → 0–100)
        rating = cand.get("rating")
        matrix["trakt_rating"] = round(float(rating) * 10, 1) if isinstance(rating, (int, float)) and rating else None

        # popularity (votes, log-scaled; ~50k votes → 100)
        votes = cand.get("votes")
        if isinstance(votes, (int, float)) and votes > 0:
            matrix["popularity"] = round(min(100.0, (math.log10(votes + 1) / math.log10(50000)) * 100), 1)
        else:
            matrix["popularity"] = None

        # recency
        year = cand.get("year")
        if isinstance(year, int) and year:
            age = max(0, _CURRENT_YEAR - year)
            matrix["recency"] = round(max(0.0, 100 - age * 8), 1)  # ~12y to reach 0
        else:
            matrix["recency"] = None

        # people affinity (cast/crew overlap with household taste). Only ADDED to the
        # matrix when the people_matrix is built AND this candidate's people are known
        # → absent otherwise, so the matrix (and total) are byte-identical when the
        # feature is off. Even when present, weight 0.0 keeps the total unchanged.
        fwd, pweights = self._people_data()
        if fwd and pweights:
            ids = cand.get("ids", {}) or {}
            is_show = cand.get("type") == "show"
            ext = ids.get("tvdb") if is_show else ids.get("tmdb")
            try:
                roles = fwd.get(("show" if is_show else "movie", int(ext))) if ext else None
            except (ValueError, TypeError):
                roles = None
            if roles:
                from scripts.managers.machine_learning.scoring._shared import person_affinity_score
                matrix["people_affinity"] = round(person_affinity_score(roles, pweights, 100.0), 1)

        total = self._weighted(matrix)
        return {"total": total, "matrix": matrix}

    @staticmethod
    def _weighted(matrix: dict) -> int:
        num = den = 0.0
        for key, weight in _WEIGHTS.items():
            val = matrix.get(key)
            if val is None:
                continue
            num += weight * float(val)
            den += weight
        return round(num / den) if den else 0

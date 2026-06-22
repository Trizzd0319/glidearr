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
    # people_affinity (cast/crew overlap with household taste): the MODULE default is
    # 0.0 so a scorer built WITHOUT a config stays byte-identical (tests / back-compat).
    # The LIVE weight is config-gated in __init__ (acquisition.people_affinity_weight,
    # default 0.08 when a config is present) — see _PEOPLE_AFFINITY_WEIGHT_DEFAULT.
    "people_affinity": 0.0,
}

# Live default for the config-gated cast/crew weight. _weighted() renormalizes on the
# PRESENT signals (dynamic denominator), so a candidate carrying NO people_affinity is
# untouched at any weight; only co-cast candidates (which DO carry it) are re-ranked.
_PEOPLE_AFFINITY_WEIGHT_DEFAULT = 0.08

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

# Short labels for the explainable "why" column. Several feed names share a source-VALUE tier
# (plex/trakt watchlist + mal_plantowatch all = 100), so the value maps to one tier word.
_SOURCE_LABEL = {100: "watchlist", 65: "suggested", 60: "playlist", 58: "hubs", 55: "seasonal", 50: "feed"}
_COMPONENT_LABEL = {"genre_affinity": "genre", "trakt_rating": "rating",
                    "popularity": "popular", "recency": "recent", "people_affinity": "cast"}


class AcquisitionScorer:
    def __init__(self, global_cache, logger, config=None):
        self.gc = global_cache
        self.logger = logger
        self._genre_weights = None
        self._people = None            # (forward_map, {person_id: weight}) lazy-loaded
        self._aff_people = None        # household top cast/crew NAMES, lazy-loaded
        # Per-instance signal weights. Copy the module defaults (people_affinity 0.0), then
        # — only when a config is supplied (the live run) — let the cast/crew weight be
        # config-gated. Absent key → the live default (ON); 0.0 → disabled (byte-identical).
        # No config (tests / back-compat) → module 0.0 → scores unchanged.
        self._weights = dict(_WEIGHTS)
        if config is not None:
            try:
                w = (config.get("acquisition") or {}).get(
                    "people_affinity_weight", _PEOPLE_AFFINITY_WEIGHT_DEFAULT)
                self._weights["people_affinity"] = (
                    float(w) if isinstance(w, (int, float)) and w >= 0
                    else _PEOPLE_AFFINITY_WEIGHT_DEFAULT)
            except Exception:
                self._weights["people_affinity"] = _PEOPLE_AFFINITY_WEIGHT_DEFAULT

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

    def taste_profile(self, k: int = 5) -> dict:
        """The household taste profile the affinity signals are measured against — the top
        genres + cast/crew BY NAME, read from the same ``tautulli/affinity`` cache the genre
        signal uses (its ``actors``/``directors`` maps are name-keyed and pre-sorted desc;
        see :func:`aggregate_affinity`). This is the nameable cast/crew context for the
        acquisition "why" breakdown: a candidate's OWN credits aren't reachable (the *arr
        lookup carries none and the people-matrix is id-only by design), but the household's
        favourite people are. Lazy-cached once per run; ``[]`` lists when affinity is absent."""
        if self._aff_people is None:
            aff = (self.gc.get("tautulli/affinity") if self.gc else None) or {}
            aff = aff if isinstance(aff, dict) else {}

            def _top(key):
                m = aff.get(key)
                return [str(n) for n in m][:k] if isinstance(m, dict) else []

            self._aff_people = {"genres": _top("genres"),
                                "directors": _top("directors"),
                                "actors": _top("actors")}
        return self._aff_people

    def score(self, cand: dict) -> dict:
        matrix: dict = {}

        # genre affinity
        weights = self._affinity()
        cand_genres = [str(g).lower() for g in (cand.get("genres") or [])]
        hits = [weights[g] for g in cand_genres if g in weights]
        matrix["genre_affinity"] = round(100 * (sum(hits) / len(hits)), 1) if hits else None
        # Which genres matched (name + normalized 0–1 household weight), descending — the
        # nameable evidence behind genre_affinity. Reuses the `weights` lookup above (no
        # extra cost) and dedups via dict. Captured in `evidence`, never folded into `matrix`.
        matched_genres = sorted(
            {g: round(weights[g], 2) for g in cand_genres if g in weights}.items(),
            key=lambda kv: kv[1], reverse=True,
        )

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
        people_ev = None
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
                # How many of THIS title's people are household favourites (weight > 0). Their
                # names aren't reachable here (the people-matrix is id-only), so the breakdown
                # pairs this count/score with the household's named top cast/crew (taste_profile).
                people_ev = {"score": matrix["people_affinity"],
                             "matched": sum(1 for pids in roles.values()
                                            for pid in pids if pweights.get(pid, 0) > 0)}

        # Raw, human-readable drivers behind the score — captured straight off the candidate
        # and returned ALONGSIDE the matrix (never inside it), so `total`/`matrix`/`reason()`
        # stay byte-identical. Rendered in the acquisition elevation breakdown.
        evidence = {
            "matched_genres": matched_genres,
            "source_feed": cand.get("source"),
            "rating10": float(rating) if isinstance(rating, (int, float)) and rating else None,
            "votes": float(votes) if isinstance(votes, (int, float)) and votes > 0 else None,
            "year": year if isinstance(year, int) and year else None,
        }
        if people_ev:
            evidence["people"] = people_ev

        total = self._weighted(matrix)
        return {"total": total, "matrix": matrix, "evidence": evidence}

    def _weighted(self, matrix: dict) -> int:
        num = den = 0.0
        for key, weight in self._weights.items():
            val = matrix.get(key)
            if val is None:
                continue
            num += weight * float(val)
            den += weight
        return round(num / den) if den else 0

    def reason(self, matrix: dict, *, top: int = 3, evidence: "dict | None" = None) -> str:
        """A short, human "why this scored what it did" — the top components by CONTRIBUTION
        (``weight × value``, not raw value), so a high score (e.g. the ≥ ``4k_dual_min_score`` that
        earns the 4K copy) is explainable at a glance: e.g. ``"Sci-Fi + Action, watchlist, recent 92"``.
        Reads this instance's (config-gated) weights, so ``cast`` only appears once the
        people_affinity weight is non-zero. ``""`` for an empty matrix (universe-saga grabs
        bypass scoring → no signals).

        ``evidence`` (the ``score()`` sibling): when given, the genre_affinity driver is rendered
        as the ACTUAL matched genre names (``"Sci-Fi + Action"``, the household-favourite genres
        this title hit, top-3 by weight) instead of the bare ``"genre 71"`` score — so the table
        names the genres. Omitted/empty evidence falls back to the score label (back-compat)."""
        if not isinstance(matrix, dict) or not matrix:
            return ""
        matched = (evidence or {}).get("matched_genres") or []
        contrib = [(w * float(matrix[k]), k, matrix[k]) for k, w in self._weights.items()
                   if w > 0 and matrix.get(k) is not None]
        contrib.sort(key=lambda t: t[0], reverse=True)
        parts = []
        for _c, key, val in contrib[:top]:
            if key == "source":
                parts.append(_SOURCE_LABEL.get(int(val), f"source {int(val)}"))
            elif key == "genre_affinity" and matched:
                parts.append(" + ".join(str(g).title() for g, _w in matched[:3]))
            else:
                parts.append(f"{_COMPONENT_LABEL.get(key, key)} {int(round(float(val)))}")
        return ", ".join(parts)

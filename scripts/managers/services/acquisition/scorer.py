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

# The explicit-intent source tier (see _SOURCE_SCORE): the household literally put this
# title on a watchlist. Used to suppress production-year recency — see score().
_EXPLICIT_INTENT_SOURCE = 100

# Per-genre ceiling for the noisy-OR below. Keeps a SINGLE matched genre under 100 so a
# multi-genre match can actually outrank it; at 0.80 the household's #1 genre alone scores
# 80.0 and leaves headroom for corroborating genres to push toward 100.
_GENRE_SATURATION = 0.80


def _genre_affinity(hits: list) -> float:
    """Combine per-genre household affinities (0–1 each) into one 0–100 score.

    Noisy-OR — ``1 - Π(1 - wᵢ·S)`` — treating each matched genre as independent evidence
    that the household wants this title.

    This REPLACED an unweighted mean, which had the aggregator backwards: dividing by the
    match count meant every additional genre the household liked dragged the score DOWN.
    Measured on the live watchlist, ``[adventure]`` scored 100.0 while
    ``[adventure, action, drama]`` scored 86.1 — a title matching the #1, #3 and #5
    household genres ranked BELOW one matching only the #1, and 131 of 663 movies won
    purely by carrying a single broad tag. The top 10 was 8 drama-only films as a result.

    Two properties the mean lacked:

    * **Monotonic.** An extra matched genre can only raise the score, never lower it.
    * **No penalty for breadth.** A genre the household has no weight for never enters
      ``hits``, so a film tagged with six genres of which two match is scored on the two —
      the four non-matching tags cost it nothing.
    """
    miss = 1.0
    for w in hits:
        miss *= 1.0 - min(1.0, max(0.0, float(w))) * _GENRE_SATURATION
    return 100.0 * (1.0 - miss)


class AcquisitionScorer:
    def __init__(self, global_cache, logger, config=None, *, weight_overrides=None):
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
        # Per-instance weight overrides for a caller scoring a DIFFERENT objective off the same
        # signals — e.g. the 'This Week in History' anniversary shelf weights all-time popularity
        # heavier than the add pipeline does (a notable old title should beat a recent obscure one on
        # a HISTORY shelf). Applied LAST so it can retune any present signal; an unknown key or a
        # negative/non-numeric value is ignored. _weighted() renormalizes on the present signals, so
        # raising one weight just dilutes the others' relative pull — no separate formula, no double-
        # count. None (the add pipeline / tests) → byte-identical.
        for key, val in (weight_overrides or {}).items():
            if key in self._weights and isinstance(val, (int, float)) and val >= 0:
                self._weights[key] = float(val)

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
        genres + cast/crew BY NAME (actors, directors, composers, producers), read from the
        same ``tautulli/affinity`` cache the genre signal uses (its people maps are
        name-keyed and pre-sorted desc; see :func:`aggregate_affinity`, which tallies all
        four roles). This is the nameable cast/crew context for the acquisition "why"
        breakdown: a candidate's OWN credits aren't reachable (the *arr lookup carries none
        and the people-matrix is id-only by design), but the household's favourite people
        are. Lazy-cached once per run; ``[]`` lists when affinity is absent — roles the
        metadata source never supplies simply come back empty and print nothing."""
        if self._aff_people is None:
            aff = (self.gc.get("tautulli/affinity") if self.gc else None) or {}
            aff = aff if isinstance(aff, dict) else {}

            def _top(key):
                m = aff.get(key)
                return [str(n) for n in m][:k] if isinstance(m, dict) else []

            self._aff_people = {"genres": _top("genres"),
                                "directors": _top("directors"),
                                "actors": _top("actors"),
                                "writers": _top("writers"),
                                "composers": _top("composers"),
                                "producers": _top("producers")}
        return self._aff_people

    def score(self, cand: dict) -> dict:
        matrix: dict = {}

        # genre affinity
        weights = self._affinity()
        cand_genres = [str(g).lower() for g in (cand.get("genres") or [])]
        hits = [weights[g] for g in cand_genres if g in weights]
        matrix["genre_affinity"] = round(_genre_affinity(hits), 1) if hits else None
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

        # recency — production age, and ONLY for candidates the household did not ask for
        # by name.
        #
        # `100 - age*8` reaches 0 at ~12.5 years, so on a watchlist full of older films it
        # is not a soft signal, it is a constant: 503 of 663 watchlisted movies are >=13y
        # old and all scored exactly 0.0, at weight 0.15. It could not separate a 1951 film
        # from a 2013 one, and it silently taxed the back catalogue by 15% of the total.
        #
        # For explicit-intent sources that tax is also WRONG in principle. Putting a title
        # on a watchlist is a deliberate act; when it was produced says nothing about how
        # much the household wants it now. So watchlisted titles carry no production-recency
        # term at all and rank equally on that axis — _weighted() renormalizes on the
        # signals present, so dropping it redistributes the weight rather than zeroing it.
        #
        # Discovery feeds (recommendations, seasonal, hubs) keep the signal: nobody asked
        # for those by name, and there "it came out recently" is genuine information.
        # `year` is read unconditionally — `evidence` below reports it even when the
        # recency SIGNAL is suppressed, so the breakdown still names the production year.
        year = cand.get("year")
        if matrix["source"] >= _EXPLICIT_INTENT_SOURCE:
            matrix["recency"] = None
        else:
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

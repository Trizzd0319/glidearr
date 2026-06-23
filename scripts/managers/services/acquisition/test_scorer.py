"""AcquisitionScorer.reason() — the human-readable "why" string rendered in the acquisition
decision table's `why` column. It names the top score drivers (by weighted contribution) so a
high total — e.g. a 4K-eligible ≥70 — is explained: which components, in descending impact."""
from __future__ import annotations

from scripts.managers.services.acquisition.scorer import AcquisitionScorer as S

# reason() reads the instance's (config-gated) weights. A no-config scorer keeps the module
# defaults (people_affinity 0.0), so this instance reproduces the original static behavior.
_S = S(None, None)


def test_orders_by_weighted_contribution_not_raw_value():
    # source 65 (×0.25 = 16.2) beats genre 50 (×0.35 = 17.5)? No — genre wins.
    # Pick values where weighted order differs from raw order to prove it sorts by contribution.
    m = {"genre_affinity": 50, "source": 65, "trakt_rating": 90}
    # genre 50×0.35=17.5, source 65×0.25=16.25, rating 90×0.15=13.5  → genre, source, rating
    assert _S.reason(m) == "genre 50, suggested, rating 90"


def test_source_renders_as_named_bucket():
    assert _S.reason({"source": 100}) == "watchlist"          # 100 → watchlist label
    assert _S.reason({"source": 60}) == "playlist"


def test_unknown_source_falls_back_to_numeric():
    assert _S.reason({"source": 42}) == "source 42"


def test_top_limits_to_three_by_default():
    m = {"genre_affinity": 80, "source": 70, "trakt_rating": 60, "popularity": 50, "recency": 40}
    assert _S.reason(m).count(",") == 2                        # exactly 3 drivers → 2 commas


def test_zero_weight_component_excluded():
    # no-config scorer keeps people_affinity weight 0.0 → never appears even if present/high.
    assert "cast" not in _S.reason({"people_affinity": 99, "genre_affinity": 30})


def test_people_component_included_when_weighted():
    # a config-built scorer (people_affinity_weight default 0.08) → cast becomes a driver.
    sw = S(None, None, {"acquisition": {}})
    assert "cast" in sw.reason({"people_affinity": 99, "genre_affinity": 30})


def test_none_components_skipped():
    assert _S.reason({"genre_affinity": None, "source": 60}) == "playlist"


def test_empty_or_invalid_returns_blank():
    assert _S.reason({}) == "" and _S.reason(None) == ""


def test_evidence_renders_named_genres_instead_of_genre_score():
    m = {"genre_affinity": 71, "source": 100, "recency": 88}   # source 25 > genre 24.85 > recency 13.2
    ev = {"matched_genres": [("sci-fi", 0.95), ("action", 0.70), ("drama", 0.40), ("comedy", 0.10)]}
    # genre driver becomes the actual genre NAMES (Title-Cased, top-3 by weight), not "genre 71".
    assert _S.reason(m, evidence=ev) == "watchlist, Sci-Fi + Action + Drama, recent 88"
    # back-compat: no/empty evidence → the original bare score label.
    assert _S.reason(m) == "watchlist, genre 71, recent 88"
    assert _S.reason(m, evidence={"matched_genres": []}) == "watchlist, genre 71, recent 88"


# ── evidence capture (the named "why" drivers) ────────────────────────────────────────
# score() returns a third `evidence` sibling key with the raw, nameable drivers; `total`
# and `matrix` stay byte-identical (so reason()/the existing tests are untouched).

class _GC:
    def __init__(self, d): self.d = dict(d)
    def get(self, k, default=None): return self.d.get(k, default)


# household genre weights normalize on the max (sci-fi=100 -> 1.0, action=70 -> 0.7); the
# actors/directors maps are already name-keyed + sorted desc (aggregate_affinity).
AFF = {"genres": {"sci-fi": 100.0, "action": 70.0, "drama": 40.0},
       "directors": {"Denis Villeneuve": 9, "Christopher Nolan": 7},
       "actors": {"Timothee Chalamet": 5, "Zendaya": 4}}


def _scorer(aff=AFF):
    return S(_GC({"tautulli/affinity": aff}), None)


def test_evidence_matched_genres_named_and_weighted():
    out = _scorer().score({"genres": ["Sci-Fi", "Action", "Romance"], "source": "trakt_watchlist"})
    assert out["evidence"]["matched_genres"] == [("sci-fi", 1.0), ("action", 0.7)]  # romance unmatched


def test_evidence_is_a_sibling_key_not_in_matrix():
    out = _scorer().score({"genres": ["sci-fi"], "source": "trakt_watchlist", "rating": 8.4})
    assert "evidence" in out
    assert "matched_genres" not in out["matrix"] and isinstance(out["total"], int)


def test_evidence_raw_signals_captured():
    out = _scorer().score({"genres": [], "source": "plex_watchlist",
                           "rating": 8.4, "votes": 412000, "year": 2024})
    ev = out["evidence"]
    assert ev["source_feed"] == "plex_watchlist"
    assert ev["rating10"] == 8.4 and ev["votes"] == 412000.0 and ev["year"] == 2024


def test_evidence_missing_signals_are_none():
    ev = _scorer().score({"genres": ["sci-fi"], "source": "trakt_recommendations"})["evidence"]
    assert ev["rating10"] is None and ev["votes"] is None and ev["year"] is None


def test_evidence_no_genre_overlap_is_empty_list():
    assert _scorer().score({"genres": ["western"], "source": "trakt_watchlist"})["evidence"]["matched_genres"] == []


def test_evidence_omits_people_when_matrix_not_built():
    assert "people" not in _scorer().score({"genres": ["sci-fi"], "source": "trakt_watchlist"})["evidence"]


def test_taste_profile_names_household_cast_and_crew():
    prof = _scorer().taste_profile(k=2)
    assert prof["directors"] == ["Denis Villeneuve", "Christopher Nolan"]
    assert prof["actors"] == ["Timothee Chalamet", "Zendaya"]
    assert prof["genres"][:2] == ["sci-fi", "action"]


def test_taste_profile_empty_without_affinity():
    assert S(_GC({}), None).taste_profile() == {"genres": [], "directors": [], "actors": []}


# ── per-instance weight overrides (the anniversary shelf re-weights popularity) ────────
# A caller scoring a different objective off the same signals can retune any weight; the
# weighted average renormalizes on the present signals, so the SAME popularity metric just
# carries more pull. None / {} → byte-identical (the add pipeline is untouched).

def test_weight_override_lets_popularity_outrank_recency():
    pop = {"votes": 50000, "year": 2005}      # very popular, old → low recency
    obscure = {"votes": 5, "year": 2026}      # obscure, brand-new → high recency
    base = S(None, None)
    # default weights (popularity 0.10 < recency 0.15): the recent obscure title wins
    assert base.score(obscure)["total"] > base.score(pop)["total"]
    boosted = S(None, None, weight_overrides={"popularity": 0.60})
    # popularity re-weighted above recency: the notable old title now wins
    assert boosted.score(pop)["total"] > boosted.score(obscure)["total"]


def test_weight_override_none_or_empty_is_byte_identical():
    cand = {"genres": ["sci-fi"], "votes": 1200, "year": 2019, "source": "trakt_recommendations"}
    ref = S(None, None).score(cand)
    assert S(None, None, weight_overrides=None).score(cand) == ref
    assert S(None, None, weight_overrides={}).score(cand) == ref


def test_weight_override_ignores_unknown_key_and_negative_value():
    cand = {"votes": 1200, "year": 2019}
    ref = S(None, None).score(cand)["total"]
    # an unknown signal name and a negative weight are both ignored → total unchanged
    assert S(None, None, weight_overrides={"nope": 0.9, "popularity": -1}).score(cand)["total"] == ref

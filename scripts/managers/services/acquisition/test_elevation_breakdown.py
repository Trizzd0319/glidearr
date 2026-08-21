"""AcquisitionManager._log_elevation_breakdown + acquisition.breakdown — the "why was this
elevated" breakdown, now emitted as boxed TABLES built off one canonical record per acted-on
title (breakdown.SCHEMA) rather than free-form prose stanzas.

The load-bearing assertions here are:
  * information preservation — every driver the prose named still reaches the output;
  * P-C (absent vs empty) — a title with NO people evidence must render 'not scored', never
    a 0, and must carry None (not 0) in the frame. scorer.score only sets evidence["people"]
    when the candidate resolves in the people-matrix, so the two states are genuinely
    different facts and collapsing them would assert a check that never happened;
  * the score decomposition sums back to the score;
  * every legend is a pure aggregation of the records (no second source of truth);
  * ASCII-safety (cp1252 log sinks).
"""
from __future__ import annotations

from scripts.managers.services.acquisition import AcquisitionManager
from scripts.managers.services.acquisition import breakdown as bd

WEIGHTS = {"genre_affinity": 0.35, "source": 0.25, "trakt_rating": 0.15,
           "popularity": 0.10, "recency": 0.15, "people_affinity": 0.08}


class _CapLogger:
    def __init__(self):
        self.lines = []
        self.tables = []

    def log_info(self, m):
        self.lines.append(str(m))

    def log_debug(self, *a, **k):
        pass

    def log_table(self, headers, data, title="", descriptions=None, caption=""):
        self.tables.append({"headers": list(headers), "rows": [list(r) for r in data],
                            "title": title, "caption": caption})
        self.lines.append(f"{title}\n" + "\n".join(" | ".join(str(c) for c in r) for r in data))


class _Scorer:
    _weights = dict(WEIGHTS)

    def taste_profile(self, k=5):
        return {"genres": ["sci-fi"], "directors": ["Denis Villeneuve"], "actors": ["Zendaya"]}


def _mgr():
    m = object.__new__(AcquisitionManager)
    m.logger = _CapLogger()
    m.global_cache = None
    return m


def _cand(**kw):
    base = {
        # score is what scorer._weighted() actually yields for the matrix below
        # (num 96.93 / den 1.08 -> 90); keeping them consistent is what lets
        # test_contributions_sum_to_score be a real check rather than a tautology.
        "title": "Dune: Part Two", "ext_id": 693134, "score": 90, "type": "movie",
        "instance": "standard", "quality_profile": {"id": 7, "name": "UHD Bluray + WEB"},
        "profile_reason": "score 90 picks up to the 2160p tier",
        "matrix": {"genre_affinity": 95.0, "source": 100, "trakt_rating": 84.0,
                   "popularity": 60.0, "recency": 96.0, "people_affinity": 71.0},
        "evidence": {"matched_genres": [("sci-fi", 0.95), ("action", 0.70)],
                     "source_feed": "trakt_watchlist", "rating10": 8.4,
                     "votes": 412000.0, "year": 2024,
                     "people": {"score": 71.0, "matched": 3}},
    }
    base.update(kw)
    return base


# ── information preservation ──────────────────────────────────────────────────────

def test_breakdown_names_all_drivers_and_is_ascii():
    m = _mgr()
    m._log_elevation_breakdown([_cand()], _Scorer())
    text = "\n".join(m.logger.lines)

    assert "Dune: Part Two" in text and "90" in text
    assert "sci-fi" in text and "action" in text          # genre NAMES on the title row
    assert "0.95" in text and "0.70" in text              # genre WEIGHTS in the legend
    assert "Trakt WL" in text and "8.4" in text and "412K" in text and "2024" in text
    assert "3 (71.0)" in text                             # cast/crew count + affinity
    assert "Denis Villeneuve" in text and "Zendaya" in text
    text.encode("cp1252")                                 # no un-encodable unicode


def test_profile_rationale_emitted_once_and_joined_by_ref():
    """The ~110-char rationale is a run-constant; it belongs in a key, referenced by (n)."""
    m = _mgr()
    same = "dual-version HD baseline (clamped to the <=1080p durable floor)"
    m._log_elevation_breakdown(
        [_cand(title="A", profile_reason=same, quality_profile={"id": 4, "name": "HD"}),
         _cand(title="B", profile_reason=same, quality_profile={"id": 4, "name": "HD"}),
         _cand(title="C", profile_reason=same, quality_profile={"id": 4, "name": "HD"})],
        _Scorer())
    text = "\n".join(m.logger.lines)
    assert text.count(same) == 1                          # once, not once per row
    assert text.count("(1)") >= 3                         # each row carries the join ref


def test_saga_column_appears_only_when_a_title_has_one():
    m = _mgr()
    m._log_elevation_breakdown([_cand(saga_names=["Marvel Cinematic Universe"])], _Scorer())
    assert "Marvel Cinematic Universe" in "\n".join(m.logger.lines)

    m2 = _mgr()
    m2._log_elevation_breakdown([_cand()], _Scorer())
    assert "Saga" not in m2.logger.tables[0]["headers"]


def test_anime_route_and_absent_optional_keys_do_not_crash():
    m = _mgr()
    m._log_elevation_breakdown(
        [_cand(title="Ao no Hako", type="show", route_category="anime",
               quality_profile={"id": 22, "name": "[Anime] Remux-1080p"}),
         {"title": "Bare Title Only", "score": 22, "evidence": {}, "matrix": {}}],
        _Scorer())
    text = "\n".join(m.logger.lines)
    assert "Ao no Hako" in text and "Bare Title Only" in text
    recs = bd.build_records([{"title": "Bare", "score": 1, "evidence": {}, "matrix": {}}],
                            WEIGHTS)
    assert recs[0]["genre_names"] == [] and recs[0]["weight_denominator"] is None


# ── P-C: absent is not empty ──────────────────────────────────────────────────────

def test_people_absent_never_renders_or_stores_as_zero():
    """No evidence["people"] means the people-matrix had no entry — the signal was never
    computed. It must NOT look like a computed zero in the cell or in the frame."""
    no_people = _cand(title="Unmatched")
    no_people["evidence"] = dict(no_people["evidence"])
    no_people["evidence"].pop("people")
    no_people["matrix"] = {k: v for k, v in no_people["matrix"].items()
                           if k != "people_affinity"}

    recs = bd.build_records([no_people], WEIGHTS)
    r = recs[0]
    assert r["people_scored"] is False
    assert r["people_matched"] is None and r["people_affinity"] is None
    assert r["sig_people_affinity"] is None and r["con_people_affinity"] is None
    assert bd._people_cell(r) == "not scored"


def test_people_scored_zero_is_distinct_from_absent():
    zero = _cand(title="Checked, none found")
    zero["evidence"] = dict(zero["evidence"], people={"score": 0.0, "matched": 0})
    zero["matrix"] = dict(zero["matrix"], people_affinity=0.0)

    r = bd.build_records([zero], WEIGHTS)[0]
    assert r["people_scored"] is True and r["people_matched"] == 0
    assert bd._people_cell(r) == "0 (0.0)"
    assert bd._people_cell(r) != "not scored"


def test_absent_signal_is_excluded_from_the_denominator_not_scored_zero():
    """A missing signal shrinks the denominator (scorer._weighted); it is not a zero term."""
    full = bd.build_records([_cand()], WEIGHTS)[0]
    thin_c = _cand()
    thin_c["matrix"] = {k: v for k, v in thin_c["matrix"].items()
                        if k not in ("popularity", "people_affinity")}
    thin = bd.build_records([thin_c], WEIGHTS)[0]
    assert full["weight_denominator"] > thin["weight_denominator"]
    assert thin["con_popularity"] is None


# ── decomposition + legends ───────────────────────────────────────────────────────

def test_contributions_sum_to_score():
    r = bd.build_records([_cand()], WEIGHTS)[0]
    total = sum(r[f"con_{k}"] for k, _l in bd._SIGNALS if r[f"con_{k}"] is not None)
    assert abs(total - r["score"]) <= 1.0


def test_legends_are_pure_aggregations_of_the_records():
    recs = bd.build_records([_cand(), _cand(title="Second")], WEIGHTS)
    assert dict(bd.genre_legend(recs)) == {"sci-fi": 0.95, "action": 0.70}
    assert bd.profile_legend(recs) == [(1, "UHD Bluray + WEB",
                                        "score 90 picks up to the 2160p tier", 2)]
    cov = {label: (present, absent) for label, _k, _w, present, absent
           in bd.signal_legend(recs, WEIGHTS)}
    assert cov["Cast"] == (2, 0)


def test_cohort_rollup_splits_show_vs_movie_per_source():
    recs = bd.build_records(
        [_cand(type="movie"), _cand(title="S", type="show"),
         _cand(title="M", type="show", evidence=dict(_cand()["evidence"],
                                                     source_feed="mal_suggestions"))],
        WEIGHTS)
    rows = {r[0]: r for r in bd.cohort_summary(recs)}
    assert rows["Trakt WL"][1:4] == (2, 1, 1)
    assert rows["MAL sugg"][1:4] == (1, 1, 0)


# ── frame export ──────────────────────────────────────────────────────────────────

def test_frame_matches_schema_and_keeps_absent_nullable():
    recs = bd.build_records([_cand()], WEIGHTS)
    assert set(recs[0]) == set(bd.SCHEMA)
    payload = bd.to_payload(recs, {"genres": ["sci-fi"]}, WEIGHTS)
    assert payload["schema_version"] == bd.SCHEMA_VERSION
    assert payload["columns"] == list(bd.SCHEMA)
    assert len(payload["rows"]) == 1

    try:
        import pandas  # noqa: F401
    except ImportError:
        return
    no_people = _cand(title="Unmatched")
    no_people["evidence"] = {k: v for k, v in no_people["evidence"].items() if k != "people"}
    df = bd.to_dataframe(bd.build_records([no_people], WEIGHTS))
    assert list(df.columns) == list(bd.SCHEMA)
    # Nullable dtype: absent stays <NA> so a stray fillna(0) in a site template cannot
    # silently reassert "we checked and found zero".
    assert df["people_matched"].isna().all()
    assert str(df["people_matched"].dtype) == "Int64"
    assert df["people_scored"].iloc[0] is False or bool(df["people_scored"].iloc[0]) is False


def test_empty_elevated_is_a_no_op():
    m = _mgr()
    m._log_elevation_breakdown([], _Scorer())
    assert m.logger.lines == [] and m.logger.tables == []

"""P5 — acquisition people-affinity signal (byte-identical at weight 0.0) + the
co-cast candidate source (default-off, library-scoped)."""
from __future__ import annotations

from scripts.managers.services.acquisition.candidates import CandidateGatherer
from scripts.managers.services.acquisition.scorer import AcquisitionScorer


class _Log:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass


class _GC:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _roles(*cast):
    return {"cast": list(cast), "directors": [], "writers": [], "composers": [], "producers": []}


# ── scorer signal ────────────────────────────────────────────────────────────
CAND = {"type": "movie", "ids": {"tmdb": 603}, "genres": [], "year": 2000}


def test_signal_absent_without_matrix():
    out = AcquisitionScorer(_GC(), _Log()).score(dict(CAND))
    assert "people_affinity" not in out["matrix"]          # byte-identical: key never added


def test_signal_computed_but_weight_zero_keeps_total_byte_identical():
    gc = _GC()
    gc.set("people_matrix/forward", {"movie:603": _roles(1245)})
    gc.set("people_matrix/affinity", {"1245": 5.0})
    on = AcquisitionScorer(gc, _Log()).score(dict(CAND))
    off = AcquisitionScorer(_GC(), _Log()).score(dict(CAND))
    assert on["matrix"]["people_affinity"] > 0             # signal is computed + shown
    assert on["total"] == off["total"]                    # …but weight 0.0 → total unchanged


def test_signal_absent_when_candidate_not_in_matrix():
    gc = _GC()
    gc.set("people_matrix/forward", {"movie:999": _roles(1245)})
    gc.set("people_matrix/affinity", {"1245": 5.0})
    out = AcquisitionScorer(gc, _Log()).score(dict(CAND))   # 603 not in matrix
    assert "people_affinity" not in out["matrix"]


# ── co-cast source ───────────────────────────────────────────────────────────
def _gatherer(gc, **sources):
    return CandidateGatherer(None, None, _Log(), sources, limit=20, global_cache=gc)


def test_source_proposes_top_people_titles():
    gc = _GC()
    gc.set("people_matrix/forward", {"movie:603": _roles(1245), "movie:24428": _roles(1245, 3223)})
    gc.set("people_matrix/affinity", {"1245": 5.0})
    cands = _gatherer(gc, people_cooccurrence=True)._people()
    assert {c["ids"]["tmdb"] for c in cands} == {603, 24428}
    assert all(c["source"] == "people_cooccurrence" and c["type"] == "movie" for c in cands)


def test_source_empty_without_matrix():
    assert _gatherer(_GC(), people_cooccurrence=True)._people() == []


def test_source_show_uses_tvdb():
    gc = _GC()
    gc.set("people_matrix/forward", {"show:77": _roles(1245)})
    gc.set("people_matrix/affinity", {"1245": 5.0})
    cands = _gatherer(gc, people_cooccurrence=True)._people()
    assert cands[0]["type"] == "show" and cands[0]["ids"]["tvdb"] == 77 and cands[0]["ids"]["tmdb"] is None


def test_show_dedups_across_id_spaces():
    # a tvdb-only co-cast show hit must collapse with a tvdb+tmdb watchlist hit for the
    # same series; explicit-intent (gathered first) wins.
    out = _gatherer(_GC())._dedup([
        {"type": "show", "ids": {"tvdb": 77, "tmdb": 555}, "source": "trakt_watchlist", "title": "S"},
        {"type": "show", "ids": {"tvdb": 77, "tmdb": None}, "source": "people_cooccurrence", "title": None},
    ])
    assert len(out) == 1 and out[0]["source"] == "trakt_watchlist"


def test_movie_dedup_unchanged():
    # movies still key on tmdb (no behaviour change from the show fix)
    out = _gatherer(_GC())._dedup([
        {"type": "movie", "ids": {"tmdb": 603, "tvdb": None}, "source": "trakt_watchlist"},
        {"type": "movie", "ids": {"tmdb": 603, "tvdb": None}, "source": "people_cooccurrence"},
    ])
    assert len(out) == 1 and out[0]["source"] == "trakt_watchlist"

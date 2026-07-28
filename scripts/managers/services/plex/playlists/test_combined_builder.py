"""Tests for the combined (movie + TV) playlist builder core (_build_for_users)."""
from __future__ import annotations

from scripts.managers.services.plex.playlists.combined_builder import (
    _GLIDE_PLAN_KEY,
    _PLAN_KEY,
    _TOUCHGO_PLAN_KEY,
    CombinedPlaylistBuilderManager,
)


class _Log:
    def __init__(self): self.infos = []; self.warns = []; self.grids = []; self.files = {}
    def log_info(self, m): self.infos.append(m)
    def log_warning(self, m): self.warns.append(m)
    def log_error(self, m): pass
    def log_grid(self, headers, rows, title="", cap=16, caption=""):
        self.grids.append((title, headers, rows))
    def log_to_file(self, category, message, *, reset=False):
        self.files.setdefault(category, []).append(message)


class _Cache:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def set(self, k, v): self.d[k] = v


def _mgr(cache, config=None):
    m = CombinedPlaylistBuilderManager.__new__(CombinedPlaylistBuilderManager)
    m.global_cache = cache
    m.logger = _Log()
    m.config = config if config is not None else {}
    m.registry = None
    m.dry_run = False
    return m


def _ep(sid, s, e, jk, title="ep"):
    return {"series_id": sid, "season_number": s, "episode_number": e, "tvdb_join_key": jk,
            "title": title, "air_date_utc": f"2020-01-0{e}", "is_special": s == 0}


def _movie(tmdb, title, year, score=None, cert=None):
    return {"tmdb_id": tmdb, "title": title, "year": year, "watchability_score": score,
            "certification": cert, "in_cinemas_date": f"{year}-01-01"}


_TRACKED = [{"safe_user": "rob", "title": "Rob"}]


def test_combined_plan_merges_tv_and_movies_with_kind_and_why():
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1", "Pilot")]
    owned_movies = [_movie(603, "The Matrix", 1999, score=70)]
    tv_inv = {"100:1:1": {"rating_key": "e", "series_title": "Show", "title": "Pilot"}}
    mv_inv = {"603": {"rating_key": "m", "title": "The Matrix", "year": 1999}}
    m = _mgr(cache)
    res = m._build_for_users(_TRACKED, owned_eps, owned_movies, tv_inv, mv_inv,
                             {1: 80.0}, {1: ["Action"]}, {}, {"rob": set()},
                             {"rob": set()}, {"rob": set()}, {"rob": {}})
    assert res == {"users": 1, "built": 1, "can_build": True}
    items = cache.get(f"{_PLAN_KEY}/rob")["items"]
    assert {it["rating_key"] for it in items} == {"e", "m"}      # BOTH mediums in one plan
    # The per-item preview (# | Title | Kind | Rank | Why) now lives ONLY in the playlists.log
    # mirror; the run log gets the compact per-profile summary instead.
    mirror = m.logger.files["playlists"]
    assert any(" | TV | " in ln for ln in mirror) and any(" | Movie | " in ln for ln in mirror)
    title, headers, _rows = m.logger.grids[0]
    assert "Combined (movie+TV) playlists" in title
    assert "Kind" not in headers and "Title" not in headers


def test_preview_de_identifies_run_log_but_keeps_name_in_playlists_log():
    # Privacy: the run log (summary rows + [ComboPlaylists] line) must carry the de-identified
    # handle, while the LOCAL playlists.log mirror keeps the real name for validation.
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1", "Pilot")]
    owned_movies = [_movie(603, "The Matrix", 1999, score=70)]
    tv_inv = {"100:1:1": {"rating_key": "e", "series_title": "Show", "title": "Pilot"}}
    mv_inv = {"603": {"rating_key": "m", "title": "The Matrix", "year": 1999}}
    m = _mgr(cache, {"plex": {"playlists": {"profile_ages": {"Rob": "adult"}}}})
    m._build_for_users(_TRACKED, owned_eps, owned_movies, tv_inv, mv_inv,
                       {1: 80.0}, {1: ["Action"]}, {}, {"rob": set()},
                       {"rob": set()}, {"rob": set()}, {"rob": {}})
    grid_title, _headers, grid_rows = m.logger.grids[0]
    combo_line = " ".join(m.logger.infos)
    flat = " ".join(str(c) for r in grid_rows for c in r)
    assert "Rob" not in grid_title and "Rob" not in flat              # summary → de-identified
    assert "R - adult 1" in flat
    assert "'Rob'" not in combo_line and "R - " in combo_line         # candidate line → de-identified
    assert any("'Rob'" in ln for ln in m.logger.files.get("playlists", []))   # file → real name


def test_mood_lists_split_in_progress_from_standalones():
    # The Long Glide = in-progress show (series 1, an ep watched); Touch & Go = standalone movie.
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1", "Pilot"), _ep(1, 1, 2, "100:1:2", "Ep2")]
    owned_movies = [_movie(603, "The Matrix", 1999, score=70)]
    tv_inv = {"100:1:1": {"rating_key": "e1", "series_title": "Show", "title": "Pilot"},
              "100:1:2": {"rating_key": "e2", "series_title": "Show", "title": "Ep2"}}
    mv_inv = {"603": {"rating_key": "m", "title": "The Matrix", "year": 1999}}
    m = _mgr(cache, {"plex": {"playlists": {"mood_lists": {"enabled": True}}}})
    m._build_for_users(_TRACKED, owned_eps, owned_movies, tv_inv, mv_inv,
                       {1: 80.0}, {1: ["Action"]}, {}, {"rob": set()},
                       {"rob": {"e1"}}, {"rob": set()}, {"rob": {}})   # e1 watched → series 1 started
    glide = {it["rating_key"] for it in cache.get(f"{_GLIDE_PLAN_KEY}/rob")["items"]}
    touchgo = {it["rating_key"] for it in cache.get(f"{_TOUCHGO_PLAN_KEY}/rob")["items"]}
    assert glide == {"e2"}                                  # in-progress show's next ep
    assert touchgo == {"m"}                                 # the standalone movie


def test_up_next_resume_boost_lifts_in_progress_tv_show():
    # REGRESSION (review): with resume_boost on, the blended Up Next must lift an in-progress SHOW
    # too (series_recency must reach the up_next build, not only the mood lists).
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1", "Pilot"), _ep(1, 1, 2, "100:1:2", "Ep2")]
    owned_movies = [_movie(603, "Hi Movie", 1999, score=99)]        # high-affinity standalone
    tv_inv = {"100:1:1": {"rating_key": "e1", "series_title": "Show", "title": "Pilot"},
              "100:1:2": {"rating_key": "e2", "series_title": "Show", "title": "Ep2"}}
    mv_inv = {"603": {"rating_key": "m", "title": "Hi Movie", "year": 1999}}
    _mgr(cache)._build_for_users(_TRACKED, owned_eps, owned_movies, tv_inv, mv_inv,
                                 {1: 30.0}, {1: ["Action"]}, {}, {"rob": set()},
                                 {"rob": {"e1"}}, {"rob": set()}, {"rob": {}},
                                 resume_boost=True)                  # e1 watched → series 1 in-progress
    assert cache.get(f"{_PLAN_KEY}/rob")["items"][0]["rating_key"] == "e2"   # show leads the standalone


def test_mood_lists_off_by_default_not_built():
    cache = _Cache()
    owned_movies = [_movie(603, "The Matrix", 1999, score=70)]
    mv_inv = {"603": {"rating_key": "m", "title": "The Matrix", "year": 1999}}
    _mgr(cache)._build_for_users(_TRACKED, [], owned_movies, {}, mv_inv, {}, {}, {},
                                 {"rob": set()}, {"rob": set()}, {"rob": set()}, {"rob": {}})
    assert cache.get(f"{_GLIDE_PLAN_KEY}/rob") is None and cache.get(f"{_TOUCHGO_PLAN_KEY}/rob") is None


def test_no_inventory_short_circuits():
    cache = _Cache(); m = _mgr(cache)
    res = m._build_for_users(_TRACKED, [], [], {}, {}, {}, {}, {}, {"rob": set()},
                             {"rob": set()}, {"rob": set()}, {"rob": {}})
    assert res["can_build"] is False and res["built"] == 0
    assert m.logger.grids == []                                 # no plans → no summary table
    assert any("owned_inventory" in w for w in m.logger.warns)


# ── run-log SUMMARY table (replaces the per-playlist 25-row preview grids) ─────
def test_combined_emits_one_summary_grid_covering_every_family_and_profile():
    """Three playlist families x two profiles = six ROWS in ONE table (previously six
    25-row preview grids in the run log)."""
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1", "Pilot"), _ep(1, 1, 2, "100:1:2", "Ep2")]
    owned_movies = [_movie(2, "Kid Film", 2000, score=50, cert="G"),
                    _movie(3, "Adult Film", 2010, score=90, cert="R")]
    tv_inv = {"100:1:1": {"rating_key": "e1", "series_title": "S", "title": "p"},
              "100:1:2": {"rating_key": "e2", "series_title": "S", "title": "q"}}
    mv_inv = {"2": {"rating_key": "kid"}, "3": {"rating_key": "adult"}}
    tracked = [{"safe_user": "rob", "title": "Rob"},
               {"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    m = _mgr(cache, {"plex": {"playlists": {"mood_lists": {"enabled": True}}}})
    m._build_for_users(tracked, owned_eps, owned_movies, tv_inv, mv_inv,
                       {1: 80.0}, {1: ["Drama"]}, {1: "TV-14"},
                       {"rob": set(), "wyatt": set()}, {"rob": {"e1"}, "wyatt": set()},
                       {"rob": set(), "wyatt": set()}, {"rob": {}, "wyatt": {}})
    assert len(m.logger.grids) == 1
    _title, headers, rows = m.logger.grids[0]
    assert [r[0] for r in rows] == ["Up Next", "The Long Glide", "Touch & Go"] * 2
    kid = dict(zip(headers, rows[3]))                           # the kid's blended Up Next
    assert kid["Profile"] == "W - little_kid 2"
    assert kid["Allowed"] == "TV-G/G" and kid["Strictest"] == "G"   # TV-14 + R both gated out
    assert kid["Cert"] == "OK"


def test_combined_summary_flags_an_over_cert_item_across_both_mediums(monkeypatch):
    """The cert evidence spans BOTH media: with the gate forced open, the TV-MA series and
    the R movie that leak into a little-kid plan are both counted in one flagged row."""
    import scripts.managers.services.plex.playlists.combined_builder as CB
    monkeypatch.setattr(CB, "cert_allowed", lambda cert, level, csm_age=None: True)
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1")]
    owned_movies = [_movie(2, "Kid Film", 2000, score=50, cert="G"),
                    _movie(3, "Adult Film", 2010, score=90, cert="R")]
    tv_inv = {"100:1:1": {"rating_key": "e", "series_title": "S", "title": "p"}}
    mv_inv = {"2": {"rating_key": "kid"}, "3": {"rating_key": "adult"}}
    m = _mgr(cache)
    m._build_for_users([{"safe_user": "lil", "title": "Lily", "restriction_profile": "little_kid"}],
                       owned_eps, owned_movies, tv_inv, mv_inv, {1: 80.0}, {1: ["Drama"]},
                       {1: "TV-MA"}, {"lil": set()}, {"lil": set()}, {"lil": set()}, {"lil": {}})
    row = dict(zip(m.logger.grids[0][1], m.logger.grids[0][2][0]))
    assert row["Items"] == "3" and row["Allowed"] == "TV-G/G"
    assert row["Strictest"] == "TV-MA"                          # most mature across TV + movie
    assert row["Cert"] == "VIOLATION x2"                        # the TV-MA episode AND the R film


def test_combined_age_gates_both_mediums_for_a_kid():
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1")]                       # series 1 = TV-MA
    owned_movies = [_movie(2, "Kid Film", 2000, score=50, cert="G"),
                    _movie(3, "Adult Film", 2010, score=90, cert="R")]
    tv_inv = {"100:1:1": {"rating_key": "e", "series_title": "S", "title": "p"}}
    mv_inv = {"2": {"rating_key": "kid"}, "3": {"rating_key": "adult"}}
    tracked = [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    m = _mgr(cache)
    m._build_for_users(tracked, owned_eps, owned_movies, tv_inv, mv_inv,
                       {1: 80.0}, {1: ["Drama"]}, {1: "TV-MA"}, {"wyatt": set()},
                       {"wyatt": set()}, {"wyatt": set()}, {"wyatt": {}})
    rks = {it["rating_key"] for it in cache.get(f"{_PLAN_KEY}/wyatt")["items"]}
    assert rks == {"kid"}                                       # TV-MA series + R movie both gated out


def test_combined_csm_age_fallback_for_uncertified_titles():
    # An uncertified series AND an uncertified movie surface for a kid via CSM age (matching
    # the standalone TV/movie builders); a high-CSM-age uncertified title stays gated out.
    cache = _Cache()
    owned_eps = [_ep(1, 1, 1, "100:1:1")]                       # series 1: no cert, CSM 4 → keep
    owned_movies = [_movie(2, "NoCert Kid", 2000, score=50, cert=None),    # CSM 5 → keep
                    _movie(3, "NoCert Adult", 2010, score=90, cert=None)]  # CSM 17 → drop
    tv_inv = {"100:1:1": {"rating_key": "e", "series_title": "S", "title": "p"}}
    mv_inv = {"2": {"rating_key": "kid"}, "3": {"rating_key": "adult"}}
    tracked = [{"safe_user": "wyatt", "title": "Wyatt", "restriction_profile": "little_kid"}]
    m = _mgr(cache)
    m._build_for_users(tracked, owned_eps, owned_movies, tv_inv, mv_inv,
                       {1: 80.0}, {1: ["Animation"]}, {}, {"wyatt": set()},
                       {"wyatt": set()}, {"wyatt": set()}, {"wyatt": {}},
                       series_csm_ages={1: 4}, movie_csm_ages={2: 5, 3: 17})
    rks = {it["rating_key"] for it in cache.get(f"{_PLAN_KEY}/wyatt")["items"]}
    assert rks == {"e", "kid"}                                  # CSM-young TV + movie kept, CSM-17 movie dropped

"""RadarrRepairAnomalyManager.triage_monitored_missing — the dedicated-4K-instance routing.

A monitored-missing title on the DISTINCT 4K instance must never be grabbed there at a sub-4K
quality (no 720p/1080p file in the 4K library): below the UHD threshold it unmonitors (the
lower-resolution baseline is the standard instance's job), and it NEVER adjusts-down-and-searches.
A title that warrants 4K — keep/universe tag, household-watched, or a score at/above the threshold —
searches at the 4K profile. The standard instance keeps its original adjust/search/unmonitor routing.

These exercise the manager wiring (instance detection + warrants_uhd) on top of the pure
``monitor_policy.triage_action`` decision (covered in test_monitor_policy.py). dry_run, so no API.
"""
from __future__ import annotations

from scripts.managers.services.radarr.repair.anomaly import RadarrRepairAnomalyManager

_SCORES = {2001: 80, 2002: 40, 2003: 10, 2004: 40}   # tmdb -> forced watchability score


class _Logger:
    def log_info(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_error(self, *a, **k): pass
    def log_success(self, *a, **k): pass
    def log_grid(self, *a, **k): pass
    def log_table(self, *a, **k): pass


class _Cache:
    """Serves the quality-profile list for the triage's HD-720p lookup; everything else default."""
    def __init__(self, profiles):
        self._profiles = profiles

    def get(self, key, default=None):
        if key.startswith("radarr.quality."):
            return self._profiles
        return default

    def set(self, key, val): pass


class _People:
    """Non-empty credits so credits_fetched is True (else a sub-threshold title defers, not acts)."""
    def get_people(self, tmdb):
        return {"cast": [1]}


def _missing(tmdb):
    return {"movie_id": tmdb, "title": f"movie {tmdb}", "year": 2015,
            "tmdb_id": tmdb, "monitored": True, "has_file": False}


def _movie(tmdb, *, qp=1):
    # isAvailable True so release_available passes; qualityProfileId != the HD-720p id (99).
    return {"id": tmdb, "tmdbId": tmdb, "title": f"movie {tmdb}", "year": 2015,
            "isAvailable": True, "qualityProfileId": qp, "ratings": {}}


def _mgr(instance_is_4k, *, keep_tmdbs=(), watched_tmdbs=(), profiles=None):
    m = object.__new__(RadarrRepairAnomalyManager)
    m.config = {"routing": {"movies": {"4k_dual_min_score": 75}}}
    m.logger = _Logger()
    m.dry_run = True
    m.radarr_api = object()        # never called in dry_run
    m.global_cache = _Cache(profiles if profiles is not None else [])
    m._resolve_instance = lambda i: i
    m._uhd_instance_name = lambda: "ultra"
    m.find_monitored_missing_files = lambda inst: [_missing(t) for t in _SCORES]
    movies = {t: _movie(t) for t in _SCORES}
    ctx = {
        "all_movies": list(movies.values()),
        "movie_by_tmdb": movies, "movie_by_id": movies,
        "genre_affinity": {}, "watched_tmdb_ids": set(watched_tmdbs),
        "collection_members": {}, "tag_label_map": {}, "people_mgr": _People(),
    }
    m._build_scoring_context = lambda inst: ctx
    keep = set(keep_tmdbs)
    m._resolve_keep_policy = lambda movie, tlm: ("keep_movie" if movie.get("tmdbId") in keep else None)
    return m


def _patch_score(monkeypatch):
    monkeypatch.setattr(
        "scripts.managers.services.trakt.movies.scorer.score_movie",
        lambda **kw: _SCORES.get(kw["movie"].get("tmdbId"), 0),
    )


def test_4k_instance_unmonitors_sub_threshold_never_adjusts(monkeypatch):
    _patch_score(monkeypatch)
    m = _mgr(instance_is_4k=True)            # triage runs ON 'ultra'
    stats = m.triage_monitored_missing("ultra")
    # 2001 (score 80) warrants 4K -> search; 2002 (40) & 2003 (10) & 2004 (40) don't -> unmonitor.
    assert stats["searched"] == 1, stats
    assert stats["unmonitored"] == 3, stats
    assert stats["adjusted_and_searched"] == 0, stats   # the drop-to-720p branch is unreachable here


def test_4k_instance_keep_tag_and_watched_warrant_search(monkeypatch):
    _patch_score(monkeypatch)
    # 2003 keep-tagged (score 10) and 2004 household-watched (score 40) now warrant 4K despite a
    # sub-threshold score; only 2002 (40, no tag/watch) unmonitors. 2001 (80) searches as before.
    m = _mgr(instance_is_4k=True, keep_tmdbs=(2003,), watched_tmdbs=(2004,))
    stats = m.triage_monitored_missing("ultra")
    assert stats["searched"] == 3, stats        # 2001, 2003, 2004
    assert stats["unmonitored"] == 1, stats      # only 2002
    assert stats["adjusted_and_searched"] == 0, stats


def test_standard_instance_routing_unchanged(monkeypatch):
    _patch_score(monkeypatch)
    # Same titles, but triage runs on 'standard' (not the 4K instance). With an HD-720p profile
    # present, the marginal 40-score titles adjust-and-search and the 80 searches — the legacy path.
    profiles = [{"id": 1, "name": "Ultra-HD"}, {"id": 99, "name": "HD-720p"}]
    m = _mgr(instance_is_4k=False, profiles=profiles)
    stats = m.triage_monitored_missing("standard")
    assert stats["searched"] == 1, stats                 # 2001 (80)
    assert stats["adjusted_and_searched"] == 2, stats    # 2002/2004 (20<=score<60) drop to 720p
    assert stats["unmonitored"] == 1, stats              # 2003 (score 10, below the floor)


# ── standard-baseline acquisition (the demote completion) ─────────────────────────
def _profile(pid, name, res):
    return {"id": pid, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_PROFS = [_profile(3, "HD-1080", 1080), _profile(2, "HD-720", 720), _profile(1, "SD-480", 480)]


class _FakeGw:
    def __init__(self): self.added = []
    def add(self, inst, payload): self.added.append((inst, payload)); return {"id": 1}


def _bare_mgr(dry_run=False):
    m = object.__new__(RadarrRepairAnomalyManager)
    m.logger = _Logger()
    m.dry_run = dry_run
    return m


def test_acquire_standard_baseline_picks_profile_by_score():
    m, gw = _bare_mgr(), _FakeGw()
    mv = {"id": 9, "tmdbId": 555, "title": "X", "year": 2015,
          "movieFile": {"id": 1}, "path": "/4k/X", "folderName": "X (2015)"}
    assert m._acquire_standard_baseline(gw, "standard", "/std", _PROFS, set(), mv, 70) == "acquired"
    inst, p = gw.added[0]
    assert inst == "standard"
    assert p["qualityProfileId"] == 3                    # score 70 → 1080 tier → HD-1080
    assert p["rootFolderPath"] == "/std" and p["monitored"] is True
    assert p["addOptions"] == {"searchForMovie": True}
    assert all(k not in p for k in ("id", "movieFile", "movieFileId", "path", "folderName"))


def test_acquire_standard_baseline_lower_score_lower_profile():
    # The matrix (target_resolution_for_score): >=35 → 1080, >=20 → 720, else 480 (all capped <4K).
    m, gw = _bare_mgr(), _FakeGw()
    m._acquire_standard_baseline(gw, "standard", "/std", _PROFS, set(), {"tmdbId": 556, "title": "Y"}, 25)
    assert gw.added[0][1]["qualityProfileId"] == 2       # score 25 → 720 tier
    gw2 = _FakeGw()
    m._acquire_standard_baseline(gw2, "standard", "/std", _PROFS, set(), {"tmdbId": 557, "title": "Z"}, 10)
    assert gw2.added[0][1]["qualityProfileId"] == 1       # score 10 → 480 tier


def test_acquire_standard_baseline_skips_when_already_present():
    m, gw = _bare_mgr(), _FakeGw()
    assert m._acquire_standard_baseline(gw, "standard", "/std", _PROFS, {555}, {"tmdbId": 555}, 70) == "present"
    assert gw.added == []


def test_acquire_standard_baseline_dry_run_does_not_add():
    m, gw = _bare_mgr(dry_run=True), _FakeGw()
    assert m._acquire_standard_baseline(gw, "standard", "/std", _PROFS, set(),
                                        {"tmdbId": 555, "title": "X"}, 70) == "would-acquire"
    assert gw.added == []


class _StdIm:
    """Fake instance-manager backing the rehome gateway: serves the standard library/profiles/roots
    and records movie POST (acquire) + movie/editor PUT (unmonitor). Queue/command calls no-op."""
    def __init__(self, std_lib=(), profiles=(), roots=()):
        self._lib = {"standard": list(std_lib)}
        self._profiles = {"standard": list(profiles)}
        self._roots = {"standard": list(roots)}
        self.adds, self.puts = [], []

    def _make_request(self, name, endpoint, method="GET", payload=None, fallback=None, **kw):
        if method == "POST" and endpoint == "movie":
            self.adds.append((name, payload)); return {"id": 1}
        if method == "PUT" and endpoint == "movie/editor":
            self.puts.append((name, payload)); return {"ok": True}
        if method == "POST":
            return {"id": 1}
        table = {"movie": self._lib, "qualityprofile": self._profiles, "rootfolder": self._roots}
        return table.get(endpoint, {}).get(name, fallback if fallback is not None else [])


def _rehome_mgr(im, *, dry_run, std_present=()):
    m = object.__new__(RadarrRepairAnomalyManager)
    m.config = {"routing": {"movies": {"4k_dual_min_score": 75, "triage_rehome_to_standard": True}},
                "movieRootFolders": {"standard": "/std"},
                "radarr_instances": {"standard": {}, "ultra": {}, "default_instance": "standard"}}
    m.logger = _Logger()
    m.dry_run = dry_run
    m.radarr_api = im
    m.instance_manager = im
    m.global_cache = _Cache([])
    m._resolve_instance = lambda i: i
    m._uhd_instance_name = lambda: "ultra"
    m.find_monitored_missing_files = lambda inst: [_missing(t) for t in _SCORES]
    movies = {t: _movie(t) for t in _SCORES}
    ctx = {"all_movies": list(movies.values()), "movie_by_tmdb": movies, "movie_by_id": movies,
           "genre_affinity": {}, "watched_tmdb_ids": set(), "collection_members": {},
           "tag_label_map": {}, "people_mgr": _People()}
    m._build_scoring_context = lambda inst: ctx
    m._resolve_keep_policy = lambda movie, tlm: None
    return m


def test_triage_4k_rehome_dry_run_counts_baseline_acquires(monkeypatch):
    _patch_score(monkeypatch)
    im = _StdIm(std_lib=[], profiles=_PROFS, roots=[{"path": "/std"}])
    stats = _rehome_mgr(im, dry_run=True).triage_monitored_missing("ultra")
    # 2002/2003/2004 (<75) unmonitor on ultra → each WOULD acquire a ≤1080 baseline on standard.
    assert stats["unmonitored"] == 3
    assert stats.get("rehomed_to_standard") == 3
    assert im.adds == []                                  # dry-run: nothing actually added


def test_triage_4k_rehome_live_acquires_score_matched_baselines(monkeypatch):
    _patch_score(monkeypatch)
    im = _StdIm(std_lib=[], profiles=_PROFS, roots=[{"path": "/std"}])
    stats = _rehome_mgr(im, dry_run=False).triage_monitored_missing("ultra")
    adds = {p["tmdbId"]: p for n, p in im.adds if n == "standard"}
    assert set(adds) == {2002, 2003, 2004}               # the three demoted titles land on standard
    assert adds[2002]["qualityProfileId"] == 3           # score 40 → 1080 (matrix: >=35)
    assert adds[2003]["qualityProfileId"] == 1           # score 10 → 480
    assert adds[2004]["qualityProfileId"] == 3
    assert all(p["addOptions"] == {"searchForMovie": True} and p["monitored"] is True
               for p in adds.values())
    assert stats.get("rehomed_to_standard") == 3
    # the 4K records were unmonitored (batched) — never grabbed at sub-4K on the 4K instance
    assert any(p.get("monitored") is False for _, p in im.puts)


def test_triage_4k_rehome_skips_titles_already_on_standard(monkeypatch):
    _patch_score(monkeypatch)
    # 2002 already has a standard record → not re-acquired; 2003/2004 are.
    im = _StdIm(std_lib=[{"tmdbId": 2002}], profiles=_PROFS, roots=[{"path": "/std"}])
    stats = _rehome_mgr(im, dry_run=False).triage_monitored_missing("ultra")
    added = {p["tmdbId"] for n, p in im.adds if n == "standard"}
    assert added == {2003, 2004}                          # 2002 left to standard's own management
    assert stats.get("rehomed_to_standard") == 2


def test_triage_4k_rehome_off_by_default(monkeypatch):
    _patch_score(monkeypatch)
    im = _StdIm(std_lib=[], profiles=_PROFS, roots=[{"path": "/std"}])
    m = _rehome_mgr(im, dry_run=False)
    m.config["routing"]["movies"]["triage_rehome_to_standard"] = False   # flag off → no acquisition
    stats = m.triage_monitored_missing("ultra")
    assert im.adds == []
    assert stats.get("rehomed_to_standard", 0) == 0

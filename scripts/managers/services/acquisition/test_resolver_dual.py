"""Tests for the resolver's dual-version (1080p baseline + 4K bonus) add-time decisions —
``dual_active`` / ``apply_hd_baseline`` / ``plan_uhd_companion``. The contract:

  • dual is ACTIVE only when routing.configured + movies.4k_policy=='both' + a MOVIE + a
    NON-anime route + a DISTINCT 4K Radarr instance (categorized label resolves to a session
    other than the default). Otherwise every method is a no-op → today's single-copy behaviour.
  • the PRIMARY copy is re-capped to a score-adaptive <=1080 baseline on the standard instance
    (even if that instance also offers a 2160p profile — the 4K copy lives only on the 4K side).
  • the COMPANION is the highest (2160p) profile on the 4K instance, routed to movieRootFolders['4k'],
    emitted only when dual_version.wants_uhd is satisfied and the title isn't already a 4K copy.
"""
from __future__ import annotations

from scripts.managers.services.acquisition.resolver import Resolver


class _Logger:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass
    def log_success(self, *a, **k): pass


def _profile(name, res):
    return {"id": res, "name": name, "items": [{"allowed": True, "quality": {"name": name, "resolution": res}}]}


_STD = [_profile("HD-720", 720), _profile("HD-1080", 1080)]
_STD_WITH_4K = _STD + [_profile("UHD-std", 2160)]
_UHD = [_profile("HD-1080", 1080), _profile("UHD-2160", 2160)]
_MRF = {"standard": "/m/std", "kids": "/m/kids", "anime": "/m/anime", "4k": "/m/4k"}


class _GW:
    available = True

    def __init__(self, obj, *, cat=None, profiles=None, in_lib=None):
        self._obj = obj
        self._cat = cat or {}
        self._profiles = profiles or {}
        self._in_lib = set(in_lib or ())

    def default_instance(self): return "standard"
    def categorized_instance(self, label="1080p"): return self._cat.get(label, "standard")
    def lookup(self, inst, term): return [self._obj]
    def in_library(self, inst, id_field, value): return (inst, str(value)) in self._in_lib
    def quality_profiles(self, inst): return self._profiles.get(inst, [])
    def root_folders(self, inst): return [{"path": "/fallback"}]


def _resolver(*, obj=None, both=True, with_4k=True, std_profiles=None, min_score=0, in_lib=None):
    obj = obj or {"tmdbId": 862, "genres": ["Drama"], "runtime": 81}
    cat = {"4K": "uhd"} if with_4k else {}
    profiles = {"standard": std_profiles or _STD, "uhd": _UHD}
    gw = _GW(obj, cat=cat, profiles=profiles, in_lib=in_lib)
    movies = {"4k_policy": "both" if both else "highest_only",
              "4k_dual_min_score": min_score, "kids_bucket_enabled": False}
    cfg = {"routing": {"configured": True, "movies": movies, "tv": {}},
           "movieRootFolders": dict(_MRF), "rootFolders": {},
           "radarr_instances_categorized": cat}
    r = Resolver({"radarr": gw}, cfg, _Logger())
    r._movie_age_cache = {}
    return r


def _movie(r, score=87, tmdb=862):
    e = r.prepare({"type": "movie", "ids": {"tmdb": tmdb}, "title": "Toy Story"})
    e["score"] = score
    return e


# ── dual_active gate ──────────────────────────────────────────────────────────
def test_dual_active_when_both_and_distinct_4k():
    r = _resolver(both=True, with_4k=True)
    assert r.dual_active(_movie(r)) is True


def test_dual_inactive_for_highest_only():
    r = _resolver(both=False, with_4k=True)
    assert r.dual_active(_movie(r)) is False


def test_dual_inactive_without_distinct_4k_instance():
    r = _resolver(both=True, with_4k=False)            # "4K" label resolves back to default
    assert r.dual_active(_movie(r)) is False


def test_dual_inactive_without_configured_stamp():
    r = _resolver(both=True, with_4k=True)
    r._routing_on = False                               # never-onboarded install
    assert r.dual_active(_movie(r)) is False


def test_dual_inactive_for_anime_route():
    obj = {"tmdbId": 99, "genres": ["Animation"], "originalLanguage": {"name": "Japanese"}, "runtime": 90}
    r = _resolver(obj=obj, both=True, with_4k=True)
    e = _movie(r, tmdb=99)
    assert e["category"] == "anime"
    assert r.dual_active(e) is False                    # anime rides the dedicated instance, single copy


def test_dual_inactive_for_show():
    r = _resolver(both=True, with_4k=True)
    assert r.dual_active({"type": "show", "category": "standard"}) is False


# ── apply_hd_baseline: <=1080 cap ─────────────────────────────────────────────
def test_apply_hd_baseline_caps_below_4k_even_with_2160_profile_on_standard():
    r = _resolver(both=True, with_4k=True, std_profiles=_STD_WITH_4K)
    e = _movie(r, score=95)                             # 95 -> 2160 tier normally
    r.apply_hd_baseline(e)
    assert e["quality_profile"]["max_res"] == 1080      # clamped to the HD baseline
    assert e.get("dual_baseline") is True


def test_apply_hd_baseline_adaptive_to_lower_score():
    r = _resolver(both=True, with_4k=True)
    e = _movie(r, score=25)                             # mid score -> 720 baseline
    r.apply_hd_baseline(e)
    assert e["quality_profile"]["max_res"] == 720


def test_apply_hd_baseline_noop_for_highest_only():
    r = _resolver(both=False, with_4k=True, std_profiles=_STD_WITH_4K)
    e = _movie(r, score=95)
    before = dict(e["quality_profile"])
    r.apply_hd_baseline(e)
    assert e["quality_profile"] == before              # untouched
    assert "dual_baseline" not in e


# ── plan_uhd_companion ────────────────────────────────────────────────────────
def _ok(_inst): return True
def _denied(_inst): return False


def test_companion_emits_on_4k_instance_at_2160():
    r = _resolver(both=True, with_4k=True)
    c = r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok)
    assert c is not None
    assert c["instance"] == "uhd"
    assert c["quality_profile"]["max_res"] == 2160
    assert c["root_folder"] == "/m/4k"
    assert c["is_uhd_companion"] is True


def test_companion_none_for_highest_only():
    r = _resolver(both=False, with_4k=True)
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok) is None


def test_companion_none_without_distinct_4k_instance():
    r = _resolver(both=True, with_4k=False)
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok) is None


def test_companion_none_when_space_denied():
    r = _resolver(both=True, with_4k=True)
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_denied) is None


def test_companion_none_when_score_below_default_threshold():
    r = _resolver(both=True, with_4k=True, min_score=0)   # 0 -> default 70
    assert r.plan_uhd_companion(_movie(r, score=55), space_ok=_ok) is None


def test_companion_emits_above_default_threshold():
    r = _resolver(both=True, with_4k=True, min_score=0)
    assert r.plan_uhd_companion(_movie(r, score=75), space_ok=_ok) is not None


def test_companion_honours_explicit_lower_threshold():
    r = _resolver(both=True, with_4k=True, min_score=30)
    assert r.plan_uhd_companion(_movie(r, score=40), space_ok=_ok) is not None


def test_companion_none_when_already_in_4k_library():
    r = _resolver(both=True, with_4k=True, in_lib={("uhd", "862")})
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok) is None


# ── can_remote_play gate (Stage C) ────────────────────────────────────────────
def test_companion_suppressed_when_remote_play_false():
    # a likely device would transcode this 2160p file → the high-score 4K bonus is suppressed
    r = _resolver(both=True, with_4k=True)
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok, can_remote_play=False) is None


def test_companion_still_emits_for_keep_tagged_even_when_remote_play_false():
    # keep/universe-tagged titles bypass the remote-play gate by design (wants_uhd ANDs
    # can_remote_play onto the SCORE branch only) — they still get the 4K copy.
    r = _resolver(both=True, with_4k=True)
    c = r.plan_uhd_companion(_movie(r, score=10), space_ok=_ok,
                             keep_tagged=True, can_remote_play=False)
    assert c is not None and c["instance"] == "uhd"


def test_companion_default_remote_play_true_emits():
    # default arg is True → unchanged from pre-Stage-C behaviour (regression lock)
    r = _resolver(both=True, with_4k=True)
    assert r.plan_uhd_companion(_movie(r, score=87), space_ok=_ok) is not None

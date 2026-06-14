"""Tests for resolve_category_roots — the shared router_show / router_movie target
resolver that follows the LIVE *arr instance (config rootFolders is only an override).

Pins the behaviour change away from the old "config==registered or bust" gate:
  • live folders are matched by leaf-name (documentary→documentaries, 4k→uhd…),
  • stale config that no longer matches a registered folder is ignored,
  • a still-valid config override is honoured,
  • a category with no folder of its own inherits the default library (series/standard),
  • mirrors resolver._pick_root_folder's category precedence.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap: import the bare `sd_replace` module the same way the routers do, so this
# works whether pytest treats tools/ as a package or not.
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from sd_replace import resolve_category_roots  # noqa: E402

TV_CATS = ("anime", "kids", "reality", "documentary", "series")
TV_ALIASES = {"documentary": ("documentaries", "docs")}
MOVIE_CATS = ("anime", "kids", "4k", "standard")
MOVIE_ALIASES = {"4k": ("uhd", "2160", "2160p")}


def _reg(*paths):
    return [{"path": p} for p in paths]


# ── live Sonarr 720 layout: every category owns its folder, stale config ignored ──
def test_tv_live_folders_matched_by_leaf_stale_config_ignored():
    registered = _reg(
        "/data/media/tv/anime", "/data/media/tv/kids", "/data/media/tv/documentaries",
        "/data/media/tv/reality", "/data/media/tv/series",
    )
    stale = {"series": "/media/tv/series", "anime": "/media/tv/anime",
             "documentary": "/media/tv/documentary"}  # old /media paths, none registered
    roots = resolve_category_roots(registered, TV_CATS, stale, "series", TV_ALIASES)

    assert {c: roots[c]["path"] for c in TV_CATS} == {
        "anime": "/data/media/tv/anime",
        "kids": "/data/media/tv/kids",
        "reality": "/data/media/tv/reality",
        "documentary": "/data/media/tv/documentaries",   # singular cat → plural folder via alias
        "series": "/data/media/tv/series",
    }
    assert all(roots[c]["via"] == "own" for c in TV_CATS)


# ── a category with no folder of its own inherits 'series' (resolver precedence) ──
def test_tv_missing_category_inherits_series():
    registered = _reg("/data/media/tv/anime", "/data/media/tv/series")
    roots = resolve_category_roots(registered, TV_CATS, {}, "series", TV_ALIASES)

    assert roots["anime"] == {"path": "/data/media/tv/anime", "via": "own", "reason": None}
    for cat in ("kids", "reality", "documentary"):
        assert roots[cat]["path"] == "/data/media/tv/series"
        assert roots[cat]["via"] == "inherit"


# ── a still-valid config override wins even when its leaf isn't the category name ──
def test_config_override_honoured_when_registered():
    registered = _reg("/data/tv/shows", "/data/tv/anime")
    cfg = {"series": "/data/tv/shows"}  # registered, but leaf 'shows' != 'series'
    roots = resolve_category_roots(registered, TV_CATS, cfg, "series", TV_ALIASES)

    assert roots["series"] == {"path": "/data/tv/shows", "via": "own", "reason": None}
    assert roots["anime"]["path"] == "/data/tv/anime"
    # kids/reality/documentary inherit the override-resolved series folder
    assert roots["kids"] == {"path": "/data/tv/shows", "via": "inherit", "reason": None}


# ── nothing registered → no usable target, helper reports a reason, not a crash ──
def test_no_registered_folders_yields_no_targets():
    roots = resolve_category_roots([], TV_CATS, {}, "series", TV_ALIASES)
    assert all(roots[c]["path"] is None for c in TV_CATS)
    assert all(roots[c]["reason"] for c in TV_CATS)
    assert {c: r["path"] for c, r in roots.items() if r["path"]} == {}


# ── live Radarr 'standard' layout: all four movie categories own their folder ──
def test_movie_live_folders_all_own():
    registered = _reg(
        "/data/media/movies/standard", "/data/media/movies/kids",
        "/data/media/movies/anime", "/data/media/movies/4k",
    )
    roots = resolve_category_roots(registered, MOVIE_CATS, None, "standard", MOVIE_ALIASES)
    assert {c: roots[c]["path"] for c in MOVIE_CATS} == {
        "anime": "/data/media/movies/anime",
        "kids": "/data/media/movies/kids",
        "4k": "/data/media/movies/4k",
        "standard": "/data/media/movies/standard",
    }
    assert all(roots[c]["via"] == "own" for c in MOVIE_CATS)


# ── movie 4k alias: a '4k' library folder named 'uhd' still matches ──
def test_movie_4k_alias_matches_uhd_folder():
    registered = _reg("/data/media/movies/standard", "/data/media/movies/uhd")
    roots = resolve_category_roots(registered, MOVIE_CATS, None, "standard", MOVIE_ALIASES)
    assert roots["4k"]["path"] == "/data/media/movies/uhd"
    assert roots["4k"]["via"] == "own"
    # anime/kids have no folder → inherit standard
    assert roots["anime"]["path"] == "/data/media/movies/standard"
    assert roots["anime"]["via"] == "inherit"


# ── trailing slashes / case differences don't defeat the match ──
def test_match_is_case_and_trailing_slash_insensitive():
    registered = _reg("/DATA/Media/TV/Anime/", "/data/media/tv/series")
    roots = resolve_category_roots(registered, TV_CATS, {}, "series", TV_ALIASES)
    assert roots["anime"]["path"] == "/DATA/Media/TV/Anime/"   # original casing preserved
    assert roots["anime"]["via"] == "own"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed.")
    sys.exit(1 if failed else 0)

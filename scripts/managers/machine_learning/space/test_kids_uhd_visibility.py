"""Tests for the kids-4K visibility gate (routing_targets.kids_uhd_*).

WHY THIS FILE EXISTS. The gate's failure is silent and lands on a child: a 2160p kids
film relocated outside the Kids Plex library simply vanishes from their profile, and the
only symptom is a child saying a movie is "gone". Nothing else in the run would report it.
So the fail-closed behaviour is pinned here, not merely documented.
"""
from scripts.managers.machine_learning.space import routing_targets as rt


def _cfg(kids_root=None, **extra):
    mv = dict(extra)
    if kids_root is not None:
        mv["uhd_root_folders"] = {"kids": kids_root, "standard": "/data/media/movies/4k"}
    return {"routing": {"movies": mv}}


# ── fails closed ──────────────────────────────────────────────────────────────

def test_no_config_at_all_is_not_visible():
    assert rt.kids_uhd_visible({}, ["/data/media/movies/kids"]) is False
    assert rt.kids_uhd_visible(None, ["/data/media/movies/kids"]) is False


def test_kids_root_configured_but_no_plex_paths_is_not_visible():
    # "we could not tell" must not read as "yes".
    cfg = _cfg("/data/media/movies/4k/kids")
    assert rt.kids_uhd_visible(cfg, []) is False
    assert rt.kids_uhd_visible(cfg, None) is False


def test_plex_has_kids_library_but_no_kids_uhd_root_configured():
    # Single-root install (today's default) -> no kids 4K, so nothing can go missing.
    assert rt.kids_uhd_visible(_cfg(), ["/data/media/movies/kids"]) is False


def test_kids_uhd_root_outside_every_kids_library_is_not_visible():
    """The exact production hazard: /4k/kids exists, but the Kids library only covers
    /movies/kids -- so a child would lose the film."""
    cfg = _cfg("/data/media/movies/4k/kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/kids"]) is False


# ── the happy path ────────────────────────────────────────────────────────────

def test_visible_when_kids_library_covers_the_uhd_root():
    cfg = _cfg("/data/media/movies/4k/kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/kids",
                                     "/data/media/movies/4k/kids"]) is True


def test_visible_when_a_kids_library_covers_a_PARENT_of_the_uhd_root():
    # A Kids library pointed at /movies/4k would include /movies/4k/kids.
    cfg = _cfg("/data/media/movies/4k/kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/4k"]) is True


def test_exact_match_counts_as_within():
    cfg = _cfg("/data/media/movies/4k/kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/4k/kids"]) is True


# ── path comparison is segment-wise, not string-prefix ────────────────────────

def test_sibling_with_a_shared_prefix_is_NOT_within():
    """/movies/4k-adult must not count as inside /movies/4k -- a naive startswith would
    make an ADULT library satisfy the kids gate, which is the worst possible false pass."""
    cfg = _cfg("/data/media/movies/4k-adult/kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/4k"]) is False


def test_trailing_slash_and_separator_style_do_not_matter():
    cfg = _cfg("/data/media/movies/4k/kids/")
    assert rt.kids_uhd_visible(cfg, ["\\data\\media\\movies\\4k"]) is True


def test_case_insensitive():
    cfg = _cfg("/Data/Media/Movies/4K/Kids")
    assert rt.kids_uhd_visible(cfg, ["/data/media/movies/4k"]) is True


# ── uhd_root_folders shape tolerance ──────────────────────────────────────────

def test_uhd_root_folders_defaults_empty_so_existing_installs_are_unchanged():
    assert rt.uhd_root_folders({}) == {}
    assert rt.uhd_root_folders({"routing": {}}) == {}
    assert rt.uhd_root_folders({"routing": {"movies": {}}}) == {}


def test_uhd_root_folders_ignores_a_non_dict():
    assert rt.uhd_root_folders({"routing": {"movies": {"uhd_root_folders": "nope"}}}) == {}


def test_kids_uhd_allowed_tracks_visibility():
    cfg = _cfg("/data/media/movies/4k/kids")
    # Returns (allowed, reason) now - the reason is what makes a refusal legible in the
    # log; a bare bool could only say no, never why.
    assert rt.kids_uhd_allowed(cfg, ["/data/media/movies/4k"]) == (True, "visible")
    allowed, why = rt.kids_uhd_allowed(cfg, ["/data/media/movies/kids"])
    assert allowed is False and why == "not-in-library"   # the reason a child's copy vanished

"""Cross-instance mirror rows — one physical (hardlinked) file, two Radarr records.

``routing.movies.4k_policy == "both"`` relocates a 2160p into the 4K instance with
``importMode=copy`` (a hardlink on the shared storage the flow is gated on) and leaves the
source file in place until the standard record's retune grabs a ≤1080p replacement. Until
that lands, both instances report ``hasFile=True`` for ONE file — and the per-instance
parquet refresh, which has no cross-instance view, wrote both rows at full size. Space
accounting then double-counted the bytes and the delete pool advertised reclaim that
unlinking one of two hardlinks cannot deliver.

These tests pin the detector: byte-identical only, resolution-tier authority, deterministic
across instances, and fail-safe when a neighbour's data is unavailable.
"""
from __future__ import annotations

from scripts.managers.services.radarr.cache.movie_files import RadarrCacheMovieFilesManager

CATS = {"720p": "standard", "1080p": "standard", "4K": "ultra", "anime": "standard"}
UHD = 52_855_844_372
HD = 8_624_416_135


class _Log:
    def log_info(self, *a, **k): pass
    def log_debug(self, *a, **k): pass
    def log_warning(self, *a, **k): pass


class _GC:
    def __init__(self, d): self.d = d
    def get(self, k): return self.d.get(k)


def _movie(tmdb, size, resolution, *, has_file=True):
    return {"id": tmdb % 10_000, "tmdbId": tmdb, "hasFile": has_file,
            "movieFile": {"size": size,
                          "quality": {"quality": {"resolution": resolution}}}}


def _mgr(libraries, *, instances=None, cats=CATS):
    m = object.__new__(RadarrCacheMovieFilesManager)
    m.logger = _Log()
    m.config = {"radarr_instances_categorized": cats,
                "radarr_instances": {i: {} for i in (instances or libraries)}}
    m.global_cache = _GC({f"radarr.movies.{i}.full": v for i, v in libraries.items()})
    m.radarr_api = None
    return m


# ── the bug ──────────────────────────────────────────────────────────────────

def test_byte_identical_uhd_pair_drops_the_non_4k_side():
    libs = {"standard": [_movie(299537, UHD, 2160)],
            "ultra": [_movie(299537, UHD, 2160)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == {299537}
    assert m._mirrored_tmdb_ids("ultra", libs["ultra"]) == set()


def test_exactly_one_side_is_dropped_so_the_title_never_vanishes():
    """Both refreshes must agree. If each instance dropped 'the other one's' row the
    title would disappear from the library entirely."""
    libs = {"standard": [_movie(1, UHD, 2160)], "ultra": [_movie(1, UHD, 2160)]}
    m = _mgr(libs)
    dropped = [i for i in libs if m._mirrored_tmdb_ids(i, libs[i])]
    assert dropped == ["standard"]


def test_mirror_detection_works_for_any_instance_pair():
    # the test instance holding a byte-identical 2160p is a mirror of the 4K instance's
    libs = {"test": [_movie(615656, UHD, 2160)], "ultra": [_movie(615656, UHD, 2160)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("test", libs["test"]) == {615656}
    assert m._mirrored_tmdb_ids("ultra", libs["ultra"]) == set()


# ── the false-positive guard ─────────────────────────────────────────────────

def test_genuine_dual_version_is_preserved():
    """A real 4K + real HD baseline: two files, two sizes, both legitimately on disk."""
    libs = {"standard": [_movie(299537, HD, 1080)],
            "ultra": [_movie(299537, UHD, 2160)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()
    assert m._mirrored_tmdb_ids("ultra", libs["ultra"]) == set()


def test_one_byte_of_difference_is_enough_to_keep_both():
    libs = {"standard": [_movie(1, UHD, 2160)], "ultra": [_movie(1, UHD + 1, 2160)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


def test_a_title_on_one_instance_only_is_never_a_mirror():
    libs = {"standard": [_movie(1, UHD, 2160)], "ultra": []}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


def test_fileless_records_are_ignored():
    libs = {"standard": [_movie(1, UHD, 2160)],
            "ultra": [_movie(1, None, 2160, has_file=False)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


def test_hd_pair_drops_the_4k_instances_copy_not_the_standard_one():
    """Authority follows the RESOLUTION TIER, not the instance name: a byte-identical
    1080p pair belongs to the 1080p instance."""
    libs = {"standard": [_movie(1, HD, 1080)], "ultra": [_movie(1, HD, 1080)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("ultra", libs["ultra"]) == {1}
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


# ── fail-safe behaviour ──────────────────────────────────────────────────────

def test_uncached_neighbour_never_causes_a_drop():
    """A neighbour whose movie list has not been pulled is simply not considered."""
    libs = {"standard": [_movie(1, UHD, 2160)]}
    m = _mgr(libs, instances=["standard", "ultra"])      # ultra configured but uncached
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


def test_no_other_instances_is_a_noop():
    libs = {"standard": [_movie(1, UHD, 2160)]}
    m = _mgr(libs)
    assert m._mirrored_tmdb_ids("standard", libs["standard"]) == set()


def test_missing_categorisation_still_resolves_deterministically():
    libs = {"alpha": [_movie(1, UHD, 2160)], "beta": [_movie(1, UHD, 2160)]}
    m = _mgr(libs, cats={})
    # alphabetically-first wins; the important property is that exactly one side drops
    assert m._mirrored_tmdb_ids("beta", libs["beta"]) == {1}
    assert m._mirrored_tmdb_ids("alpha", libs["alpha"]) == set()


def test_categorised_instance_absent_from_the_group_falls_back():
    libs = {"alpha": [_movie(1, UHD, 2160)], "beta": [_movie(1, UHD, 2160)]}
    m = _mgr(libs)          # cats point 4K at "ultra", which is not in this pair
    assert m._mirrored_tmdb_ids("alpha", libs["alpha"]) == set()
    assert m._mirrored_tmdb_ids("beta", libs["beta"]) == {1}


def test_three_way_group_keeps_exactly_one():
    libs = {"standard": [_movie(1, UHD, 2160)], "test": [_movie(1, UHD, 2160)],
            "ultra": [_movie(1, UHD, 2160)]}
    m = _mgr(libs)
    kept = [i for i in libs if not m._mirrored_tmdb_ids(i, libs[i])]
    assert kept == ["ultra"]

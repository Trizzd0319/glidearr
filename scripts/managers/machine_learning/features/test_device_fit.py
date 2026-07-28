"""features/test_device_fit.py — Group-D (device/playback fit) in the MOVIE path.

Regression gate for the bug where D1/D2/D3 were structurally dead for movies:

  * D1 needed ``target_resolution``, which ``score_movie_features`` never passed →
    always 0.0 for every movie in the library.
  * D2 read ``movie["videoCodec"]``, but Radarr nests the codec at
    ``movieFile.mediaInfo.videoCodec`` → the key was always absent, so D2 collapsed
    to the constant +2.0 "unknown codec" branch.
  * D3 needed ``target_resolution`` too → always 0.0.

Both inputs live in ``movie_files.parquet`` as ``resolution`` / ``video_codec`` and
are now marshalled by ``build_movie_feature_row`` and threaded by
``score_movie_features``. These tests use a SYNTHETIC platform/transcode matrix so
they never depend on the real household cache.
"""
from __future__ import annotations

from scripts.managers.machine_learning.features.movie_features import (
    build_movie_feature_row,
    score_movie_features,
)
from scripts.managers.machine_learning.scoring._shared import _DEVICE_RESOLUTION_CEILING

# Synthetic household: a 4K-capable primary (Windows), a 4K TV, two 1080p handhelds.
# Chosen so the capable-share rungs differ by target resolution.
PLATFORMS = {"Windows": 60, "Tizen": 20, "Android": 15, "iOS": 5}
TRANSCODES = {"vp9/vp9": 7}           # only vp9 has ever been involved in a transcode

BASE = dict(genre_affinity={}, watched_tmdb_ids=set(), collection_members={})


def _d(resolution, codec, *, platforms=PLATFORMS, transcodes=TRANSCODES):
    row = {"tmdb_id": 42, "resolution": resolution, "video_codec": codec}
    fr = build_movie_feature_row(row, credits={})
    _, bd = score_movie_features(
        fr, **BASE, platform_usage=platforms, transcode_stats=transcodes,
        return_breakdown=True,
    )
    return (bd["D1_device_capability"], bd["D2_transcode_avoidance"],
            bd["D3_platform_ceiling"])


# ── the marshalling itself ───────────────────────────────────────────────────

def test_feature_row_carries_resolution_and_codec():
    fr = build_movie_feature_row(
        {"tmdb_id": 1, "resolution": 1080.0, "video_codec": "x264"}, credits={})
    assert fr.resolution == 1080          # float column coerced to int
    assert fr.video_codec == "x264"


def test_feature_row_tolerates_missing_columns():
    fr = build_movie_feature_row({"tmdb_id": 1}, credits={})
    assert fr.resolution is None and fr.video_codec is None


# ── D1 — primary device capability ───────────────────────────────────────────

def test_d1_full_credit_only_at_the_primary_device_ceiling():
    # Primary is Windows (2160 ceiling): an exact match earns the full +6, anything
    # the device can play but does not need its full ceiling for earns +3.
    assert _d(2160, "h264")[0] == 6.0
    assert _d(1080, "h264")[0] == 3.0
    assert _d(720, "h264")[0] == 3.0


def test_d1_penalises_content_above_the_primary_ceiling():
    # Primary is a 1080p handheld → a 4K file must be downscaled.
    assert _d(2160, "h264", platforms={"iPhone": 90, "Windows": 1})[0] == -2.0


def test_d1_and_d3_are_zero_without_a_resolution():
    d1, _, d3 = _d(None, "h264")
    assert (d1, d3) == (0.0, 0.0)


def test_d1_and_d3_are_zero_without_household_platform_data():
    d1, _, d3 = _d(2160, "h264", platforms=None)
    assert (d1, d3) == (0.0, 0.0)


# ── D2 — transcode avoidance ─────────────────────────────────────────────────

def test_d2_rewards_a_codec_the_household_has_never_transcoded():
    assert _d(1080, "h264")[1] == 5.0


def test_d2_demotes_a_codec_that_has_caused_transcodes():
    # h264 appears in the transcode matrix, but it direct-plays on typical devices
    # (_TRANSCODE_FRIENDLY_CODECS) → partial credit rather than none.
    assert _d(1080, "h264", transcodes={"h264/h264": 3})[1] == 2.0
    # vp9 has transcoded AND is not on the friendly list → no credit at all.
    assert _d(1080, "vp9")[1] == 0.0


def test_d2_stays_neutral_when_the_codec_is_unknown():
    assert _d(1080, None)[1] == 2.0
    assert _d(1080, "")[1] == 2.0


def test_d2_no_longer_reads_the_absent_top_level_movie_key():
    # The pre-fix path: the codec is not a top-level Radarr movie field, so a row
    # without the video_codec column can only ever reach the neutral branch. This is
    # the exact shape that pinned every movie in the library at +2.0.
    fr = build_movie_feature_row({"tmdb_id": 1, "resolution": 1080}, credits={})
    _, bd = score_movie_features(fr, **BASE, platform_usage=PLATFORMS,
                                 transcode_stats=TRANSCODES, return_breakdown=True)
    assert bd["D2_transcode_avoidance"] == 2.0


# ── D3 — whole-household platform ceiling ────────────────────────────────────

def test_d3_scales_with_the_share_of_plays_that_can_handle_the_resolution():
    # 1080p: every platform can play it → 100% capable → full +4.
    assert _d(1080, "h264")[2] == 4.0
    # 2160p: only Windows(60) + Tizen(20) of 100 plays → 80% → still the top rung.
    assert _d(2160, "h264")[2] == 4.0
    # A household that is mostly 1080p handhelds drops off the top rungs.
    mostly_handheld = {"iPhone": 40, "Android": 30, "Windows": 30}
    assert _d(2160, "h264", platforms=mostly_handheld)[2] == 1.0
    assert _d(1080, "h264", platforms=mostly_handheld)[2] == 4.0


def test_d3_ignores_unrecognised_platforms():
    # An unmapped platform contributes plays to the denominator but never to the
    # capable count — which is exactly why Tizen/iOS had to be added to the table.
    assert _d(1080, "h264", platforms={"Windows": 50, "Nonesuch OS": 50})[2] == 2.0


# ── the device table gap this fix also closed ────────────────────────────────

def test_tizen_and_ios_resolve_in_the_device_ceiling_table():
    """Tautulli names Samsung TVs "Tizen" and Apple handhelds "iOS"; neither string
    fuzzy-matched any pre-existing key, so 42% of this household's plays counted as
    unknown devices and D3 could never clear its 75% rung."""
    def _ceiling(platform):
        p = platform.lower().strip()
        return next((c for k, c in _DEVICE_RESOLUTION_CEILING.items()
                     if k in p or p in k), None)

    assert _ceiling("Tizen") == 2160
    assert _ceiling("iOS") == 1080
    # the additions must not shadow the platforms that already resolved
    assert _ceiling("Windows") == 2160
    assert _ceiling("Android") == 1080
    assert _ceiling("Chrome") == 1080
    assert _ceiling("PlayStation") == 2160

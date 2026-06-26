"""Pure tests for quality_analytics.legacy_codec: codec classification, replacement selection, and
extracting owned legacy files from an episode-files dataframe."""
from __future__ import annotations

import pandas as pd

from scripts.managers.machine_learning.quality_analytics.legacy_codec import (
    best_modern_release,
    is_legacy_codec,
    legacy_files_from_df,
)


def _rel(title, res, score=0, rejected=False):
    return {"title": title, "quality": {"quality": {"resolution": res}},
            "customFormatScore": score, "rejected": rejected, "guid": title, "indexerId": 1}


def test_is_legacy_codec_classifies_old_codecs():
    for c in ["XviD", "xvid", "DivX", "MPEG2", "mpeg2video", "WMV", "msmpeg4v3", "mpeg4", "VC-1"]:
        assert is_legacy_codec(c), c
    for c in ["x264", "h264", "AVC", "x265", "hevc", "av1", "H.264", "HEVC", "", None]:
        assert not is_legacy_codec(c), c


def test_best_modern_prefers_same_tier_then_score():
    rels = [_rel("Show.S01E01.DVDRip.XviD-G", 480, 50),            # legacy → skip
            _rel("Show.S01E01.1080p.BluRay.x265-G", 1080, 900),    # modern but a resolution upgrade
            _rel("Show.S01E01.DVDRip.x264-A", 480, 100),           # modern, same tier
            _rel("Show.S01E01.DVDRip.x264-B", 480, 300),           # modern, same tier, better score
            _rel("Show.S01E01.DVDRip.x264-R", 480, 999, rejected=True)]   # rejected → skip
    assert best_modern_release(rels, 480)["title"] == "Show.S01E01.DVDRip.x264-B"


def test_best_modern_allows_upgrade_when_no_same_tier():
    rels = [_rel("S.S01E01.XviD", 480), _rel("S.S01E01.720p.x265", 720, 100),
            _rel("S.S01E01.1080p.x264", 1080, 100)]
    assert "720p" in best_modern_release(rels, 480)["title"]


def test_best_modern_none_paths():
    assert best_modern_release([_rel("X.S01E01.XviD-G", 480)], 480) is None    # only legacy
    assert best_modern_release([_rel("X.S01E01.x264-G", 480)], 720) is None    # modern but below current res
    assert best_modern_release([], 480) is None


def test_legacy_files_from_df_drops_modern_and_sorts_watched_first():
    df = pd.DataFrame([
        {"series_id": 1, "episode_file_id": 11, "series_title": "A", "video_codec": "XviD",
         "resolution": 480, "season_number": 1, "episode_number": 1, "watch_count": 0},
        {"series_id": 2, "episode_file_id": 22, "series_title": "B", "video_codec": "x264",
         "resolution": 1080, "season_number": 1, "episode_number": 1, "watch_count": 5},
        {"series_id": 3, "episode_file_id": 33, "series_title": "C", "video_codec": "MPEG2",
         "resolution": 480, "season_number": 2, "episode_number": 3, "watch_count": 9},
    ])
    out = legacy_files_from_df(df)
    assert [r["episode_file_id"] for r in out] == [33, 11]   # x264 dropped; watched(9) before unwatched(0)
    assert out[0]["series_title"] == "C" and out[0]["resolution"] == 480


def test_legacy_files_from_df_missing_columns():
    assert legacy_files_from_df(pd.DataFrame([{"foo": 1}])) == []
    assert legacy_files_from_df(None) == []

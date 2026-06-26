"""Pure-logic tests for regrab_legacy_codecs: legacy-codec classification + which release we'd swap to."""
from __future__ import annotations

from scripts.support.tools.regrab_legacy_codecs import best_modern_release, _is_legacy


def _rel(title, res, score=0, rejected=False):
    return {"title": title, "quality": {"quality": {"resolution": res}},
            "customFormatScore": score, "rejected": rejected, "guid": title, "indexerId": 1}


def test_is_legacy_classifies_old_codecs():
    for c in ["XviD", "xvid", "DivX", "MPEG2", "mpeg2video", "WMV", "msmpeg4v3", "mpeg4", "VC-1"]:
        assert _is_legacy(c), c
    for c in ["x264", "h264", "AVC", "x265", "hevc", "av1", "H.264", "HEVC"]:
        assert not _is_legacy(c), c


def test_best_modern_prefers_same_tier_then_score():
    cur = 480
    rels = [
        _rel("Show.S01E01.DVDRip.XviD-GRP", 480, 50),               # legacy → skip
        _rel("Show.S01E01.1080p.BluRay.x265-GRP", 1080, 900),       # modern but a resolution upgrade
        _rel("Show.S01E01.DVDRip.x264-A", 480, 100),                # modern, same tier
        _rel("Show.S01E01.DVDRip.x264-B", 480, 300),                # modern, same tier, better CF score
        _rel("Show.S01E01.DVDRip.x264-R", 480, 999, rejected=True),  # rejected → skip
    ]
    best = best_modern_release(rels, cur)
    assert best is not None and best["title"] == "Show.S01E01.DVDRip.x264-B"   # same tier (480), top score


def test_best_modern_allows_upgrade_when_no_same_tier():
    # No same-tier modern release → take the smallest-res modern release that's >= current.
    rels = [_rel("S.S01E01.XviD", 480, 10), _rel("S.S01E01.720p.x265", 720, 100),
            _rel("S.S01E01.1080p.x264", 1080, 100)]
    best = best_modern_release(rels, 480)
    assert best is not None and "720p" in best["title"]


def test_best_modern_none_when_only_legacy_or_lower_res():
    assert best_modern_release([_rel("X.S01E01.XviD-G", 480)], 480) is None      # only legacy
    assert best_modern_release([_rel("X.S01E01.x264-G", 480)], 720) is None      # modern but below current res
    assert best_modern_release([], 480) is None

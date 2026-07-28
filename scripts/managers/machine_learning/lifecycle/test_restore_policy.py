"""Tests for lifecycle.restore_policy — the deleted-episode ledger's release
identity and the targeted-restore match.

The two invariants that matter: the ledger round-trips BOTH schema versions (a v1
entry recorded before releases existed must keep working forever), and the match
is TOTAL-fallback (anything unmatched returns None, which the caller reads as
"blind search" — a stale scene_name must never block a restore).
"""
from __future__ import annotations

from scripts.managers.machine_learning.lifecycle.restore_policy import (
    LEDGER_SCHEMA_VERSION,
    RELEASE_FIELDS,
    episode_key,
    ledger_releases,
    match_release,
    merge_ledger_entry,
    release_record,
)


def _rel(title, group=None, quality=None, res=None, size=None, guid="g", indexer=1):
    return {"title": title, "releaseGroup": group, "guid": guid, "indexerId": indexer,
            "size": size, "quality": {"quality": {"name": quality, "resolution": res}}}


_RECORDED = {"scene_name": "Blue.Bloods.S02E15.1080p.WEB-DL.DD5.1.H.264-NTb",
             "release_group": "NTb", "quality_name": "WEBDL-1080p",
             "resolution": 1080, "size_bytes": 2_000_000_000}


# ── episode_key / release_record ────────────────────────────────────────────────
def test_episode_key():
    assert episode_key(2, 15) == "S02E15"
    assert episode_key("2", "15") == "S02E15"
    assert episode_key(None, 15) is None
    assert episode_key(2, "x") is None


def test_release_record_projects_only_the_identity_fields():
    row = {"scene_name": " Show.S01E01-GRP ", "release_group": "GRP",
           "quality_name": "HDTV-720p", "resolution": "720", "size_bytes": 1234.0,
           "series_title": "Show", "path": "/x"}
    rec = release_record(row)
    assert set(rec) <= set(RELEASE_FIELDS)
    assert rec == {"scene_name": "Show.S01E01-GRP", "release_group": "GRP",
                   "quality_name": "HDTV-720p", "resolution": 720, "size_bytes": 1234}


def test_release_record_is_none_when_nothing_is_known():
    assert release_record({}) is None
    assert release_record(None) is None
    assert release_record({"scene_name": None, "release_group": float("nan")}) is None
    # a PARTIAL record is still worth keeping — group + quality narrows a search
    assert release_record({"release_group": "GRP"}) == {"release_group": "GRP"}


# ── ledger schema round-trip ────────────────────────────────────────────────────
def test_v1_entry_round_trips_and_reports_no_releases():
    """The deployed shape: {episodes, ts} with no release info at all."""
    v1 = {"episodes": [[1, 1], [1, 2]], "ts": "2026-07-01T00:00:00+00:00"}
    assert ledger_releases(v1) == {}
    merged = merge_ledger_entry(v1, {"episodes": [[1, 2], [1, 3]], "ts": "2026-07-27T00:00:00+00:00"})
    assert merged["episodes"] == [[1, 1], [1, 2], [1, 3]]        # deduped union
    assert merged["ts"] == "2026-07-27T00:00:00+00:00"           # newest deletion wins
    assert "releases" not in merged and "v" not in merged        # stays v1-shaped


def test_v1_entry_upgrades_in_place_when_a_release_is_recorded():
    v1 = {"episodes": [[1, 1]], "ts": "2026-07-01T00:00:00+00:00"}
    inc = {"episodes": [[1, 2]], "ts": "2026-07-27T00:00:00+00:00",
           "releases": {"S01E02": _RECORDED}}
    merged = merge_ledger_entry(v1, inc)
    assert merged["v"] == LEDGER_SCHEMA_VERSION
    assert merged["episodes"] == [[1, 1], [1, 2]]                # v1 coords preserved
    assert ledger_releases(merged)["S01E02"] == _RECORDED


def test_v2_entry_round_trips_and_a_re_delete_overwrites_the_release():
    v2 = {"episodes": [[1, 1]], "ts": "a", "v": 2,
          "releases": {"S01E01": {"release_group": "OLD"}}}
    merged = merge_ledger_entry(v2, {"episodes": [[1, 1]], "ts": "b",
                                     "releases": {"S01E01": {"release_group": "NEW"}}})
    assert merged["episodes"] == [[1, 1]]                        # no duplicate coords
    assert ledger_releases(merged) == {"S01E01": {"release_group": "NEW"}}


def test_merge_tolerates_a_missing_or_garbled_side():
    assert merge_ledger_entry(None, {"episodes": [[1, 1]], "ts": "t"})["episodes"] == [[1, 1]]
    assert merge_ledger_entry("junk", {"episodes": [[1, 1]], "ts": "t"})["episodes"] == [[1, 1]]
    assert merge_ledger_entry({"episodes": [[1, 1]], "ts": "t"}, None)["episodes"] == [[1, 1]]
    assert ledger_releases("junk") == {} and ledger_releases(None) == {}
    assert ledger_releases({"releases": "junk"}) == {}


# ── the targeted match ──────────────────────────────────────────────────────────
def test_exact_scene_name_wins_even_against_a_bigger_sibling():
    releases = [
        _rel("Blue.Bloods.S02E15.1080p.WEB-DL.DD5.1.H.264-NTb", "NTb", "WEBDL-1080p", 1080, 2_000_000_000),
        _rel("Blue.Bloods.S02E15.2160p.WEB-DL-OTHER", "OTHER", "WEBDL-2160p", 2160, 9_000_000_000),
    ]
    assert match_release(releases, _RECORDED)["releaseGroup"] == "NTb"


def test_punctuation_and_case_churn_still_matches():
    got = match_release([_rel("blue bloods s02e15 1080p web dl dd5 1 h 264 ntb")], _RECORDED)
    assert got is not None


def test_group_plus_quality_clears_the_bar_but_group_alone_does_not():
    recorded = {"release_group": "NTb", "quality_name": "WEBDL-1080p", "resolution": 1080}
    assert match_release([_rel("Something.Else", "NTb", "WEBDL-1080p", 1080)], recorded) is not None
    # group only -> +1, below min_confidence 2 -> no match -> blind search
    assert match_release([_rel("Something.Else", "NTb", "HDTV-720p", 720)],
                         {"release_group": "NTb"}) is None


def test_closest_size_breaks_a_tie_between_equal_confidence_releases():
    recorded = {"release_group": "GRP", "quality_name": "WEBDL-1080p", "resolution": 1080,
                "size_bytes": 2_000_000_000}
    releases = [_rel("A", "GRP", "WEBDL-1080p", 1080, 8_000_000_000, guid="big"),
                _rel("B", "GRP", "WEBDL-1080p", 1080, 2_100_000_000, guid="close")]
    assert match_release(releases, recorded)["guid"] == "close"


def test_no_match_returns_none_so_the_caller_blind_searches():
    assert match_release([_rel("Totally.Different-XYZ", "XYZ", "HDTV-720p", 720)], _RECORDED) is None
    assert match_release([], _RECORDED) is None
    assert match_release(None, _RECORDED) is None
    assert match_release("junk", _RECORDED) is None
    assert match_release([{"not": "a release"}, None, 7], _RECORDED) is None


def test_absent_scene_name_falls_back_cleanly_and_never_blocks():
    """The 80 % case: scene_name is populated on only ~1 row in 5. With nothing
    recorded at all the match MUST return None (blind search), not raise."""
    assert match_release([_rel("Anything")], None) is None
    assert match_release([_rel("Anything")], {}) is None
    # ...and a record with only a size is not enough to claim a match
    assert match_release([_rel("Anything", size=2_000_000_000)], {"size_bytes": 2_000_000_000}) is None

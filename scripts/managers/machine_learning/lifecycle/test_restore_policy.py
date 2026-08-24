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
    looks_like_secret,
    match_release,
    merge_ledger_entry,
    push_payload,
    redact_grab_url,
    refill_grab_url,
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


# ── grab-url redaction (GLD-RST-07) ──────────────────────────────────────────
# The archive these feed is APPEND-ONLY, so a leak here is a credential on disk
# forever. Every test below asserts on the DECODED form as well as the raw string:
# the previous implementation's two worst leaks read clean to a naive substring
# check because the passkey was base64-encoded inside Prowlarr's `link=`.
import base64
from urllib.parse import unquote

_PASSKEY = "9f3c1e7a2b8d4f60a1c5e9b3d7f2a4c6"
_APIKEY = "aaaabbbbccccddddeeeeffff00001111"
_INNER_PRIVATE = (f"https://tracker.example.org/download.php/12345/{_PASSKEY}"
                  f"/Show.S01E01.1080p.WEB-DL-NTb.torrent")
_INNER_PUBLIC = "https://public.example.org/download/Show.S01E01.1080p.WEB-DL-NTb.torrent"


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _contains_anywhere(haystack, needle):
    """Whether *needle* survives in *haystack* in ANY encoding — raw, percent-decoded,
    or base64 inside a param. A secret that is merely encoded is still on disk."""
    import re as _re
    text = str(haystack or "")
    pool = [text, unquote(text)]
    for blob in _re.findall(r"[A-Za-z0-9+/=_-]{16,}", unquote(text)):
        for fn in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                pool.append(fn(blob + "=" * (-len(blob) % 4)).decode("utf-8", "ignore"))
            except Exception:
                pass
    return any(needle in p for p in pool)


def test_malformed_query_strings_cannot_smuggle_a_key_through_the_path():
    """REGRESSION. Real indexers emit URLs whose params are appended with ``&`` and no
    ``?``::

        https://drunkenslug.com/getnzb/<id>.nzb&i=139402&r=<apikey>

    ``urlparse`` files that entire tail under PATH and leaves ``query`` empty, so the
    query allowlist never sees it AND the path-segment test rejects the segment for
    holding ``.`` and ``&`` (charset check fails). It passed through verbatim and
    shipped a live Newznab apikey into 266 rows of an append-only archive.

    Missed because every URL in the original corpus was WELL-FORMED — the adversarial
    cases covered base64-nested passkeys, path tokens and netloc userinfo, but never
    a URL that simply does not parse. Found by reading real output, not by review.
    """
    key, uid = "b069b52f8352a576e9c26554875d77a9", "139402"
    out = redact_grab_url(
        f"https://drunkenslug.com/getnzb/2aea283c6554536af3f293ca1062310b62638d6d.nzb"
        f"&i={uid}&r={key}")
    assert out
    assert not _contains_anywhere(out, key)
    assert not _contains_anywhere(out, uid)
    # the non-secret part must survive, or the row loses its forensic value
    assert "drunkenslug.com" in out and "getnzb" in out


def test_newznab_r_and_i_are_not_refillable():
    """``r`` is the Newznab apikey and ``i`` the user id. Neither is the *arr-side
    indexer key this system holds, so a descriptor bearing them must be DROPPED rather
    than pushed with the wrong secret substituted in."""
    out = redact_grab_url("https://x.io/getnzb/abc.nzb&i=1&r=SECRETKEY123456")
    assert refill_grab_url(out, "SOME-OTHER-KEY") is None


def test_a_well_formed_url_is_unaffected_by_the_malformed_path():
    """The ``&``-in-path split must not fire on URLs that already have a query."""
    u = "https://api.nzbgeek.info/api?t=get&id=abc123&apikey=AAAA1111BBBB2222"
    assert refill_grab_url(redact_grab_url(u), "AAAA1111BBBB2222") == u


def test_all_four_secret_channels_are_scrubbed():
    """Query, nested-URL param, path segment and netloc userinfo. The original
    implementation scrubbed only the first and leaked 6 of 9 real grab shapes."""
    cases = [
        f"https://gazelle.example.org/torrents.php?id=99&authkey={_APIKEY}&torrent_pass={_PASSKEY}",
        f"http://prowlarr:9696/1/download?apikey={_APIKEY}&link={_b64(_INNER_PRIVATE)}",
        f"https://indexer.example.net/rss/{_PASSKEY}/t/5/dl.torrent",
        f"https://robert:{_PASSKEY}@usenet.example.com/nzb/12345",
    ]
    for url in cases:
        out = redact_grab_url(url)
        assert out, url
        assert not _contains_anywhere(out, _PASSKEY), url
        assert not _contains_anywhere(out, _APIKEY), url


def test_unknown_query_params_are_redacted_rather_than_passed_through():
    """The inverted fail direction. A denylist leaks every param nobody thought of;
    on a file that never rotates that is permanent, so unknown must mean redact."""
    out = redact_grab_url(f"https://x.io/api?t=get&id=abc&some_new_key={_APIKEY}")
    assert "t=get" in out and "id=abc" in out         # known-inert params survive
    assert not _contains_anywhere(out, _APIKEY)


def test_a_public_nested_link_survives_so_the_push_still_works():
    """Blanket-blanking `link=` would cost a push on every public/usenet grab. Only
    a nested URL that actually carried a secret is dropped."""
    out = redact_grab_url(f"http://prowlarr:9696/1/download?apikey={_APIKEY}&link={_b64(_INNER_PUBLIC)}")
    assert _contains_anywhere(out, "public.example.org")
    assert refill_grab_url(out, _APIKEY) is not None


def test_release_names_are_never_mistaken_for_credentials():
    """A false positive costs a push; it must not fire on ordinary release names,
    including the underscore style that carries no dots."""
    for benign in ("Show_S01E01_1080p_WEBDL", "Show.S01E01.1080p.WEB-DL-NTb",
                   "The_Mandalorian_S03E05_2160p_HDR", "Some_Movie_2019_BluRay_REMUX",
                   "123456789012345678901234", "downloads"):
        assert not looks_like_secret(benign), benign
    for secret in (_PASSKEY, _APIKEY, "A1b2C3d4E5f6G7h8I9j0K1l2",
                   "abcdefghijklmnopqrstuvwxyzabcdef"):
        assert looks_like_secret(secret), secret


def test_a_refillable_url_round_trips_byte_exact():
    """Redaction must be lossless for everything except the secret itself — a push
    built from the archive has to be the URL that was originally grabbed."""
    for url in (f"https://api.nzbgeek.info/api?t=get&id=abc123&apikey={_APIKEY}",
                "https://public.example.org/download/Show.S01E01.1080p.WEB-DL-NTb.torrent"):
        assert refill_grab_url(redact_grab_url(url), _APIKEY) == url


def test_v1_entries_already_on_disk_still_refill():
    """The ledger predates named placeholders. Those entries must keep working
    forever — the migration story is that v1 simply never gains the new names."""
    assert refill_grab_url("https://x.io/api?apikey=%3Credacted%3E", "KEY").endswith("apikey=KEY")
    assert refill_grab_url("https://x.io/api?apikey=<redacted>", "KEY").endswith("apikey=KEY")
    assert refill_grab_url("https://x.io/api?apikey=<redacted>", None) is None


def test_an_unrefillable_url_drops_the_descriptor_and_magnet_still_carries_it():
    """A passkey was never stored and never can be put back. Pushing the half-dead
    URL would surface as a bad indexer rather than the missing credential it is —
    so the descriptor is dropped, and a torrent still restores via its infohash."""
    scrubbed = redact_grab_url(
        f"http://prowlarr:9696/1/download?apikey={_APIKEY}&link={_b64(_INNER_PRIVATE)}")
    assert refill_grab_url(scrubbed, _APIKEY) is None
    assert push_payload({"title": "S", "download_url": scrubbed}, indexer_api_key=_APIKEY) is None
    withhash = push_payload({"title": "S", "download_url": scrubbed, "info_hash": "a1" * 20},
                            indexer_api_key=_APIKEY)
    assert withhash and withhash["magnetUrl"].startswith("magnet:?xt=urn:btih:")
    assert "downloadUrl" not in withhash


def test_redaction_never_raises_and_refuses_what_it_cannot_parse():
    """A diagnostic that crashes the pass it documents is worse than no diagnostic;
    and a URL we cannot confidently scrub must be dropped, never half-scrubbed."""
    for hostile in (None, "", "   ", 0, [], {}, b"bytes", "http://", "://x",
                    "not a url", "javascript:alert(1)", "magnet:?xt=urn:btih:" + "A" * 40,
                    "https://h/" + "A" * 5000, "https://h/?link=" + "%" * 100,
                    "https://h/?a=%E0%A4%A", "https://h/%zz/p"):
        out = redact_grab_url(hostile)
        assert out is None or isinstance(out, str)

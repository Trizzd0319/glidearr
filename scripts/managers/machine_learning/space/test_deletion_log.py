"""Tests for space.deletion_log — the permanent record of what was destroyed.

Three invariants carry the whole design:

* **Absent stays absent.** The six delete paths know different things, so a field
  the caller could not supply must be MISSING, not defaulted. Zero is a real value
  and must survive (P-C).
* **The archive is exact.** It is written once and never rewritten, so a record
  must round-trip byte-for-byte through JSONL — including the numpy scalars the
  parquet delete paths hand it.
* **It never raises.** A diagnostic that can abort the pass it documents is worse
  than no diagnostic, and this one runs while files are being deleted.
"""
from __future__ import annotations

from scripts.managers.machine_learning.space.deletion_log import (
    DISPOSITIONS,
    deletion_record,
    library_class,
    new_run_id,
    parse_jsonl,
    render,
    seed_records,
    to_jsonl,
)

_RUN = "20260823T042426Z"


def _rec(**kw):
    base = dict(run_id=_RUN, media="episode", instance="sonarr-720", title="Show")
    base.update(kw)
    return deletion_record(**base)


# ── run id ──────────────────────────────────────────────────────────────────────
def test_run_id_is_stable_sortable_and_suffix_free():
    """It must NOT be the rotation suffix: -N shifts every run, so a deletion row
    pointing at default-2.log is wrong within 24 hours."""
    from datetime import datetime, timezone
    rid = new_run_id(datetime(2026, 8, 23, 4, 24, 26, tzinfo=timezone.utc))
    assert rid == "20260823T042426Z"
    assert new_run_id() <= new_run_id()          # monotonic, so sorts chronologically


# ── library class ───────────────────────────────────────────────────────────────
def test_library_class_reads_the_segment_after_the_media_kind():
    assert library_class("/data/media/tv/documentaries/Show/S01E01.mkv") == "documentaries"
    assert library_class("/mnt/user/data/media/movies/kids/Coco (2017)/x.mkv") == "kids"


def test_library_class_returns_none_rather_than_guessing():
    """An unknown class is honest; a wrong one contaminates every later query."""
    for junk in (None, "", "/data/media/tv", "C:/somewhere/else", 7, [], {}):
        assert library_class(junk) is None


# ── record shape ────────────────────────────────────────────────────────────────
def test_absent_fields_are_omitted_but_zero_is_kept():
    """The P-C rule. `score=0` is a real score and `size_bytes=0` is a real size;
    conflating either with 'not supplied' would silently rewrite the evidence."""
    zero = _rec(score=0, size_bytes=0)
    assert zero["score"] == 0 and zero["size_bytes"] == 0
    sparse = _rec()
    for absent in ("score", "size_bytes", "reason", "pid", "path", "class", "release", "push"):
        assert absent not in sparse, absent


def test_class_is_derived_but_the_raw_path_is_kept_beside_it():
    """The class is only as true as the path it came from, so the path must survive
    for a stale derivation to be recomputable rather than silently believed."""
    r = _rec(path="/data/media/tv/anime/Show/S01E01.mkv")
    assert r["class"] == "anime"
    assert r["path"].endswith("S01E01.mkv")


def test_every_record_carries_the_five_fields_that_make_it_traceable():
    r = _rec()
    for required in ("v", "run_id", "deleted_at", "media", "instance", "title", "disposition"):
        assert required in required and r.get(required) is not None


# ── JSONL fidelity ──────────────────────────────────────────────────────────────
def test_numpy_scalars_survive_as_numbers_not_strings():
    """The delete paths read parquet, so ids and scores arrive as np.int64 — which
    is NOT json-serialisable and was being rendered as a STRING by default=str,
    while np.float64 (a real float subclass) came through as a number. A silently
    and INCONSISTENTLY mistyped archive breaks any later `score < 20` query."""
    np = __import__("numpy")
    r = _rec(series_id=np.int64(17209), score=np.int64(50),
             size_bytes=np.float64(2.5e9), resolution=np.int64(1080))
    assert isinstance(r["series_id"], int) and not isinstance(r["series_id"], bool)
    assert isinstance(r["score"], int)
    assert r["series_id"] == 17209 and r["score"] == 50
    assert parse_jsonl(to_jsonl([r])) == [r]


def test_round_trip_is_exact():
    rows = [_rec(season=1, episode=2, size_bytes=123, reason="grace expired"),
            _rec(media="movie", title="Inception", year=2010, tmdb_id=27205)]
    assert parse_jsonl(to_jsonl(rows)) == rows


def test_a_torn_final_line_does_not_cost_the_whole_archive():
    """An append-only file killed mid-write ends in a partial object. Refusing to
    read 40,000 good rows because of one bad tail would make the archive useless
    exactly when it is being consulted."""
    good = to_jsonl([_rec(), _rec(title="Second")])
    assert len(parse_jsonl(good + ['{"partial": ', "", "   ", "not json"])) == 2


def test_one_unserialisable_row_does_not_drop_the_batch():
    class Weird:
        pass
    rows = [_rec(title="Fine"), _rec(extra_obj=Weird())]
    assert len(to_jsonl(rows)) == 2          # default=str carries the odd one


# ── seeding ─────────────────────────────────────────────────────────────────────
def test_seeding_reads_both_ledger_versions():
    """v1 (no releases map) and v2 share one entry shape by construction, which is
    why one seeder reads deleted_episodes AND stepdown_releases."""
    ledger = {
        "17267": {"episodes": [[1, 2]], "ts": "2026-08-07T16:43:29+00:00",
                  "releases": {"S01E02": {"release_group": "PFa", "quality_name": "Bluray-720p",
                                          "resolution": 720, "size_bytes": 2802404531}}, "v": 2},
        "999": {"episodes": [[3, 4]], "ts": "2026-08-01T00:00:00+00:00"},      # v1
    }
    rows = seed_records(ledger, run_id=_RUN, instance="sonarr-720",
                        titles={"17267": "The Jetsons"})
    assert len(rows) == 2
    jetsons = next(r for r in rows if r["season"] == 1)
    assert jetsons["title"] == "The Jetsons"          # id resolved to a name
    assert jetsons["release"]["release_group"] == "PFa"
    assert jetsons["deleted_at"].startswith("2026-08-07")
    v1 = next(r for r in rows if r["season"] == 3)
    assert v1["title"] == "series:999"                # unresolved id still recorded
    assert "release" not in v1


def test_seeded_rows_never_invent_a_reason():
    """The ledger never recorded one. A plausible guess in a file whose whole value
    is that it does not guess would be worse than the gap."""
    rows = seed_records({"1": {"episodes": [[1, 1]], "ts": "2026-01-01T00:00:00+00:00"}},
                        run_id=_RUN, instance="i")
    assert rows and "reason" not in rows[0] and rows[0]["source"] == "seed"


# ── render ──────────────────────────────────────────────────────────────────────
def test_render_groups_by_disposition_and_only_counts_real_bytes():
    """`marked-not-consented` rows describe files that are still on disk, so they
    must not be added to the reclaimed total."""
    rows = [_rec(disposition="deleted", size_bytes=2 * 1024 ** 3),
            _rec(disposition="marked-not-consented", size_bytes=8 * 1024 ** 3)]
    out = "\n".join(render(rows, run_id=_RUN))
    assert "2.00 GB" in out.split("--")[0]        # header counts only the deleted row
    assert "marked-not-consented (1" in out
    assert "deleted (1" in out


def test_render_filters_to_one_run():
    rows = [_rec(), deletion_record(run_id="OTHER", media="movie",
                                    instance="radarr-720", title="Elsewhere")]
    assert "Elsewhere" not in "\n".join(render(rows, run_id=_RUN))


def test_render_flags_the_strongest_restore_handle_available():
    """magnet > url > identity — the operator needs to see at a glance which rows
    can actually be re-acquired and which only describe what was lost."""
    magnet = _rec(push={"info_hash": "a" * 40})
    url = _rec(push={"download_url": "https://x.io/a?t=get"})
    ident = _rec(release={"release_group": "NTb"})
    assert "[magnet]" in "\n".join(render([magnet]))
    assert "[url]" in "\n".join(render([url]))
    assert "[identity]" in "\n".join(render([ident]))


# ── never raises ────────────────────────────────────────────────────────────────
def test_nothing_in_this_module_raises_on_hostile_input():
    """Swept as a CLASS rather than one shape at a time — the pattern this register
    criticises elsewhere. It is cheap only because the module is pure."""
    hostile = [None, {}, [], "", 0, "junk", {"a": None}, {"a": {}},
               {"a": {"episodes": None}}, {"a": {"episodes": "xx"}},
               {"a": {"episodes": [[1]]}}, {"a": {"episodes": [["x", "y"]]}},
               {"a": {"episodes": [[1, 2]], "releases": "notadict"}},
               {"a": {"episodes": [[1, 2]], "releases": {"S01E02": "notadict"}}},
               {"a": {"episodes": [[1, 2]], "ts": 12345}},
               {"a": {"episodes": [[None, None]]}}, {None: {"episodes": [[1, 2]]}}]
    for h in hostile:
        rows = seed_records(h, run_id=_RUN, instance="i")
        assert isinstance(rows, list)
        assert isinstance(to_jsonl(rows), list)
        assert isinstance(render(rows), list)

    for h in (None, [], "", 0, ["junk"], [None], ['{"a":1'], [b"x"], ["[1,2,3]"], ["null"]):
        assert isinstance(parse_jsonl(h), list)

    for h in (None, [], [None], ["junk"], [{}], [{"disposition": "weird"}],
              [{"size_bytes": "NaN"}], [{"season": "x", "episode": "y"}],
              [{"push": "notadict"}], [{"release": 7}], [{"title": None}]):
        assert isinstance(render(h), list)


def test_dispositions_cover_the_states_a_pass_can_end_in():
    """`marked-not-consented` exists because that is exactly the state the
    2026-08-23 run was in — 266 rows queued, consent withheld — and no artifact
    anywhere recorded WHICH 266."""
    assert "marked-not-consented" in DISPOSITIONS
    assert set(DISPOSITIONS) >= {"deleted", "would-delete", "failed"}

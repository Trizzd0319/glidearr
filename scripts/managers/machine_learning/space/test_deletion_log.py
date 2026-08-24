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
    churn_key,
    deletion_record,
    detect_churn,
    drift_after_rebuild,
    drift_tolerances,
    intersection_drift,
    merge_upgrade_intents,
    reconcile_upgrades,
    upgrade_intent,
    upgrade_events,
    upgrade_key,
    coverage,
    library_class,
    new_run_id,
    parse_jsonl,
    render,
    seed_records,
    space_ledger,
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


# ── churn + space accounting (GLD-DEL-06) ──────────────────────────────────
def _ev(disp, day, tmdb=1, size=0, new=None, title="T"):
    return deletion_record(
        run_id=_RUN, media="movie", instance="radarr-720", title=title,
        disposition=disp, tmdb_id=tmdb, size_bytes=size,
        deleted_at=f"2026-08-{day:02d}T00:00:00+00:00",
        replaced_by=({"size_bytes": new} if new is not None else None))


def test_a_flip_is_a_direction_CHANGE_not_an_event():
    """Three consecutive step-downs are one descent, not churn. Counting events
    instead would flag every ordinary multi-stage reclaim and the signal would be
    ignored inside a week."""
    descent = [_ev("stepped-down", d) for d in (1, 2, 3)]
    assert detect_churn(descent, min_flips=1, window_days=None) == []

    thrash = [_ev("upgraded", 1), _ev("stepped-down", 2), _ev("upgraded", 3)]
    hits = detect_churn(thrash, min_flips=2, window_days=None)
    assert len(hits) == 1 and hits[0]["flips"] == 2 and hits[0]["events"] == 3


def test_churn_is_keyed_on_the_external_id_not_the_title():
    """A title string changes with a metadata refresh; keying on it would split one
    asset's history in two and hide the thrash."""
    evs = [_ev("upgraded", 1, tmdb=27205, title="Inception"),
           _ev("stepped-down", 2, tmdb=27205, title="Inception (2010)"),
           _ev("upgraded", 3, tmdb=27205, title="Inception")]
    hits = detect_churn(evs, min_flips=2, window_days=None)
    assert len(hits) == 1 and hits[0]["key"] == "tmdb_id:27205"
    assert churn_key({"title": "No ids"}) == "title:No ids"
    assert churn_key(None) is None


def test_churn_respects_the_window_and_sorts_worst_first():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    old = [_ev("upgraded", 1, tmdb=9), _ev("stepped-down", 2, tmdb=9),
           _ev("upgraded", 3, tmdb=9)]
    assert detect_churn(old, window_days=3, min_flips=1, now=now) == []

    mild = [_ev("upgraded", 29, tmdb=1), _ev("stepped-down", 30, tmdb=1)]
    bad = [_ev("upgraded", 29, tmdb=2), _ev("stepped-down", 30, tmdb=2),
           _ev("upgraded", 30, tmdb=2), _ev("stepped-down", 31, tmdb=2)]
    hits = detect_churn(mild + bad, window_days=30, min_flips=1, now=now)
    assert [h["key"] for h in hits] == ["tmdb_id:2", "tmdb_id:1"]


def test_deletes_and_marks_are_not_churn():
    """Only quality TRANSITIONS have a direction. A delete is terminal and a
    `marked-not-consented` row is not an event at all."""
    noise = [_ev("deleted", 1), _ev("marked-not-consented", 2), _ev("failed", 3)]
    assert detect_churn(noise, min_flips=1, window_days=None) == []


def test_space_ledger_counts_the_spend_not_only_the_reclaim():
    """The system logged `freed_now_gb` and never the consumption that CAUSES the
    pressure. A step-down that frees 29 GB and grabs back 4 GB nets 25 GB."""
    gb = 1024 ** 3
    led = space_ledger([
        _ev("stepped-down", 1, size=29 * gb, new=4 * gb),
        _ev("deleted", 2, size=2 * gb),
        _ev("upgraded", 3, size=2 * gb, new=8 * gb),      # projected +6
    ])
    assert led["reclaimed_bytes"] == 31 * gb
    assert led["spent_bytes"] == 10 * gb                   # 4 regrab + 6 projected
    assert led["net_bytes"] == 21 * gb
    assert led["by_disposition"]["stepped-down"]["n"] == 1


def test_churn_and_ledger_never_raise_on_hostile_input():
    for h in (None, [], [None], ["junk"], [{}], [{"disposition": "upgraded"}],
              [{"disposition": "upgraded", "tmdb_id": 1, "deleted_at": None}],
              [{"disposition": "stepped-down", "size_bytes": "x"}],
              [{"disposition": "upgraded", "replaced_by": "notadict"}],
              [{"disposition": "stepped-down", "replaced_by": {"size_bytes": "x"}}]):
        assert isinstance(detect_churn(h, window_days=None), list)
        assert isinstance(space_ledger(h), dict)


# ── parquet drift (GLD-DEL-08) ─────────────────────────────────────────
TIB = 1024 ** 4
GiB = 1024 ** 3


def test_gates_scale_with_sqrt_n_across_library_sizes():
    """A flat gate breaks in OPPOSITE directions as the library grows: 5 files is
    4.2% of a tiny library but 0.03% of a large one, while 5% of size is 5 GiB on
    the former and 609 GiB on the latter. √n tightens the percentage as the library
    grows, which is the correct direction, and is never absurd at either end."""
    expect = {120: 11, 800: 29, 4_000: 64, 15_548: 125, 40_000: 200, 120_000: 347}
    for n, files in expect.items():
        t = drift_tolerances(n, n * 0.8 * (1024 ** 3))
        assert t["files"] == files, (n, t["files"], files)
    # monotonic in absolute terms, tightening in relative terms
    pcts = [drift_tolerances(n, n * GiB)["files"] / n for n in sorted(expect)]
    assert pcts == sorted(pcts, reverse=True)


def test_the_floor_holds_for_tiny_libraries():
    """Below ~25 files √n collapses and single-file events dominate."""
    assert drift_tolerances(1, GiB)["files"] == 5
    assert drift_tolerances(0, 0)["files"] == 5
    assert drift_tolerances(9, 9 * GiB)["files"] == 5          # √9 = 3 -> floor
    assert drift_tolerances(36, 36 * GiB)["files"] == 6        # √36 = 6 -> above floor


def test_the_size_gate_is_derived_from_the_count_gate_not_a_percentage():
    """Two independent gates measure different things and one always fires first —
    at 12.2 TiB the 5% gate was 609 GiB while the 5-file gate was 0.03%, so size was
    decorative. Derived, both express the same severity in different units."""
    t = drift_tolerances(15_548, 12.2 * TIB)
    assert t["files"] == 125
    assert 100 < t["bytes"] / GiB < 101                        # ~100.4 GiB, not 609
    assert abs(t["mean_file_bytes"] - (12.2 * TIB / 15_548)) < 1


def test_the_by_design_coverage_gap_is_NOT_a_breach():
    """The premise the first version was built on was FALSE. The parquet is a
    working set: 97.6% of series (13,065 of 13,382) hold exactly one row, because
    `_ingest_inventory_tv` gives an unwatched series only a PILOT row. Comparing
    totals would have breached nightly forever and triggered a rebuild that cannot
    close a gap that is design. The *arr having files the parquet does not is
    therefore silent — that is the whole correction."""
    parquet = {i: 1_000 for i in range(10_711)}
    arr = {i: 1_000 for i in range(15_548)}          # 4,837 the parquet never tracked
    d = intersection_drift(parquet, arr)
    assert not d["breach"] and d["orphaned"] == 0 and d["size_mismatch"] == 0
    assert d["checked"] == 10_711


def test_orphaned_rows_breach():
    """A file_id the parquet owns and the *arr does not: the row counts bytes that
    are already gone. This is the BBT S3 `files=6` shape — deleted outside glidearr,
    still owned."""
    parquet = {i: 1_000 for i in range(10_000)}
    arr = {i: 1_000 for i in range(10_000) if i >= 300}      # 300 vanished
    d = intersection_drift(parquet, arr)
    assert d["breach"] and d["orphaned"] == 300
    assert d["tolerance"] == 100                              # √10,000
    assert any("orphaned" in r for r in d["reasons"])


def test_the_id_list_is_a_WORK_list_and_is_never_truncated():
    """REGRESSION. These ids were capped at 50 for log readability, and the Sonarr
    caller then drove its repair off the capped list — so the live 2026-08-24 breach
    of 194 orphans re-synced only the 9 series covered by the first 50 ids, cleared
    28% (194->140), and reported PERSISTENT when a full repair would likely have
    closed it. Truncation belongs at the DISPLAY layer."""
    parquet = {i: 1_000 for i in range(10_000)}
    arr = {i: 1_000 for i in range(10_000) if i >= 300}
    d = intersection_drift(parquet, arr)
    assert len(d["orphaned_ids"]) == 300, "the repair list must be complete"
    assert d["orphaned_ids"] == sorted(d["orphaned_ids"])


def test_size_mismatches_breach():
    """Both sides hold the id and disagree: the file was replaced and the parquet
    kept the old figure, so every reclaim projection reading it is wrong."""
    parquet = {i: 1_000 for i in range(10_000)}
    arr = {i: (2_000 if i < 300 else 1_000) for i in range(10_000)}
    d = intersection_drift(parquet, arr)
    assert d["breach"] and d["size_mismatch"] == 300 and d["orphaned"] == 0


def test_ordinary_staleness_stays_under_the_gate():
    """A handful of rows going stale between syncs is normal; √n absorbs it."""
    parquet = {i: 1_000 for i in range(10_000)}
    arr = {i: (2_000 if i < 40 else 1_000) for i in range(10_000) if i >= 20}
    d = intersection_drift(parquet, arr)
    assert not d["breach"] and d["orphaned"] == 20 and d["size_mismatch"] == 20


def test_tolerance_scales_over_the_INTERSECTION_not_the_library():
    """The population being checked is what the parquet claims, not what the *arr
    holds — scaling over the library would hand a working set a gate sized for data
    it was never going to have."""
    small = intersection_drift({i: 1 for i in range(100)}, {i: 1 for i in range(50_000)})
    assert small["tolerance"] == 10                            # √100, not √50,000


def test_an_empty_arr_side_does_not_condemn_the_whole_parquet():
    """A failed fetch returns nothing. Calling every row orphaned on that would
    declare the entire cache invalid on one bad read."""
    d = intersection_drift({1: 100, 2: 200}, {})
    assert not d["breach"] and d["orphaned"] == 0
    assert any("unmeasurable" in r for r in d["reasons"])


def test_coverage_is_a_gauge_and_never_a_gate():
    """65% is the designed steady state. Kept separate from the detector so the two
    can never be confused again."""
    c = coverage(10_711, 15_548)
    assert 0.68 < c["ratio"] < 0.69
    assert "breach" not in c
    assert coverage(None, "x")["ratio"] is None


def test_intersection_drift_never_raises():
    for p, a in ((None, None), ({}, {}), ("x", "y"), ([], []), ({1: "x"}, {1: "y"}),
                 ({None: 1}, {1: 1}), ({1: None}, {1: None}), ({1: 0}, {1: 0})):
        d = intersection_drift(p, a)
        assert isinstance(d, dict) and isinstance(d["reasons"], list)


def test_the_second_measurement_is_what_carries_the_verdict():
    """A first breach cannot tell a stale cache from one that keeps going stale.
    `persistent` must NOT trigger another resync, or an expensive pass repeats every
    run to reach the same answer until the operator disables the detector."""
    bad = {"breach": True, "orphaned": 300, "size_mismatch": 0}
    clean = {"breach": False, "orphaned": 2, "size_mismatch": 0}
    assert drift_after_rebuild(bad, clean) == "closed"
    assert drift_after_rebuild(bad, bad) == "persistent"
    assert drift_after_rebuild(bad, {"breach": True, "orphaned": 100,
                                     "size_mismatch": 0}) == "improved"
    assert drift_after_rebuild(bad, {"breach": True, "orphaned": 900,
                                     "size_mismatch": 0}) == "worse"


def test_drift_helpers_never_raise():
    for a, b in ((None, None), ({}, {}), ("x", "y"), ({"breach": True}, None)):
        assert isinstance(drift_after_rebuild(a, b), str)


# ── upgrade intent + reconciliation (GLD-DEL-10) ─────────────────────────────
from datetime import datetime, timedelta, timezone

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _intent(fid, hours_ago=1, sid=17209, s=1, e=2):
    return upgrade_intent(series_id=sid, season=s, episode=e, file_id=fid,
                          size_bytes=2_000_000_000, quality_name="WEBDL-720p",
                          title="Ted Lasso",
                          at=(_NOW - timedelta(hours=hours_ago)).isoformat())


def test_the_intent_is_keyed_on_the_EPISODE_not_the_file():
    """The file id is precisely the thing about to change — an upgrade deletes the old
    file and imports a new one with a new id. Keying on what SURVIVES the upgrade is
    what makes "did it land?" answerable."""
    assert upgrade_key(17209, 1, 2) == "17209:S01E02"
    assert upgrade_key(17209, None, 2) is None
    assert upgrade_key("x", 1, 2) is None
    i = _intent(63808)
    assert i["key"] == "17209:S01E02" and i["from_file_id"] == 63808


def test_an_unkeyable_intent_is_refused_rather_than_stored():
    """It would sit on the worklist forever and never resolve."""
    assert upgrade_intent(series_id=None, season=1, episode=2, file_id=1) is None


def test_a_changed_file_id_is_the_proof_the_upgrade_LANDED():
    led = merge_upgrade_intents({}, [_intent(63808)])
    r = reconcile_upgrades(led, {"17209:S01E02": 99999}, now=_NOW)
    assert len(r["fulfilled"]) == 1 and not r["pending"]
    assert r["fulfilled"][0]["observed_file_id"] == 99999
    assert r["fulfilled"][0]["from_file_id"] == 63808


def test_an_unchanged_id_inside_the_window_stays_PENDING():
    """Usenet queues. A 20 GB Remux sitting behind other downloads for hours is the
    normal state, not a failure — which is why the window is 48h and not 30 minutes."""
    led = merge_upgrade_intents({}, [_intent(63808, hours_ago=6)])
    r = reconcile_upgrades(led, {"17209:S01E02": 63808}, now=_NOW)
    assert not r["fulfilled"] and not r["abandoned"]
    assert "17209:S01E02" in r["pending"]


def test_an_unchanged_id_past_the_window_is_ABANDONED():
    led = merge_upgrade_intents({}, [_intent(63808, hours_ago=60)])
    r = reconcile_upgrades(led, {"17209:S01E02": 63808}, now=_NOW)
    assert len(r["abandoned"]) == 1 and not r["pending"]


def test_no_file_at_all_is_an_ORPHAN_not_a_fulfilment():
    """The old file is gone and nothing replaced it, so the parquet row is a dead
    pointer — exactly what the purge exists to remove."""
    led = merge_upgrade_intents({}, [_intent(63808)])
    r = reconcile_upgrades(led, {"17209:S01E02": None}, now=_NOW)
    assert len(r["orphaned"]) == 1 and not r["fulfilled"]


def test_ABSENT_and_NONE_are_not_the_same_thing():
    """**P-C.** A key absent from `observed` means "not examined this pass"; a key
    present with None means the *arr answered "no file". Conflating them would archive
    phantom orphans for every series whose fetch failed — the same shape as
    `_get_episode_files` returning [] for both empty and failed (GLD-DEL-09)."""
    led = merge_upgrade_intents({}, [_intent(63808)])
    absent = reconcile_upgrades(led, {}, now=_NOW)
    assert not absent["orphaned"] and "17209:S01E02" in absent["pending"]
    answered = reconcile_upgrades(led, {"17209:S01E02": None}, now=_NOW)
    assert len(answered["orphaned"]) == 1 and not answered["pending"]


def test_an_unreadable_observation_is_ignorance_too():
    led = merge_upgrade_intents({}, [_intent(63808)])
    r = reconcile_upgrades(led, {"17209:S01E02": "junk"}, now=_NOW)
    assert not r["orphaned"] and not r["fulfilled"] and r["pending"]


def test_a_re_triggered_upgrade_REPLACES_the_older_intent():
    """The older `from_file_id` is stale the moment the newer search fires, and
    keeping both would resolve the same episode twice."""
    led = merge_upgrade_intents({}, [_intent(63808, hours_ago=40)])
    led = merge_upgrade_intents(led, [_intent(70000, hours_ago=1)])
    assert len(led) == 1 and led["17209:S01E02"]["from_file_id"] == 70000
    r = reconcile_upgrades(led, {"17209:S01E02": 70000}, now=_NOW)
    assert not r["abandoned"] and r["pending"]          # young again, not stale


def test_a_missing_from_file_id_counts_as_fulfilled_when_a_file_appears():
    """An episode with no file that gains one is a successful acquisition, and
    leaving it pending forever would be wrong."""
    led = merge_upgrade_intents({}, [_intent(None)])
    r = reconcile_upgrades(led, {"17209:S01E02": 5150}, now=_NOW)
    assert len(r["fulfilled"]) == 1


def test_many_episodes_resolve_independently():
    led = {}
    for i, (fid, hrs) in enumerate([(1, 1), (2, 1), (3, 60), (4, 1)], start=1):
        led = merge_upgrade_intents(led, [_intent(fid, hours_ago=hrs, e=i)])
    obs = {"17209:S01E01": 900,        # changed -> fulfilled
           "17209:S01E02": 2,          # unchanged, young -> pending
           "17209:S01E03": 3,          # unchanged, stale -> abandoned
           "17209:S01E04": None}       # no file -> orphaned
    r = reconcile_upgrades(led, obs, now=_NOW)
    assert len(r["fulfilled"]) == 1 and len(r["pending"]) == 1
    assert len(r["abandoned"]) == 1 and len(r["orphaned"]) == 1


def test_upgrade_helpers_never_raise():
    for led in (None, {}, "x", {"k": None}, {"k": "junk"}, {"k": {}},
                {"k": {"from_file_id": "x", "at": None}}):
        for obs in (None, {}, "x", {"k": None}, {"k": "junk"}, {"k": 5}):
            r = reconcile_upgrades(led, obs, now=_NOW)
            assert isinstance(r, dict) and isinstance(r["pending"], dict)
    assert isinstance(merge_upgrade_intents(None, None), dict)
    assert isinstance(merge_upgrade_intents("x", [None, {}, {"key": "a"}]), dict)


# ── movies + archive emission (GLD-DEL-12 / GLD-DEL-13) ───────────────────────
def test_a_movie_gets_its_own_key_namespace():
    """A movie has no season/episode. The `movie:` prefix also means a Sonarr and a
    Radarr worklist can never collide on a shared numeric id."""
    assert upgrade_key(movie_id=812) == "movie:812"
    assert upgrade_key(movie_id=None, series_id=17209, season=1, episode=2) == "17209:S01E02"
    assert upgrade_key(movie_id="x") is None
    i = upgrade_intent(movie_id=812, file_id=9001, size_bytes=4_000_000_000,
                       quality_name="Remux-1080p", title="Inception")
    assert i["key"] == "movie:812" and i["media"] == "movie" and i["movie_id"] == 812
    assert "season" not in i and "series_id" not in i


def test_reconciliation_is_media_agnostic():
    """The comparison is id-then vs id-now; nothing about it is TV-specific, which is
    why Radarr needs only the two adapters and no second implementation (P-E)."""
    led = merge_upgrade_intents({}, [
        upgrade_intent(movie_id=812, file_id=9001, at=(_NOW - timedelta(hours=1)).isoformat()),
        _intent(63808)])
    r = reconcile_upgrades(led, {"movie:812": 9500, "17209:S01E02": 63808}, now=_NOW)
    assert len(r["fulfilled"]) == 1 and r["fulfilled"][0]["media"] == "movie"
    assert len(r["pending"]) == 1


def test_a_landed_upgrade_emits_an_event_with_a_MEASURED_replacement():
    """At trigger time the *arr had not picked a release, so an intent could only say
    what was being replaced. Emitting at RECONCILIATION is what lets `replaced_by`
    carry a real size — and that is what makes the spend figure trustworthy."""
    led = merge_upgrade_intents({}, [_intent(63808)])
    r = reconcile_upgrades(led, {"17209:S01E02": 99999}, now=_NOW)
    evs = upgrade_events(r, run_id=_RUN, instance="sonarr-720",
                         observed_files={99999: {"size_bytes": 6_000_000_000,
                                                 "quality_name": "WEBDL-1080p"}})
    assert len(evs) == 1
    e = evs[0]
    assert e["disposition"] == "upgraded" and e["source"] == "upgrade"
    assert e["size_bytes"] == 2_000_000_000              # what was replaced
    assert e["replaced_by"]["size_bytes"] == 6_000_000_000
    assert e["replaced_by"]["state"] == "imported"       # observed, not queued
    assert "WEBDL-720p" in e["reason"] and "WEBDL-1080p" in e["reason"]


def test_an_abandoned_upgrade_emits_an_event_with_no_replacement():
    led = merge_upgrade_intents({}, [_intent(63808, hours_ago=60)])
    r = reconcile_upgrades(led, {"17209:S01E02": 63808}, now=_NOW)
    evs = upgrade_events(r, run_id=_RUN, instance="sonarr-720")
    assert len(evs) == 1 and evs[0]["disposition"] == "upgrade-abandoned"
    assert "replaced_by" not in evs[0]


def test_pending_and_orphaned_emit_NOTHING():
    """`pending` has not happened yet; `orphaned` belongs to the purge, which owns
    that row's fate. Emitting either would put a non-event in an append-only file."""
    led = merge_upgrade_intents({}, [_intent(63808, e=2), _intent(1, e=3)])
    r = reconcile_upgrades(led, {"17209:S01E02": 63808, "17209:S01E03": None}, now=_NOW)
    assert r["pending"] and r["orphaned"]
    assert upgrade_events(r, run_id=_RUN, instance="sonarr-720") == []


def test_upgrade_events_close_the_churn_loop():
    """This is why GLD-DEL-13 exists: without an `upgraded` row, detect_churn only ever
    saw the step-down half and could never register a direction CHANGE."""
    up = upgrade_events(
        reconcile_upgrades(merge_upgrade_intents({}, [_intent(1)]),
                           {"17209:S01E02": 2}, now=_NOW),
        run_id=_RUN, instance="sonarr-720")
    for e in up:
        e["tmdb_id"] = 27205
    down = [deletion_record(run_id=_RUN, media="episode", instance="sonarr-720",
                            title="T", disposition="stepped-down", tmdb_id=27205,
                            deleted_at="2026-08-24T06:00:00+00:00")]
    assert detect_churn(up, min_flips=1, window_days=None) == []      # one side alone
    hits = detect_churn(up + down, min_flips=1, window_days=None)
    assert len(hits) == 1 and hits[0]["flips"] == 1


def test_upgrade_events_never_raise():
    for r in (None, {}, "x", {"fulfilled": None}, {"fulfilled": [None, "x", {}]},
              {"abandoned": [{}]}, {"fulfilled": [{"observed_file_id": None}]}):
        evs = upgrade_events(r, run_id=_RUN, instance="i", observed_files="junk")
        assert isinstance(evs, list)

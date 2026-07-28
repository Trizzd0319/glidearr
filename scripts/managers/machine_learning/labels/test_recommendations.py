"""Tests for the recommendation-event ledger: event build, month-partitioned append,
IDEMPOTENT re-load (republishing the same shelf twice in a day is one row per pick), and the
re-surface cooldown."""
from __future__ import annotations

from scripts.managers.machine_learning.labels.recommendations import (
    COLUMNS,
    append_events,
    build_events,
    iso_to_epoch,
    load_events,
    open_picks,
    recent_entity_ids,
    recommendation_dir,
)

_DAY = 86400.0
_TS = "2026-07-20T12:00:00+00:00"
_T0 = iso_to_epoch(_TS)


def _picks():
    return [{"tmdb_id": 603, "title": "The Matrix", "year": 1999, "taste_score": 71.4, "rank": 0},
            {"tmdb_id": 78, "title": "Blade Runner", "year": 1982, "taste_score": 64.0, "rank": 1}]


def test_build_events_carries_the_join_keys():
    print("test_build_events_carries_the_join_keys:")
    rows = build_events(_picks(), profile="rob", recommended_at=_TS, window_days=30)
    assert [r["entity_id"] for r in rows] == ["603", "78"]
    assert [r["rank"] for r in rows] == [0, 1]
    assert rows[0]["recommended_date"] == "2026-07-20"
    assert rows[0]["surface"] == "hidden_gems" and rows[0]["profile"] == "rob"
    assert rows[0]["taste_score"] == 71.4 and rows[0]["window_days"] == 30
    # title + year are stored so the outcome join survives the file being deleted.
    assert rows[0]["title"] == "The Matrix" and rows[0]["year"] == 1999
    assert set(rows[0]) == set(COLUMNS)
    # a pick with no id has nothing to join an outcome to -> dropped, never half-written
    assert build_events([{"title": "No id"}], profile="rob", recommended_at=_TS) == []


def test_events_persist_and_reload_is_idempotent(tmp_path):
    """Republishing the same shelf on the same day must NOT double-count: the ledger dedupes on
    (date, surface, profile, entity) keeping last, so the second append adds 0 new rows and the
    reload still shows one row per pick."""
    print("test_events_persist_and_reload_is_idempotent:")
    rows = build_events(_picks(), profile="rob", recommended_at=_TS)
    assert append_events(tmp_path, rows) == 2
    assert append_events(tmp_path, rows) == 0                     # idempotent re-run
    df = load_events(tmp_path)
    assert len(df) == 2 and sorted(df["entity_id"]) == ["603", "78"]
    assert (recommendation_dir(tmp_path) / "2026-07.parquet").is_file()
    # a later run the SAME day wins on the mutable fields (a re-ranked shelf)
    rerank = build_events([{**_picks()[0], "rank": 4, "taste_score": 80.0}],
                          profile="rob", recommended_at="2026-07-20T23:00:00+00:00")
    assert append_events(tmp_path, rerank) == 0
    df = load_events(tmp_path)
    assert float(df[df["entity_id"] == "603"]["taste_score"].iloc[0]) == 80.0
    assert len(df) == 2


def test_a_new_day_is_a_new_measurable_event(tmp_path):
    print("test_a_new_day_is_a_new_measurable_event:")
    append_events(tmp_path, build_events(_picks()[:1], profile="rob", recommended_at=_TS))
    append_events(tmp_path, build_events(_picks()[:1], profile="rob",
                                         recommended_at="2026-08-25T12:00:00+00:00"))
    df = load_events(tmp_path)
    assert len(df) == 2                                           # two partitions, two events
    assert sorted(p.name for p in recommendation_dir(tmp_path).glob("*.parquet")) == \
        ["2026-07.parquet", "2026-08.parquet"]


def test_per_profile_events_do_not_collide(tmp_path):
    print("test_per_profile_events_do_not_collide:")
    append_events(tmp_path, build_events(_picks()[:1], profile="rob", recommended_at=_TS))
    append_events(tmp_path, build_events(_picks()[:1], profile="kid", recommended_at=_TS))
    assert len(load_events(tmp_path)) == 2
    assert list(load_events(tmp_path, profile="kid")["profile"]) == ["kid"]


def test_missing_ledger_loads_as_an_empty_frame(tmp_path):
    print("test_missing_ledger_loads_as_an_empty_frame:")
    df = load_events(tmp_path / "nothing-here")
    assert df.empty and list(df.columns) == list(COLUMNS)


def test_recent_ids_are_the_resurface_cooldown(tmp_path):
    """A published pick owns its slot for the whole measurement window — re-offering it on day
    3 would corrupt the label (which recommendation caused the play?)."""
    print("test_recent_ids_are_the_resurface_cooldown:")
    append_events(tmp_path, build_events(_picks(), profile="rob", recommended_at=_TS,
                                         window_days=30))
    df = load_events(tmp_path)
    assert recent_entity_ids(df, profile="rob", now_ts=_T0 + 3 * _DAY) == {603, 78}
    assert recent_entity_ids(df, profile="rob", now_ts=_T0 + 31 * _DAY) == set()
    assert recent_entity_ids(df, profile="kid", now_ts=_T0 + 3 * _DAY) == set()
    # shortening the knob must NOT retroactively re-surface a pick still being measured under
    # the longer window it was published with.
    assert recent_entity_ids(df, profile="rob", now_ts=_T0 + 10 * _DAY,
                             window_days=7) == {603, 78}


def test_open_picks_are_ordered_oldest_first_and_deduped(tmp_path):
    """The shelf's HELD slots: oldest publication first (so the shelf order is stable as it
    tops up), deduped on the EARLIEST publication (the one actually being measured)."""
    print("test_open_picks_are_ordered_oldest_first_and_deduped:")
    append_events(tmp_path, build_events(_picks(), profile="rob", recommended_at=_TS))
    append_events(tmp_path, build_events(
        [{"tmdb_id": 99, "title": "Later", "year": 2020, "taste_score": 90.0, "rank": 0},
         {"tmdb_id": 603, "title": "The Matrix", "year": 1999, "taste_score": 71.4, "rank": 0}],
        profile="rob", recommended_at="2026-07-25T12:00:00+00:00"))
    df = load_events(tmp_path)
    ids = [p["tmdb_id"] for p in open_picks(df, profile="rob", now_ts=_T0 + 2 * _DAY)]
    assert ids == [603, 78, 99]                  # day-1 pair first (by rank), then the newcomer
    assert len(ids) == len(set(ids))             # 603 appears once, at its ORIGINAL position
    # once every window closes there is nothing held
    assert open_picks(df, profile="rob", now_ts=_T0 + 40 * _DAY) == []


def test_iso_to_epoch_reads_a_naive_stamp_as_utc():
    print("test_iso_to_epoch_reads_a_naive_stamp_as_utc:")
    assert iso_to_epoch("2026-07-20T12:00:00") == iso_to_epoch("2026-07-20T12:00:00+00:00")
    assert iso_to_epoch("2026-07-20T12:00:00Z") == _T0
    assert iso_to_epoch(None) is None and iso_to_epoch("not a date") is None
